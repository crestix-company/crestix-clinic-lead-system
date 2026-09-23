from copy import deepcopy
import re
import unicodedata
from bs4 import BeautifulSoup
from src.scoring.age_estimator import AgeEstimator
from src.normalizer.clinic_name import normalize_person
from src.utils.date_utils import parse_year, parse_date, today_japan

YEAR_PATTERN = r"(?:19\d{2}|20\d{2}|(?:平成|昭和|令和)\s*(?:元|\d{1,2}))年?"


def _clean(text):
    return unicodedata.normalize("NFKC",str(text or ""))


def _director_chunks(page, manager_name):
    """複数医師ページでも、現在の管理者名の近傍だけを経歴候補にする。"""
    name=normalize_person(manager_name)
    if not name:
        return []
    soup=BeautifulSoup(page.html,"html.parser")
    for x in soup.select("script,style,noscript,template"):
        x.decompose()
    chunks=[]
    seen=set()
    for node in soup.find_all(string=True):
        raw=_clean(node)
        if name not in normalize_person(raw):
            continue
        cur=node.parent
        selected=None
        for _ in range(6):
            if cur is None:
                break
            text=_clean(cur.get_text(" ",strip=True))
            # 名前を含む最小の経歴ブロックを採用し、他医師の経歴との混同を避ける。
            if 20 <= len(text) <= 5000 and name in normalize_person(text) and re.search(r"院長|管理者|経歴|略歴|プロフィール|卒業|医師免許|医籍",text):
                selected=text
                if re.search(r"卒業|医師免許|医籍",text):
                    break
            cur=cur.parent
        if selected:
            key=selected[:1000]
            if key not in seen:
                seen.add(key);chunks.append(selected)
    # DOM上で名前が分割される場合の補完。ページ全体を使うのは本人名と院長表記が両方あるときだけ。
    full=_clean(soup.get_text(" ",strip=True))
    if not chunks and name in normalize_person(full) and re.search(r"院長|管理者",full):
        pos=normalize_person(full).find(name)
        # normalize後の位置は原文位置とずれるので、長すぎないページだけ全体を候補にする。
        if len(full)<=8000:
            chunks=[full]
    return chunks


def _years_from_chunks(chunks, kind):
    results=[]
    for text in chunks:
        if kind=="graduation":
            patterns=[
                rf"({YEAR_PATTERN})[^。\n]{{0,90}}?(?:大学[^。\n]{{0,35}}?(?:医学部|医科大学)[^。\n]{{0,25}}?(?:卒業|卒))",
                rf"(?:大学[^。\n]{{0,35}}?(?:医学部|医科大学)[^。\n]{{0,25}}?(?:卒業|卒))[^。\n]{{0,50}}?({YEAR_PATTERN})",
            ]
        else:
            patterns=[
                rf"({YEAR_PATTERN})[^。\n]{{0,70}}?(?:医師免許(?:取得)?|医籍(?:登録)?|医師国家試験(?:合格)?)",
                rf"(?:医師免許(?:取得)?|医籍(?:登録)?|医師国家試験(?:合格)?)[^。\n]{{0,50}}?({YEAR_PATTERN})",
            ]
        for pattern in patterns:
            for m in re.finditer(pattern,text,re.I):
                year=parse_year(m[1])
                if year and 1900<=year<=today_japan().year:
                    results.append((year,m[0]))
    return results


def graduation_evidence(record,pages):
    """現在の院長/管理者本人の近傍から大学医学部の卒業年を抽出。複数医師ページにも対応。"""
    manager=record.get("manager_name","")
    result=[]
    seen=set()
    for page in pages:
        for year,evidence in _years_from_chunks(_director_chunks(page,manager),"graduation"):
            key=(year,page.url)
            if key in seen:
                continue
            seen.add(key)
            result.append({"year":year,"url":page.url,"evidence":evidence[:300],"doctor_name":manager})
    return result


def license_evidence(record,pages):
    manager=record.get("manager_name","")
    result=[]
    seen=set()
    for page in pages:
        for year,evidence in _years_from_chunks(_director_chunks(page,manager),"license"):
            key=(year,page.url)
            if key in seen:
                continue
            seen.add(key)
            result.append({"year":year,"url":page.url,"evidence":evidence[:300],"doctor_name":manager})
    return result


def _int_age(value):
    try:
        age=int(float(str(value).strip()))
        return age if 20<=age<=100 else None
    except (TypeError,ValueError):
        return None


