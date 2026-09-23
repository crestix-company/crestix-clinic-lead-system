"""営業対象を毎回コムデスクのA〜AB列へ出力する。保存済み元行は変更しない。"""
import json

import pandas as pd

from src.io.output_writer import csv_bytes, xlsx_bytes
from src.master.comdesk import COMDESK_HEADERS, PREFECTURE, _key
from src.master.filters import where


OUTPUT_FIELDS = {
    "uuid": "UUID", "clinic_name": "名前", "phone": "Tel1",
    "prefecture": "都道府県", "address": "住所１", "address2": "住所２",
    "postal_code": "郵便番号", "manager_name": "院長名", "url": "URL",
}


def fixed_row(data, headers=(), mapping=None, raw=None):
    """既存の値は空欄を含めて保持し、入力にない項目だけ確認済み情報で補完。"""
    values = {heading: "" for heading in COMDESK_HEADERS}
    for field, heading in OUTPUT_FIELDS.items():
        if field not in {"url", "uuid"}:
            values[heading] = data.get(field, "") or ""
    values["UUID"] = data.get("uuid", "") or ""
    maps_url = (data.get("maps_website_url", "") or "") if data.get("maps_presence_status") == "MAPS_MATCHED_WEBSITE" else ""
    values["URL"] = maps_url or ((data.get("hp_url", "") or "") if data.get("hp_status") == "VERIFIED" else "")
    # 新規（Comdesk元行なし）の医院は、Mapsで取得できた診療情報をComdesk列へ補完する。
    values["休診日"] = data.get("maps_regular_holiday", "") or data.get("regular_holiday", "") or ""
    values["診療日"] = data.get("maps_business_days", "") or data.get("business_days", "") or ""
    values["午前始"] = data.get("maps_morning_start", "") or data.get("morning_start", "") or ""
    values["午前終"] = data.get("maps_morning_end", "") or data.get("morning_end", "") or ""
    values["午後始"] = data.get("maps_afternoon_start", "") or data.get("afternoon_start", "") or ""
    values["午後終"] = data.get("maps_afternoon_end", "") or data.get("afternoon_end", "") or ""
    address = values["住所１"]
    prefix = PREFECTURE.match(address)
    if prefix:
        values["都道府県"] = prefix[1]
        values["住所１"] = address[len(prefix[1]):].lstrip()
    address2 = values["住所２"]
    if address2 and values["住所１"].endswith(address2):
        values["住所１"] = values["住所１"][:-len(address2)].rstrip()

    if raw is not None:
        if len(raw) != len(headers):
            raise ValueError("保存済み元行の列数と見出しが一致しません。")
        columns = {}
        for index, heading in enumerate(headers):
            columns.setdefault(_key(heading), []).append(index)
        selected = {}
        for heading in COMDESK_HEADERS:
            candidates = columns.get(_key(heading), [])
            if candidates:
                if len({raw[index] for index in candidates}) > 1:
                    raise ValueError(f"「{heading}」の列が複数あり値が異なります。元ファイルの見出しを確認してください。")
                selected[heading] = candidates[0]
        for field, heading in OUTPUT_FIELDS.items():
            index = (mapping or {}).get(field)
            if index is not None:
                if not isinstance(index, int) or not 0 <= index < len(raw):
                    raise ValueError("保存済みの列対応を確認してください。")
                selected[heading] = index
        for heading, index in selected.items():
            values[heading] = raw[index]
        # 既存Comdesk行は原文を保持。URLが空欄のときだけ、本人確認済みMaps websiteで補完する。
        if not str(values.get("URL", "") or "").strip() and maps_url:
            values["URL"] = maps_url
    return [values[heading] for heading in COMDESK_HEADERS]


def export_fixed(store, filters, as_of=None):
    sql, args = where(filters, as_of)
    rows = []
    with store.connect() as connection:
        # 一回の出力で件数・元行・調査結果を同じ時点から読む。
        connection.execute("BEGIN")
        templates = {
            row["id"]: (json.loads(row["headers_json"]), json.loads(row["mapping_json"]))
            for row in connection.execute("SELECT * FROM templates")
        }
        records = connection.execute("SELECT id FROM clinics WHERE " + sql + " ORDER BY id", args).fetchall()
        for item in records:
            data = store._get(connection, item["id"])
            name = str(data.get("clinic_name", "") or "")
            facility = str(data.get("facility_type", "") or "")
            if data.get("exclude_reason") in {"hospital","center"} or facility == "病院" or "病院" in name or "センター" in name:
                continue
            originals = connection.execute(
                "SELECT * FROM comdesk_original_rows WHERE clinic_id=? ORDER BY (uuid<>'') DESC,id",
                (data["id"],),
            ).fetchall()
            if originals:
                original = next((row for row in originals if not data["uuid"] or row["uuid"] == data["uuid"]), None)
                if original is None:
                    raise ValueError("既存UUIDに対応する元行を確認できません。元データを確認してください。")
                headers, mapping = templates[original["template_id"]]
                rows.append(fixed_row(data, headers, mapping, json.loads(original["row_json"])))
            else:
                rows.append(fixed_row(data))
    frame = pd.DataFrame(rows, columns=COMDESK_HEADERS)
    return {
        "final_comdesk_import.xlsx": xlsx_bytes({"営業対象": frame}),
        "final_comdesk_import.csv": csv_bytes(COMDESK_HEADERS, rows),
    }
