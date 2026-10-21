"""コムデスクの列対応。元行を変更せず、照合用に分割住所を組み立てる。"""
import re
import unicodedata

from src.io.input_loader import ALIASES, infer_columns


COMDESK_HEADERS = [
    "UUID", "種別", "名前", "カナ", "郵便番号", "都道府県", "住所１", "住所２",
    "住所カナ", "Tel1", "Tel2", "Tel3", "Tel4", "FAX", "URL", "備考", "旧社名",
    "リードソース", "履歴", "記事名", "休診日", "診療日", "午前始", "午前終",
    "午後始", "午後終", "院長名", "開業日",
]

# Formal outbound format from 2026-10-09 onward. Keep COMDESK_HEADERS at the
# legacy 28-column contract because import/provenance history must continue to
# accept and preserve existing Comdesk files byte-for-byte.
COMDESK_EXPORT_HEADERS = [*COMDESK_HEADERS, "HPランク", "メールアドレス"]

EXTRA_ALIASES = {
    # `UUID` is the only legacy Comdesk clinic identifier accepted as the
    # clinics.uuid value. Case/lead IDs are kept in a separate source field.
    "uuid": ["UUID", "Comdesk UUID", "comdesk_uuid"],
    "comdesk_case_id": ["案件ID", "管理ID", "リードID", "lead_id", "case_id", "案件番号", "Comdesk ID", "id"],
    "clinic_name": ["名前"],
    "phone": ["Tel1", "電話番号1"],
    "address": ["住所1", "address1"],
    "address2": ["住所2", "address2", "建物名"],
    "prefecture": ["都道府県", "prefecture"],
    "postal_code": ["郵便番号", "postal_code"],
    "manager_name": ["院長名", "管理者名", "manager_name"],
}
COMDESK_UUID_HEADERS = {"uuid", "comdesk uuid", "comdesk_uuid"}
COMDESK_CASE_ID_HEADERS = {
    "案件id", "管理id", "リードid", "lead_id", "case_id", "案件番号", "comdesk id", "id"
}
PREFECTURES = (
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県",
    "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県",
    "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県", "長崎県",
    "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
)
PREFECTURE = re.compile(r"^(" + "|".join(map(re.escape, PREFECTURES)) + r")")


def _key(value):
    return re.sub(r"[\s_]", "", unicodedata.normalize("NFKC", value)).casefold()


def infer_comdesk_columns(table):
    result = infer_columns(table)
    for field, extra in EXTRA_ALIASES.items():
        names = {_key(x) for x in ALIASES.get(field, []) + extra}
        found = [i for i, name in enumerate(table.headers) if _key(name) in names]
        if found:
            result[field] = found[0] if len(found) == 1 else None
        else:
            result.setdefault(field, None)
    return result


def validate_comdesk_mapping(headers, mapping):
    """Keep Comdesk UUID and case identifiers in their separate source fields."""
    header_keys = {_key(str(header)) for header in headers}
    activity_columns = {
        _key(value) for value in
        ("進捗", "最終架電日", "最終架電者", "コール数", "フィルタープロジェクト名", "プロジェクト")
    }
    if "uuid" in header_keys and len(header_keys & activity_columns) >= 2:
        raise ValueError("架電・プロジェクト補助CSVは医院マスター取込に使用できません。UUIDで別途対応付けてください。")
    uuid_col = mapping.get("uuid")
    case_col = mapping.get("comdesk_case_id")
    if uuid_col is not None:
        if not isinstance(uuid_col, int) or not 0 <= uuid_col < len(headers):
            raise ValueError("Comdesk UUIDの列指定が範囲外です。")
        header = _key(str(headers[uuid_col])).replace("_", " ")
        if header not in {_key(value).replace("_", " ") for value in COMDESK_UUID_HEADERS}:
            raise ValueError("医院UUID欄には明示的なUUID列だけを指定してください。案件ID・管理ID・リードIDは別列です。")
    if case_col is not None:
        if not isinstance(case_col, int) or not 0 <= case_col < len(headers):
            raise ValueError("Comdesk案件IDの列指定が範囲外です。")
        header = _key(str(headers[case_col])).replace("_", " ")
        if header in {_key(value).replace("_", " ") for value in COMDESK_UUID_HEADERS}:
            raise ValueError("Comdesk UUID列を案件ID欄へ割り当てることはできません。")
    if uuid_col is not None and case_col is not None and uuid_col == case_col:
        raise ValueError("Comdesk UUID列と案件ID列に同じ列は指定できません。")
    return mapping


def record_from_row(raw, mapping):
    record = {field: raw[col] for field, col in mapping.items() if col is not None}
    record["uuid"] = record.get("uuid", "").strip()
    record["comdesk_case_id"] = str(record.get("comdesk_case_id", "")).strip()
    if "url" in record:
        record["hp_candidate_url"] = record.pop("url")

    address = record.get("address", "").strip()
    address2 = record.get("address2", "").strip()
    prefecture = record.get("prefecture", "").strip()
    prefix = PREFECTURE.match(address)
    if prefix:
        if prefecture and prefecture != prefix[1]:
            raise ValueError("都道府県の列と住所の都道府県が一致しません。元の行または列対応を確認してください。")
        prefecture = prefix[1]
    elif prefecture and address:
        address = prefecture + address
    if address2 and not address.endswith(address2):
        address = f"{address} {address2}".strip()
    record.update(address=address, prefecture=prefecture)
    return record


def clinic_master_fields(record, *, source=""):
    """Keep external identifiers in provenance, not in the clinic attribute payload."""
    excluded = {"comdesk_case_id"}
    if source == "コムデスク":
        # Comdesk identifiers and research/contact hints stay in provenance. They
        # must not overwrite canonical clinic identity or researched fields.
        excluded.update({"uuid", "hp_candidate_url", "hp_rank", "email", "email_address"})
    return {key: value for key, value in record.items() if key not in excluded}


def new_export_row(data, headers, mapping):
    """新規医院を元の列構成へ配置。未確定UUIDは空欄、確定済み正規UUIDは再利用。"""
    row = [""] * len(headers)
    values = {field: data.get(field, "") for field in ("uuid", "clinic_name", "phone", "address", "prefecture")}
    if mapping.get("prefecture") is not None:
        prefix = PREFECTURE.match(values["address"])
        if prefix:
            values["prefecture"] = prefix[1]
            values["address"] = values["address"][len(prefix[1]):].lstrip()
    for field, value in values.items():
        col = mapping.get(field)
        if col is not None:
            row[col] = value
    return row