def estimate_profile_age(record,pages=(),current_year=None):
    """年齢根拠の優先順: 公的/取込済み年齢・生年・医籍年 → HPの医籍年 → HP卒業年。

    指定年月日は開業時期の公的情報として先に確認するが、年齢そのものは算出できないため
    年齢値の根拠には使わず、経歴へ自動フォールバックする。
    """
    current_year=current_year or today_japan().year
    designation=parse_date(record.get("designation_date",""))
    master_context={
        "designation_date":designation.isoformat() if designation else str(record.get("designation_date","") or ""),
        "registration_reason":str(record.get("registration_reason","") or ""),
        "owner_manager_equal":record.get("owner_manager_equal"),
    }

    result={
        "doctor_name":record.get("manager_name",""),"license_registration_year":None,"graduation_year":None,
        "graduation_evidence":[],"license_evidence":[],"age_probability_under_59":None,
        "age_estimation_source":"UNKNOWN","age_estimation_confidence":"UNKNOWN","age_estimation_year":current_year,
        "age_master_context":master_context,
    }

    direct_age=next((_int_age(record.get(k)) for k in ["manager_age","doctor_age"] if _int_age(record.get(k)) is not None),None)
    if direct_age is None and record.get("owner_manager_equal") is True:
        direct_age=_int_age(record.get("owner_age"))
    if direct_age is not None:
        return {**result,"age_probability_under_59":1.0 if direct_age<=59 else 0.0,
                "age_estimation_source":"厚生局/補足マスタの管理者年齢","age_estimation_confidence":"OFFICIAL_VALUE",
                "age_estimation_reason":f"公的・取込済み管理者年齢 {direct_age}歳を使用。指定年月日も確認済み。"}

    birth=parse_year(record.get("manager_birth_year") or record.get("doctor_birth_year"))
    if birth and 1900<=birth<=current_year:
        approx=current_year-birth
        probability=1.0 if approx<=58 else .5 if approx==59 else 0.0
        return {**result,"age_probability_under_59":probability,"age_estimation_source":"厚生局/補足マスタの管理者生年",
                "age_estimation_confidence":"OFFICIAL_YEAR","age_estimation_reason":"生年のみのため誕生日未反映。指定年月日も確認済み。"}

    grad_evidence=graduation_evidence(record,pages) if pages else record.get("graduation_evidence",[]) or []
    lic_evidence=license_evidence(record,pages) if pages else record.get("license_evidence",[]) or []
    grad_years={e["year"] for e in grad_evidence if e.get("year")}
    lic_years={e["year"] for e in lic_evidence if e.get("year")}

    lic=parse_year(record.get("license_registration_year"))
    if not lic and len(lic_years)==1:
        lic=next(iter(lic_years))
    if lic and not 1900<=lic<=current_year:
        lic=None
    grad=parse_year(record.get("graduation_year"))
    if not grad and len(grad_years)==1:
        grad=next(iter(grad_years))
    if grad and not 1900<=grad<=current_year:
        grad=None

    result.update(license_registration_year=lic,graduation_year=grad,
                  graduation_evidence=grad_evidence,license_evidence=lic_evidence)

    if len(grad_years)>1 or len(lic_years)>1 or (lic and grad and (lic<grad or lic-grad>4)):
        return {**result,"age_estimation_confidence":"REVIEW",
                "age_estimation_reason":"院長本人の卒業年/医籍年候補が複数または矛盾しています。指定年月日では年齢を確定できないため要確認。"}

    if not lic and not grad:
        source=(f"厚生局指定年月日 {designation.isoformat()} を確認（年齢は直接算出不可）" if designation else "年齢根拠未取得")
        return {**result,"age_estimation_source":source,
                "age_estimation_reason":"指定年月日は開業時期の根拠であり院長年齢ではないため、HPの院長経歴まで確認したが卒業年/医籍年を取得できませんでした。"}

    if lic:
        estimate=AgeEstimator().estimate(lic,current_year)
        if record.get("license_registration_year"):
            source=record.get("license_source") or "厚生局/取込済み医籍登録年"
        else:
            source=lic_evidence[0]["url"] if lic_evidence else "HP院長経歴の医師免許年"
    else:
        config=deepcopy(AgeEstimator().config)
        config["national_exam_delay_distribution"]={"delay_0":1.0}
        estimate=AgeEstimator(config).estimate(grad,current_year)
        source=grad_evidence[0]["url"] if grad_evidence else "HP院長経歴の大学卒業年"

    return {**result,"age_probability_under_59":estimate.probability,"age_estimation_source":source,
            "age_estimation_confidence":"MODEL_ESTIMATE",
            "age_estimation_reason":"指定年月日を先に確認し、年齢を直接算出できないため院長本人の医籍年/大学卒業年から仮定分布で59歳以下確率を推定。"}
