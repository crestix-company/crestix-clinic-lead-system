"""完全一致は内部IDへ集約し、類似一致は人による確認まで保留する。"""
from dataclasses import dataclass
from rapidfuzz import fuzz
from src.normalizer.phone import normalize_phone
from src.normalizer.clinic_name import normalize_clinic_name
from src.normalizer.address import normalize_address


@dataclass
class MasterMatch:
    status: str
    candidates: list[int]
    reason: str
    score: float = 0


def medical_key(record):
    code = str(record.get("clinic_id", "")).strip()
    if not code:
        return ""
    code = code.split(":")[-1].replace(",", "").replace("-", "")
    # 県・医科/歯科の名前空間を分離。区分不明のコードで自動結合しない。
    if not record.get("prefecture") or not record.get("medical_type"):
        return ""
    return f"{record['prefecture']}:{record['medical_type']}:{code}"


def match_record(conn, record):
    phone, name, address = normalize_phone(record.get("phone")), normalize_clinic_name(record.get("clinic_name")), normalize_address(record.get("address"))
    med = medical_key(record)
    uid = str(record.get("uuid", "")).strip()
    def rows(sql, args):
        return [dict(r) for r in conn.execute(sql, args)]
    def choose(pool, reason, score=100):
        return MasterMatch("MATCHED" if len(pool) == 1 else "AMBIGUOUS", [r["id"] for r in pool], reason, score)
    if uid:
        pool = rows("SELECT * FROM clinics WHERE uuid=? AND merged_into IS NULL", (uid,))
        if pool:
            if med and pool[0]["medical_key"] and pool[0]["medical_key"] != med:
                return MasterMatch("AMBIGUOUS", [r["id"] for r in pool], "同じUUIDに異なる医療機関番号", 100)
            return choose(pool, "既存UUID一致")
    if med:
        pool = rows("SELECT * FROM clinics WHERE medical_key=? AND merged_into IS NULL", (med,))
        if pool:
            if uid and any(r["uuid"] and r["uuid"] != uid for r in pool):
                return MasterMatch("AMBIGUOUS", [r["id"] for r in pool], "医療機関番号一致・別UUIDあり", 100)
            return choose(pool, "医療機関番号一致")
    if phone:
        pool = rows("SELECT * FROM clinics WHERE phone_norm=? AND merged_into IS NULL", (phone,))
        if pool:
            exact = [r for r in pool if name and address and r["name_norm"] == name and r["address_norm"] == address]
            selected = exact if len(exact) == 1 else pool
            compatible = [r for r in selected if not (med and r["medical_key"] and med != r["medical_key"])
                          and not (uid and r["uuid"] and uid != r["uuid"])
                          and not (record.get("prefecture") and r["prefecture"] and record["prefecture"] != r["prefecture"])
                          and not (record.get("medical_type") and r["medical_type"] and record["medical_type"] != r["medical_type"])]
            if len(selected) == len(compatible) == 1:
                r = selected[0]
                name_ok = bool(name and r["name_norm"] and fuzz.ratio(name, r["name_norm"]) >= 80)
                addr_ok = bool(address and r["address_norm"] and address == r["address_norm"])
                if (name_ok and (not address or not r["address_norm"] or fuzz.ratio(address, r["address_norm"]) >= 80)) or (addr_ok and not name):
                    return choose(selected, "電話番号一致・医院情報で裏付け")
            return MasterMatch("AMBIGUOUS", [r["id"] for r in pool], "共有電話・医院情報またはUUIDの相違", 90)
    if name and address:
        pool = rows("SELECT * FROM clinics WHERE name_norm=? AND address_norm=? AND merged_into IS NULL", (name, address))
        if pool:
            if any((med and r["medical_key"] and med != r["medical_key"]) or (uid and r["uuid"] and uid != r["uuid"]) for r in pool):
                return MasterMatch("AMBIGUOUS", [r["id"] for r in pool], "医院名・住所一致だが識別番号が相違", 100)
            return choose(pool, "医院名＋住所一致")
        # 全国全件fuzzy比較を避け、同じ名前先頭2文字/県で候補を絞る。
        pool = rows("SELECT * FROM clinics WHERE name_prefix=? AND (prefecture=? OR prefecture='') AND merged_into IS NULL LIMIT 200", (name[:2], record.get("prefecture", "")))
        scored = [(0.65*fuzz.ratio(name, r["name_norm"])+0.35*fuzz.ratio(address, r["address_norm"]), r) for r in pool
                  if fuzz.ratio(name, r["name_norm"]) >= 80 and fuzz.ratio(address, r["address_norm"]) >= 80]
        scored.sort(key=lambda x: x[0], reverse=True)
        if scored:
            return MasterMatch("AMBIGUOUS", [r["id"] for _, r in scored[:10]], "医院名・住所が類似。自動統合は保留", round(scored[0][0], 2))
    return MasterMatch("NEW", [], "一致する医院なし")
