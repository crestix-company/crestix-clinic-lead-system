"""電話の完全一致、医院名＋住所の完全一致、曖昧な候補の順に照合する。"""
from dataclasses import dataclass
from rapidfuzz import fuzz
from src.normalizer.phone import tel_match_key
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
    if not record.get("prefecture") or not record.get("medical_type"):
        return ""
    return f"{record['prefecture']}:{record['medical_type']}:{code}"


def match_record(conn, record, *, exclude_ids=(), comdesk_only=False, track_identity=True):
    phone = tel_match_key(record.get("phone"))
    name = normalize_clinic_name(record.get("clinic_name"))
    address = normalize_address(record.get("address"))
    med = medical_key(record)
    uid = str(record.get("uuid", "")).strip()
    scope = "merged_into IS NULL"
    scope_args = []
    if exclude_ids:
        scope += " AND id NOT IN (" + ",".join("?" for _ in exclude_ids) + ")"
        scope_args.extend(exclude_ids)
    if comdesk_only:
        scope += " AND EXISTS(SELECT 1 FROM comdesk_original_rows o WHERE o.clinic_id=clinics.id)"

    def rows(condition, args):
        return [dict(row) for row in conn.execute("SELECT * FROM clinics WHERE " + scope + " AND " + condition,
                                                  (*scope_args, *args))]

    identity = []
    if track_identity:
        if uid:
            identity.extend(rows("uuid=?", (uid,)))
        if med:
            identity.extend(rows("medical_key=?", (med,)))
        identity = list({row["id"]: row for row in identity}.values())

    def ambiguous(pool, reason, score=90):
        return MasterMatch("AMBIGUOUS", sorted({row["id"] for row in pool + identity}), reason, score)

    def compatible_medical_identity(pool):
        """厚生局の明示的な医療機関番号が異なる施設は、名称・住所・電話が似ていても別施設として扱う。

        コムデスク由来で medical_key が未設定の候補は残すため、既存UUIDとの名寄せは従来どおり行える。
        同じ medical_key の月次更新も候補に残る。
        """
        if not med:
            return pool
        # 同じ医療機関番号をすでに保持している月次更新では、電話等が別の
        # 医療機関番号を指す矛盾も要確認に残す。新しい厚生局施設を初回投入
        # するときだけ、別の明示的な医療機関番号を候補から除外する。
        if any(row["medical_key"] == med for row in identity):
            return pool
        return [row for row in pool if not row["medical_key"] or row["medical_key"] == med]

    def conflict(row):
        return bool(
            (uid and row["uuid"] and uid != row["uuid"])
            or (med and row["medical_key"] and med != row["medical_key"])
            or (record.get("prefecture") and row["prefecture"] and record["prefecture"] != row["prefecture"])
            or (record.get("medical_type") and row["medical_type"] and record["medical_type"] != row["medical_type"])
            or row.get("merge_hold", 0)
        )

    def select(pool, reason):
        if len(pool) != 1 or conflict(pool[0]) or any(row["id"] != pool[0]["id"] for row in identity):
            return ambiguous(pool, reason + "・複数候補または識別情報の相違")
        return MasterMatch("MATCHED", [pool[0]["id"]], reason, 100)

    # 1. 空のキー同士は一致扱いにしない。
    if phone:
        pool = compatible_medical_identity(rows("tel_match_key=?", (phone,)))
        if pool:
            exact = [row for row in pool if name and address and row["name_norm"] == name and row["address_norm"] == address]
            selected = exact if len(exact) == 1 else pool
            if len(selected) == 1:
                candidate = selected[0]
                name_differs = bool(name and candidate["name_norm"] and fuzz.ratio(name, candidate["name_norm"]) < 80)
                address_differs = bool(address and candidate["address_norm"] and fuzz.ratio(address, candidate["address_norm"]) < 80)
                if not name_differs and not address_differs:
                    return select(selected, "電話番号キー完全一致")
            return ambiguous(pool, "電話番号キー一致・共有電話または医院情報の相違")

    # 2. 電話に候補がない場合、名前と住所の両方が完全一致するかを確認。
    if name and address:
        pool = compatible_medical_identity(rows("name_norm=? AND address_norm=?", (name, address)))
        if pool:
            return select(pool, "医院名＋住所完全一致")

    # 同じUUID/医療機関番号の再取込は既存レコードの更新として追跡する。
    # 電話や名前＋住所が別案件を示す場合は、上の処理で先に保留になる。
    if identity:
        return select(identity, "保存済み識別番号の更新")

    # 3. 類似一致は人による確認へ回す。
    if name and address:
        pool = compatible_medical_identity(
            rows("name_prefix=? AND (prefecture=? OR prefecture='')", (name[:2], record.get("prefecture", "")))
        )[:200]
        scored = [(0.65 * fuzz.ratio(name, row["name_norm"]) + 0.35 * fuzz.ratio(address, row["address_norm"]), row)
                  for row in pool if fuzz.ratio(name, row["name_norm"]) >= 80 and fuzz.ratio(address, row["address_norm"]) >= 80]
        scored.sort(key=lambda item: item[0], reverse=True)
        if scored:
            return ambiguous([row for _, row in scored[:10]], "医院名・住所が類似。自動統合は保留", round(scored[0][0], 2))
    return MasterMatch("NEW", [], "一致する医院なし")
