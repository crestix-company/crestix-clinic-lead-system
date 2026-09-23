from collections import defaultdict
from copy import deepcopy
import re
import unicodedata
from src.scoring.age_estimator import AgeEstimator
from src.normalizer.clinic_name import normalize_person
from src.utils.date_utils import parse_year, today_japan


def graduation_evidence(record,pages):
    """院長名が同じプロフィールで、大学医学部の卒業年だけを抽出。"""
    name = normalize_person(record.get("manager_name", ""))
    if not name:
        return []
    result = []
    year_pattern = r"(?:19\d{2}|20\d{2}|(?:平成|昭和|令和)\s*(?:元|\d{1,2}))年?"
    for page in pages:
        text = unicodedata.normalize("NFKC",page.main_text)
        if not re.search(r"院長|管理者",text) or name not in normalize_person(text):
            continue
        # 複数医師をまとめたスタッフページでは所属医師の経歴と混同しない。
        if len(re.findall(r"(?:副院長|医師紹介|非常勤医師)",text)) or len(re.findall(r"大学.{0,20}医学部.{0,15}卒業",text))>1:
            continue
        for m in re.finditer(r"("+year_pattern+r")[\s:：、]*([^。\n]{0,65}大学[^。\n]{0,30}医学部[^。\n]{0,15}卒業)",text):
            year = parse_year(m[1])
            if year and 1900<=year<=today_japan().year:
                result.append({"year":year,"url":page.url,"evidence":m[0],"doctor_name":record.get("manager_name", "")})
        # 年と学校が表の逆順になっている形式。
        for m in re.finditer(r"大学[^。\n]{0,20}医学部\s*("+year_pattern+r")\s*卒業",text):
            year = parse_year(m[1])
            if year and 1900<=year<=today_japan().year:
                result.append({"year":year,"url":page.url,"evidence":m[0],"doctor_name":record.get("manager_name", "")})
    return result


def estimate_profile_age(record,pages=(),current_year=None):
    current_year = current_year or today_japan().year
    evidence = graduation_evidence(record,pages) if pages else record.get("graduation_evidence",[])
    years = {e["year"] for e in evidence}
    lic = parse_year(record.get("license_registration_year"))
    if lic and not 1900<=lic<=current_year:
        lic = None
    grad = next(iter(years)) if len(years)==1 else parse_year(record.get("graduation_year")) if not years else None
    result = {"doctor_name":record.get("manager_name",""),"license_registration_year":lic,"graduation_year":grad,
              "graduation_evidence":evidence,"age_probability_under_59":None,"age_estimation_source":"UNKNOWN",
              "age_estimation_confidence":"UNKNOWN","age_estimation_year":current_year}
    if len(years)>1 or (lic and grad and (lic<grad or lic-grad>3)):
        return {**result,"age_estimation_confidence":"REVIEW","age_estimation_reason":"卒業年・医籍登録年が矛盾しています。本人と経歴を確認してください。"}
    if not lic and not grad:
        return result
    if lic:
        estimate = AgeEstimator().estimate(lic,current_year)
        source = record.get("license_source","医籍登録年")
    else:
        # 卒業年には国家試験の遅延を加算しない。
        config = deepcopy(AgeEstimator().config)
        config["national_exam_delay_distribution"] = {"delay_0":1.0}
        estimate = AgeEstimator(config).estimate(grad,current_year)
        source = evidence[0]["url"] if evidence else "卒業年"
    return {**result,"age_probability_under_59":estimate.probability,"age_estimation_source":source,
            "age_estimation_confidence":"MODEL_ESTIMATE","age_estimation_reason":"仮定分布による59歳以下確率。実年齢の確定ではありません。"}
