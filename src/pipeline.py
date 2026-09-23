from dataclasses import dataclass
from datetime import date
import hashlib
import json
import pandas as pd
from src.matching.kouseikyoku_matcher import KouseikyokuMatcher
from src.scoring.age_estimator import AgeEstimator
from src.scoring.succession_detector import detect_succession
from src.scoring.lead_ranker import rank_lead
from src.normalizer.phone import normalize_phone
from src.normalizer.address import normalize_address
from src.normalizer.clinic_name import normalize_clinic_name
from src.enrichment.epark_checker import canonical_epark_url, EparkResult
from src.enrichment.doctor_license import is_true
from src.utils.date_utils import within_years, parse_date, today_japan


@dataclass
class RunResult:
    judgments: pd.DataFrame
    final_indices: list[int]
    excluded_indices: list[int]
    review_indices: list[int]
    metrics: dict
    metadata: dict


def eligibility(record, prefecture, as_of, max_age):
    excluded, missing = [], []
    region = record.get("prefecture", "")
    if region and region != prefecture:
        excluded.append("対象都道府県外")
    elif not region:
        missing.append("都道府県が未確認")
    medical = record.get("medical_type", "")
    if medical and medical != "医科":
        excluded.append("医科以外")
    elif not medical:
        missing.append("医科/歯科区分が未確認")
    kind = record.get("facility_type", "")
    if kind in {"病院", "薬局"}:
        excluded.append("診療所・クリニック以外")
    elif kind not in {"診療所", "クリニック", "医院", "医科診療所"}:
        missing.append("病院/診療所区分が未確認")
    status = record.get("status", "")
    if status in {"休止", "廃止", "閉院", "閉業", "取消", "辞退", "false", "False", "0"}:
        excluded.append("休止・廃止等の記録あり")
    elif status not in {"現存", "営業中", "開業中", "稼働中", "true", "True", "1"}:
        missing.append("現在営業中か未確認")
    basis = parse_date(record.get("as_of", ""))
    if basis is None:
        missing.append("マスタの基準日が未取得")
    elif not 0 <= (as_of-basis).days <= max_age:
        missing.append("マスタが古いまたは未来日。現在の営業状況を確認")
    if excluded:
        return False, excluded
    return (None, missing) if missing else (True, [])


def extra_for(record, extra):
    if extra is None or extra.empty:
        return {}, ""
    hits = extra.iloc[0:0]
    if "clinic_id" in extra and record.get("clinic_id"):
        hits = extra[extra["clinic_id"] == record["clinic_id"]]
    if hits.empty and "phone" in extra and record.get("phone"):
        hits = extra[extra["phone"].map(normalize_phone) == normalize_phone(record["phone"])]
    if hits.empty and "clinic_name" in extra and "address" in extra:
        hits = extra[(extra["clinic_name"].map(normalize_clinic_name) == normalize_clinic_name(record.get("clinic_name"))) &
                     (extra["address"].map(normalize_address) == normalize_address(record.get("address"))) &
                     extra["address"].astype(bool)]
    hits = hits.drop_duplicates()
    if len(hits) > 1:
        return {}, "補足マスタの照合候補が複数"
    allowed = {"epark_url", "profile_text", "profile_source", "other_media", "hp_weak", "owner_age",
               "owner_surname", "manager_surname", "family_business_evidence"}
    return ({k: v for k, v in hits.iloc[0].items() if k in allowed and str(v).strip()}, "") if len(hits) else ({}, "")


