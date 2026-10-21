"""電話の完全一致、医院名＋住所の完全一致、曖昧な候補の順に照合する。"""
import json
import re
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


class ClinicMatchingIndex:
    """In-memory equivalent of the indexed SELECTs used by match_record()."""
    def __init__(self, clinics):
        self._rows = [dict(row) for row in clinics if row.get("merged_into") is None]
        self._indexes = {key: {} for key in ("uuid", "medical_key", "tel_match_key", "name_address", "name_prefix")}
        for row in self._rows:
            for key in ("uuid", "medical_key", "tel_match_key"):
                value = row.get(key)
                if value:
                    self._indexes[key].setdefault(value, []).append(row)
            name_address = (row.get("name_norm") or "", row.get("address_norm") or "")
            if all(name_address):
                self._indexes["name_address"].setdefault(name_address, []).append(row)
            prefix = row.get("name_prefix") or ""
            if prefix:
                self._indexes["name_prefix"].setdefault(prefix, []).append(row)

    def lookup(self, condition, args, exclude_ids=()):
        if condition == "uuid=?":
            rows = self._indexes["uuid"].get(args[0], [])
        elif condition == "medical_key=?":
            rows = self._indexes["medical_key"].get(args[0], [])
        elif condition == "tel_match_key=?":
            rows = self._indexes["tel_match_key"].get(args[0], [])
        elif condition == "name_norm=? AND address_norm=?":
            rows = self._indexes["name_address"].get((args[0], args[1]), [])
        elif condition == "name_prefix=? AND (prefecture=? OR prefecture='')":
            rows = [row for row in self._indexes["name_prefix"].get(args[0], [])
                    if (row.get("prefecture") or "") in {args[1], ""}]
        else:
            raise ValueError(f"Unsupported clinic lookup predicate: {condition}")
        excluded = {int(value) for value in exclude_ids}
        return [row for row in rows if int(row["id"]) not in excluded]


def _strict_address_key(value):
    """Canonicalize only the separator after a numbered address component.

    The database may contain address_norm values produced before this rule;
    applying it to both stored normalized values and incoming values avoids a
    data migration. Building names, floors, room labels, and all digits remain.
    """
    normalized = normalize_address(value)
    return re.sub(r"(?<=\d)-(?=[^\d-])", "", normalized)


def _strict_address_equivalent(left, right, left_prefecture="", right_prefecture=""):
    """Allow a verified prefecture-prefix omission while retaining all address detail."""
    left_key, right_key = _strict_address_key(left), _strict_address_key(right)
    if left_key == right_key:
        return bool(left_key)
    from src.master.comdesk import PREFECTURE

    left_pref, right_pref = _strict_address_key(left_prefecture), _strict_address_key(right_prefecture)
    if left_pref and right_pref and left_pref != right_pref:
        return False
    trusted_pref = left_pref or right_pref
    if not trusted_pref:
        return False
    left_match, right_match = PREFECTURE.match(left_key), PREFECTURE.match(right_key)
    if bool(left_match) == bool(right_match):
        return False
    prefixed, unprefixed = (left_key, right_key) if left_match else (right_key, left_key)
    match = left_match or right_match
    if _strict_address_key(match[1]) != trusted_pref:
        return False
    explicit_unprefixed = right_pref if left_match else left_pref
    if explicit_unprefixed and explicit_unprefixed != trusted_pref:
        return False
    return prefixed[len(match[1]):] == unprefixed


def medical_key(record):
    code = str(record.get("clinic_id", "")).strip()
    if not code:
        return ""
    code = code.split(":")[-1].replace(",", "").replace("-", "")
    if not record.get("prefecture") or not record.get("medical_type"):
        return ""
    return f"{record['prefecture']}:{record['medical_type']}:{code}"


