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

EXTRA_ALIASES = {
    "uuid": ["UUID", "案件ID", "管理ID", "リードID", "lead_id", "id"],
    "clinic_name": ["名前"],
    "phone": ["Tel1", "電話番号1"],
    "address": ["住所1", "address1"],
    "address2": ["住所2", "address2", "建物名"],
    "prefecture": ["都道府県", "prefecture"],
    "postal_code": ["郵便番号", "postal_code"],
    "manager_name": ["院長名", "管理者名", "manager_name"],
}
PREFECTURE = re.compile(r"^(東京都|北海道|(?:京都|大阪)府|.{2,3}県)")


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


def record_from_row(raw, mapping):
    record = {field: raw[col] for field, col in mapping.items() if col is not None}
    record["uuid"] = record.get("uuid", "").strip()
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


def new_export_row(data, headers, mapping):
    """新規医院だけを元の列構成へ配置。確定できない列とUUIDは空欄。"""
    row = [""] * len(headers)
    values = {field: data.get(field, "") for field in ("clinic_name", "phone", "address", "prefecture")}
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
