from pathlib import Path

from src.io.output_writer import csv_bytes
from src.master.comdesk_reconciliation import (
    discover_comdesk_csvs,
    input_rows,
    is_comdesk_auxiliary_export,
    is_comdesk_export,
    mark_input_conflicts,
    uuid_group_report,
)
from src.master.comdesk import infer_comdesk_columns, validate_comdesk_mapping
from src.io.input_loader import load_table


HEADERS = [
    "UUID", "種別", "名前", "カナ", "郵便番号", "都道府県", "住所１", "住所２",
    "住所カナ", "Tel1", "Tel2", "Tel3", "Tel4", "FAX", "URL", "備考", "旧社名",
    "リードソース", "履歴", "記事名", "休診日", "診療日", "午前始", "午前終",
    "午後始", "午後終", "院長名", "開業日", "案件ID", "フィルタープロジェクト名",
]


def _row(uid, name, phone, address, case_id=""):
    row = [""] * len(HEADERS)
    row[0], row[2], row[9], row[6], row[28], row[29] = uid, name, phone, address, case_id, "プロジェクトA"
    return row


def test_exact_file_copy_is_detected_and_counted_once(tmp_path):
    raw = csv_bytes(HEADERS, [_row("UUID-1", "青空医院", "03-1111-2222", "東京都千代田区1-1", "CASE-1")])
    (tmp_path / "project-a.csv").write_bytes(raw)
    (tmp_path / "project-a-copy.csv").write_bytes(raw)
    sources, copies = discover_comdesk_csvs(tmp_path)
    assert len(sources) == 1
    assert len(copies) == 1
    rows = input_rows(sources)
    assert len(rows) == 1
    assert rows[0]["record"]["uuid"] == "UUID-1"
    assert rows[0]["record"]["comdesk_case_id"] == "CASE-1"


def test_blank_leading_csv_row_does_not_hide_a_comdesk_export(tmp_path):
    raw = b"\r\n" + csv_bytes(HEADERS, [_row("UUID-1", "青空医院", "03-1111-2222", "東京都千代田区1-1")])
    (tmp_path / "blank-first-row.csv").write_bytes(raw)
    sources, _ = discover_comdesk_csvs(tmp_path)
    assert len(sources) == 1
    assert sources[0]["header_row_offset"] == 1
    rows = input_rows(sources)
    assert rows[0]["source_row"] == 3


def test_auxiliary_activity_export_is_separated_from_clinic_master_mapping():
    main_headers = [*HEADERS]
    aux_headers = [
        "UUID", "レート", "進捗", "応対者", "最終架電日", "最終架電者", "MyBox", "MyBox担当者",
        "禁止", "コール数", "配布状況", "配布割当ユーザー", "フィルタープロジェクト名", "プロジェクト\t",
        "名前", "住所１", "Tel1", "URL", "履歴", "記事名", "休診日", "診療日", "午前始", "午前終",
        "午後始", "午後終", "院長名", "開業日", "HPランク", "メールアドレス",
    ]
    assert is_comdesk_export(main_headers)
    assert is_comdesk_auxiliary_export(aux_headers)
    assert not is_comdesk_export(aux_headers)
    table = load_table(csv_bytes(aux_headers, [[""] * len(aux_headers)]), "data.csv")
    mapping = infer_comdesk_columns(table)
    assert mapping["uuid"] == 0 and mapping["clinic_name"] == 14 and mapping["phone"] == 16
    assert mapping["address"] == 15 and mapping["comdesk_case_id"] is None
    import pytest
    with pytest.raises(ValueError, match="補助CSV"):
        validate_comdesk_mapping(aux_headers, mapping)


def test_same_source_uuid_with_different_clinic_identity_is_held():
    raw = csv_bytes(HEADERS, [
        _row("UUID-1", "青空医院", "03-1111-2222", "東京都千代田区1-1", "CASE-1"),
        _row("UUID-1", "別医院", "03-9999-8888", "東京都港区9-9", "CASE-2"),
    ])
    table = load_table(raw, "project.csv")
    source = {"path": Path("project.csv"), "raw": raw, "table": table,
              "sha256": "fixture", "rows": 2, "bytes": len(raw), "local_mtime": 0,
              "header_row_offset": 0}
    rows, _, _ = mark_input_conflicts(input_rows([source]))
    assert all("SOURCE_UUID_IDENTITY_CONFLICT:ADDRESS,NAME,PHONE" in row["input_conflict"] for row in rows)


def test_same_uuid_formatted_variants_are_not_held_just_for_repetition():
    first = _row("UUID-1", "医療法人青空会 青空医院", "03-1111-2222", "東京都千代田区一丁目1番1号")
    second = _row("UUID-1", "医療法人青空会　青空医院", "0311112222", "東京都千代田区1-1-1")
    raw = csv_bytes(HEADERS, [first, second])
    table = load_table(raw, "project.csv")
    source = {"path": Path("project.csv"), "raw": raw, "table": table,
              "sha256": "fixture", "rows": 2, "bytes": len(raw), "local_mtime": 0,
              "header_row_offset": 0}
    rows, _, _ = mark_input_conflicts(input_rows([source]))
    assert all(not row["input_conflict"] for row in rows)
    assert uuid_group_report(rows)[0]["classification"] == "CONSISTENT_REPEATED_UUID"