def match_record(conn, record, *, exclude_ids=(), comdesk_only=False, track_identity=True,
                 strict_identity=False, candidate_index=None):
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
        if candidate_index is not None:
            return candidate_index.lookup(condition, args, exclude_ids)
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

    def basic_conflict(row):
        return bool(
            (uid and row["uuid"] and uid != row["uuid"])
            or (med and row["medical_key"] and med != row["medical_key"])
            or (record.get("prefecture") and row["prefecture"] and record["prefecture"] != row["prefecture"])
            or (record.get("medical_type") and row["medical_type"] and record["medical_type"] != row["medical_type"])
            or row.get("merge_hold", 0)
        )

    def conflict(row):
        basic = basic_conflict(row)
        if not strict_identity:
            return basic
        base = row.get("base_json") or {}
        if isinstance(base, str):
            try:
                base = json.loads(base)
            except (TypeError, ValueError):
                base = {}
        source_name = normalize_clinic_name(record.get("clinic_name"))
        source_address = normalize_address(record.get("address"))
        source_phone = tel_match_key(record.get("phone"))
        target_name = row.get("name_norm") or normalize_clinic_name(base.get("clinic_name"))
        target_address = row.get("address_norm") or normalize_address(base.get("address"))
        target_phone = row.get("tel_match_key") or tel_match_key(base.get("phone"))
        address_conflict = bool(source_address and target_address and not _strict_address_equivalent(
            source_address, target_address, record.get("prefecture", ""), row.get("prefecture", "")
        ))
        return basic or bool(
            source_name and target_name and fuzz.ratio(source_name, target_name) < 95
            or address_conflict
            or source_phone and target_phone and source_phone != target_phone
        )

    def select(pool, reason):
        if len(pool) != 1 or conflict(pool[0]) or any(row["id"] != pool[0]["id"] for row in identity):
            return ambiguous(pool, reason + "・複数候補または識別情報の相違")
        return MasterMatch("MATCHED", [pool[0]["id"]], reason, 100)

    # 1. 空のキー同士は一致扱いにしない。
    if phone:
        phone_rows = rows("tel_match_key=?", (phone,))
        pool = compatible_medical_identity(phone_rows)
        if pool:
            uuid_candidate = (
                len(pool) == 1 and len(identity) == 1
                and pool[0]["id"] == identity[0]["id"] and pool[0].get("uuid") == uid
            )
            strict_exact = [row for row in phone_rows if name and address and row["name_norm"] == name
                            and _strict_address_equivalent(address, row.get("address_norm") or "",
                                                           record.get("prefecture", ""), row.get("prefecture", ""))]
            if strict_identity and not uuid_candidate and (not name or not address
                                                             or len(phone_rows) > 1 and (
                                                                 len(strict_exact) != 1
                                                                 or strict_exact[0]["id"] not in {r["id"] for r in pool}
                                                             )):
                return ambiguous(pool, "電話番号候補あり・住所または医院名の裏付け不足")
            exact = [row for row in pool if name and address and row["name_norm"] == name
                     and _strict_address_equivalent(address, row.get("address_norm") or "",
                                                    record.get("prefecture", ""), row.get("prefecture", ""))]
            selected = exact if len(exact) == 1 else pool
            if len(selected) == 1:
                candidate = selected[0]
                threshold = 95 if strict_identity else 80
                name_differs = bool(name and candidate["name_norm"] and fuzz.ratio(name, candidate["name_norm"]) < threshold)
                address_differs = bool(address and candidate["address_norm"] and (
                    not _strict_address_equivalent(address, candidate["address_norm"], record.get("prefecture", ""),
                                                   candidate.get("prefecture", "")) if strict_identity
                    else fuzz.ratio(address, candidate["address_norm"]) < threshold
                ))
                if not name_differs and not address_differs:
                    return select(selected, "電話番号キー完全一致")

            # Safe normalization rescue: only when the phone identifies exactly
            # one active clinic, the normalized full address (including building,
            # floor, and room) is identical, and the non-generic clinic-name stem
            # is identical after normalizing 医院/クリニック/診療所. This does not
            # treat a shared phone as unique and never changes UUIDs.
            if strict_identity and len(phone_rows) == 1 and len(pool) == 1:
                candidate = pool[0]
                source_name_core = normalize_clinic_name(record.get("clinic_name"), loose=True).replace("診療所", "")
                target_name_core = normalize_clinic_name(candidate.get("name_norm"), loose=True).replace("診療所", "")
                address_equivalent = _strict_address_equivalent(
                    record.get("address"), candidate.get("address_norm") or "",
                    record.get("prefecture", ""), candidate.get("prefecture", ""),
                )
                identity_ids = {row["id"] for row in identity}
                if (
                    source_name_core and len(source_name_core) >= 2
                    and source_name_core == target_name_core
                    and address_equivalent
                    and not basic_conflict(candidate)
                    and (not identity_ids or identity_ids == {candidate["id"]})
                ):
                    return MasterMatch("MATCHED", [candidate["id"]],
                                       "電話・住所完全一致＋医院名の施設種別正規化一致", 100)
            if strict_identity and len(pool) == 1 and address and pool[0].get("address_norm") and (
                not _strict_address_equivalent(address, pool[0]["address_norm"], record.get("prefecture", ""),
                                               pool[0].get("prefecture", ""))
            ):
                return ambiguous(pool, "電話番号候補一致・正規化後住所差異（建物・番地等確認）")
            return ambiguous(pool, "電話番号キー一致・共有電話または医院情報の相違")

    # 2. 電話に候補がない場合、名前と住所の両方が完全一致するかを確認。
    if name and address:
        pool = compatible_medical_identity(rows("name_norm=? AND address_norm=?", (name, address)))
        if pool:
            if strict_identity and phone:
                return ambiguous(pool, "電話番号入力あり・住所＋医院名候補と電話が一致しない")
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