def run_pipeline(table, mapping, master, config, licenses, epark_checker, *,
                 age_model=None, prefecture="東京都", as_of=None, options=None,
                 extra=None, html_files=None, allow_network=False, progress=None):
    as_of = as_of or today_japan()
    options = {**config["lead"], **(options or {})}
    age_model = age_model or AgeEstimator()
    matcher = KouseikyokuMatcher(master, config["matching"])
    judgments = []
    for i in range(len(table.data)):
        values = {k: table.value(i, mapping.get(k)) for k in ["phone", "clinic_name", "address", "url", "epark_url"]}
        match = matcher.match(values["phone"], values["clinic_name"], values["address"])
        record = match.record or {}
        extras, extra_issue = extra_for(record, extra) if record else ({}, "")
        record.update(extras)
        license_result = licenses.lookup(record.get("manager_name", ""), record.get("clinic_id", ""), record.get("phone", ""))
        age = age_model.estimate(license_result.year, as_of.year)
        succession = detect_succession(record, age, config["succession"])
        recent = within_years(record.get("designation_date", ""), as_of, options["recent_years"])
        url = values["epark_url"] or record.get("epark_url", "") or canonical_epark_url(values["url"])
        url_key = canonical_epark_url(url)
        if options["epark_enabled"]:
            manual_html = (html_files or {}).get(url_key)
            epark = epark_checker.check(url, html=manual_html, allow_network=allow_network)
        else:
            epark = EparkResult(url=url_key, reason="今回の判定ではEPARKを使用しない")
        if match.record:
            eligible, gate_reasons = eligibility(record, prefecture, as_of, config["matching"]["max_master_age_days"])
        else:
            eligible, gate_reasons = None, [match.reason]
        if not normalize_phone(values["phone"]):
            gate_reasons.append("元ファイルの電話番号が空白。インポート前に補完")
            if eligible is True:
                eligible = None
        if extra_issue:
            gate_reasons.append(extra_issue)
            if eligible is True:
                eligible = None
        decision = rank_lead(eligible=eligible, gate_reasons=gate_reasons, recent=recent, young=age.young,
                             succession=succession, epark=epark.status,
                             other_media=is_true(record.get("other_media", "")),
                             hp_weak=is_true(record.get("hp_weak", "")), options=options)
        review_reasons = []
        if "要確認" in decision.status:
            review_reasons.append(decision.reason)
        if decision.status != "除外" and not record.get("registration_reason"):
            review_reasons.append("登録理由が空白。指定年月日と実開院日は一致しない可能性")
        if decision.status != "除外" and record and not record.get("profile_source") and record.get("profile_text"):
            review_reasons.append("プロフィールの出典URLが未入力")
        age_label = "不明" if age.young is None else ("59歳以下" if age.young else "60歳以上")
        date_label = "不明" if recent is None else ("開業10年以内" if recent else "開業10年超")
        judgments.append({
            "入力行番号": i+2, "営業対象判定": decision.status, "営業優先度": decision.rank,
            "最終出力対象": decision.include, "最終判定理由": decision.reason,
            "要確認理由": " / ".join(review_reasons), "除外理由": decision.reason if decision.status == "除外" else "",
            "厚生局一致": bool(match.record), "照合方式": match.method, "照合スコア": match.score,
            "照合候補ID": " / ".join(match.candidates), "厚生局医療機関ID": record.get("clinic_id", ""),
            "医院名": record.get("clinic_name", "") or values["clinic_name"],
            "開設者氏名": record.get("owner_name", ""), "管理者氏名": record.get("manager_name", ""),
            "都道府県": record.get("prefecture", ""), "医科歯科区分": record.get("medical_type", ""),
            "病院診療所区分": record.get("facility_type", ""), "営業状況": record.get("status", ""),
            "厚生局基準日": record.get("as_of", ""), "厚生局出典": record.get("source_url", ""),
            "指定年月日": record.get("designation_date", ""), "登録理由": record.get("registration_reason", ""),
            "開業10年以内判定": date_label, "指定日基準10年以内": recent,
            "医籍登録年": age.registration_year, "医籍取得状況": license_result.status,
            "医籍出典": license_result.source, "医籍確認日": license_result.checked_at,
            "医籍信頼度": license_result.confidence, "医籍確認理由": license_result.reason,
            "推定年齢中央値": age.median, "推定年齢下限": age.lower, "推定年齢上限": age.upper,
            "59歳以下確率": age.probability, "年齢判定": age_label, "年齢判定理由": age.reason,
            "開設者管理者一致判定": "不明" if succession.owner_manager_equal is None else ("一致" if succession.owner_manager_equal else "不一致"),
            "継承候補判定": succession.candidate, "継承候補スコア": succession.score,
            "継承候補理由": " / ".join(succession.reasons) or "有効な根拠なし",
            "プロフィール出典": record.get("profile_source", ""),
            "EPARK URL": epark.url, "EPARK掲載有無": epark.listing, "EPARK課金判定": epark.status,
            "EPARK判定理由": epark.reason, "EPARK出典": epark.source,
            "EPARK確認日": epark.checked_at, "EPARK特徴": epark.features_json,
        })
        if progress:
            progress(i+1, len(table.data))
    frame = pd.DataFrame(judgments)
    if frame.empty:
        raise ValueError("判定対象行がありません。")
    final = frame.index[frame["最終出力対象"]].tolist()
    excluded = frame.index[frame["営業対象判定"] == "除外"].tolist()
    reviews = frame.index[(frame["営業対象判定"].str.contains("要確認")) | frame["要確認理由"].astype(bool)].tolist()
    counts = {
        "入力件数": len(frame), "厚生局一致件数": int(frame["厚生局一致"].sum()),
        "開業10年以内件数": int((frame["指定日基準10年以内"] == True).sum()),
        "59歳以下判定件数": int((frame["年齢判定"] == "59歳以下").sum()),
        "継承候補件数": int(frame["継承候補判定"].sum()),
        "EPARK課金済み件数": int((frame["EPARK課金判定"] == "課金済み").sum()),
        "最終営業対象件数": len(final), "要確認件数": len(reviews), "除外件数": len(excluded)
    }
    model_text = json.dumps(age_model.config, ensure_ascii=False, sort_keys=True)
    metadata = {"判定基準日": as_of.isoformat(), "対象都道府県": prefecture,
                "入力列数": len(table.headers), "元文字コード": table.encoding, "元シート": table.sheet_name,
                "条件設定": json.dumps(options, ensure_ascii=False),
                "年齢モデル": model_text,
                "年齢設定SHA256": hashlib.sha256(model_text.encode()).hexdigest(),
                "判定設定": json.dumps(config, ensure_ascii=False),
                "定義": "指定年月日による期間。年齢は仮定モデル。現存は一覧基準日時点。要確認リストは対象との重複あり。"}
    return RunResult(frame, final, excluded, reviews, counts, metadata)
