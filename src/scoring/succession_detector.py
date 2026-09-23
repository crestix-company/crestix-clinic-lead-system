from dataclasses import dataclass
import re
from src.normalizer.clinic_name import normalize_person, person_from_owner, surname


@dataclass
class Succession:
    owner_manager_equal: bool | None
    candidate: bool
    score: int
    reasons: list[str]
    change_reason: bool


def detect_succession(record, age, config):
    owner = person_from_owner(record.get("owner_name", ""))
    manager = record.get("manager_name", "")
    equal = normalize_person(owner) == normalize_person(manager) if owner and manager else None
    reason = record.get("registration_reason", "")
    text = record.get("profile_text", "")
    # 単純キーワードの否定文を肯定根拠にしない。複雑な文脈は要確認。
    sentences = re.split(r"[。！？\n]", text)
    affirmative = "。".join(s for s in sentences if not re.search(r"ではない|していない|ありません|関係ない|無関係", s))
    weights = config["weights"]
    evidence, score = [], 0
    def add(key, message):
        nonlocal score
        score += int(weights[key])
        evidence.append(message)
    inheritance = any(k in reason for k in config["inheritance_reasons"])
    change = any(k in reason for k in config["change_reasons"])
    profile_inheritance = any(k in affirmative for k in config["profile_inheritance_keywords"])
    profile_family = any(k in affirmative for k in config["profile_family_keywords"])
    appointment = any(k in affirmative for k in config["profile_appointment_keywords"])
    if inheritance:
        add("inheritance_reason", "登録理由に継承/承継")
    elif change:
        add("change_reason", "登録理由に交代/管理者変更/開設者変更")
    a, b = surname(record.get("owner_name", ""), record.get("owner_surname", "")), surname(manager, record.get("manager_surname", ""))
    if a and b and a == b:
        add("same_surname", "姓が一致（親子関係の確定ではない）")
    if age.young is True:
        add("young_manager", "管理者の59歳以下確率が閾値以上")
    if profile_inheritance:
        add("profile_inheritance", "プロフィールに承継・代替わりの記載")
    if profile_family:
        add("profile_family", "プロフィールに親族に関する記載")
    if appointment:
        add("profile_appointment", "プロフィールに院長就任・副院長の記載")
    try:
        owner_age = float(record.get("owner_age", ""))
        if age.median is not None and owner_age - age.median >= config["minimum_age_gap"]:
            add("age_gap", "開設者の確認年齢と管理者推定中央値に世代差")
    except (TypeError, ValueError):
        pass
    if str(record.get("family_business_evidence", "")).strip():
        add("family_business", "手動で家族経営の根拠を記録済み")
    strong = inheritance or change or profile_inheritance or (profile_family and appointment)
    candidate = score >= config["threshold"] and (strong or not config["require_strong_evidence"])
    # 法人から個人が抽出できなくても、管理者と承継根拠があれば候補にできる。
    return Succession(equal, bool(candidate), min(100, score), evidence, inheritance or change)
