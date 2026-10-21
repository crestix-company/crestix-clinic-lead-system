import json

import pytest

from src.io.input_loader import load_table
from src.master.comdesk import (
    infer_comdesk_columns,
    record_from_row,
    validate_comdesk_mapping,
)
from src.master.matching import ClinicMatchingIndex, _strict_address_equivalent, match_record
from src.master.samples import sample_records
from src.master.store import ClinicStore
from src.io.output_writer import csv_bytes


def _table(headers, rows):
    return load_table(csv_bytes(headers, rows), "comdesk.csv")


def test_case_id_headers_never_infer_as_clinic_uuid():
    headers = ["案件ID", "名前", "Tel1", "都道府県", "住所１", "住所２"]
    table = _table(headers, [["lead-55", "青空内視鏡クリニック", "03-0000-0001", "東京都", "千代田区丸の内1-1", ""]])
    mapping = infer_comdesk_columns(table)
    assert mapping["uuid"] is None
    assert mapping["comdesk_case_id"] == 0
    record = record_from_row(table.data.iloc[0].tolist(), mapping)
    assert record["uuid"] == ""
    assert record["comdesk_case_id"] == "lead-55"


@pytest.mark.parametrize("header", ["案件ID", "管理ID", "リードID", "lead_id", "id"])
def test_case_id_column_cannot_be_manually_mapped_to_clinic_uuid(header):
    with pytest.raises(ValueError, match="明示的なUUID列"):
        validate_comdesk_mapping([header, "名前"], {"uuid": 0, "clinic_name": 1})


def test_comdesk_uuid_column_cannot_be_manually_mapped_to_case_id():
    with pytest.raises(ValueError, match="Comdesk UUID列を案件ID欄"):
        validate_comdesk_mapping(["Comdesk UUID", "案件ID"], {"uuid": 0, "comdesk_case_id": 0})


def test_two_cases_on_same_clinic_are_preserved_and_reimport_is_idempotent(tmp_path):
    store = ClinicStore(tmp_path / "clinic.sqlite3")
    clinic = sample_records()[0]
    store.import_master([clinic])
    headers = ["案件ID", "名前", "Tel1", "都道府県", "住所１", "住所２"]
    rows = [
        ["lead-A", clinic["clinic_name"], clinic["phone"], clinic["prefecture"], clinic["address"], ""],
        ["lead-B", clinic["clinic_name"], clinic["phone"], clinic["prefecture"], clinic["address"], ""],
    ]
    table = _table(headers, rows)
    first = store.import_comdesk(table)
    second = store.import_comdesk(table)
    assert first["MATCHED"] == 2
    assert second["already_imported"] is True
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM clinics").fetchone()[0] == 1
        originals = conn.execute("SELECT row_json FROM comdesk_original_rows ORDER BY row_number").fetchall()
        source_records = conn.execute("SELECT record_json FROM source_records WHERE source='コムデスク'").fetchall()
        base = json.loads(conn.execute("SELECT base_json FROM clinics").fetchone()[0])
    assert len(originals) == 2
    assert [json.loads(row[0])[0] for row in originals] == ["lead-A", "lead-B"]
    assert {json.loads(row[0])["comdesk_case_id"] for row in source_records} == {"lead-A", "lead-B"}
    assert "comdesk_case_id" not in base
    assert store.query()[0]["uuid"] == clinic.get("uuid", "")


def test_strict_comdesk_match_requires_phone_address_and_name_support(tmp_path):
    store = ClinicStore(tmp_path / "clinic.sqlite3")
    clinic = sample_records()[0]
    store.import_master([clinic])
    with store.connect() as conn:
        phone_only = match_record(conn, {"uuid": "", "phone": clinic["phone"], "address": "", "clinic_name": ""}, strict_identity=True)
        phone_and_name = match_record(conn, {
            "uuid": "", "phone": clinic["phone"], "address": "", "clinic_name": clinic["clinic_name"],
        }, strict_identity=True)
        address_mismatch = match_record(conn, {
            "uuid": "", "phone": clinic["phone"], "address": "東京都別の区9-9",
            "clinic_name": clinic["clinic_name"],
        }, strict_identity=True)
        exact_triple = match_record(conn, {
            "uuid": "", "phone": clinic["phone"], "address": clinic["address"],
            "clinic_name": clinic["clinic_name"],
        }, strict_identity=True)
    assert phone_only.status == "AMBIGUOUS"
    assert phone_and_name.status == "AMBIGUOUS"
    assert address_mismatch.status == "AMBIGUOUS"
    assert exact_triple.status == "MATCHED"


def test_strict_match_accepts_clinic_type_name_variant_only_with_unique_phone_and_exact_address(tmp_path):
    store = ClinicStore(tmp_path / "normalization-rescue.sqlite3")
    clinic = {**sample_records()[0], "clinic_name": "青空医院", "address": "東京都千代田区丸の内一丁目2番3号テストビル4階401号", "uuid": ""}
    store.import_master([clinic])
    with store.connect() as conn:
        result = match_record(conn, {
            "uuid": "", "clinic_name": "青空クリニック", "phone": clinic["phone"],
            "address": "東京都千代田区丸の内1-2-3テストビル4階401号",
        }, strict_identity=True)
    assert result.status == "MATCHED"
    assert len(result.candidates) == 1
    assert "施設種別正規化一致" in result.reason


def test_strict_match_accepts_prefecture_omission_only_when_target_field_verifies_it(tmp_path):
    store = ClinicStore(tmp_path / "verified-prefecture-prefix.sqlite3")
    clinic = {
        **sample_records()[0], "clinic_name": "青空医院", "address": "東京都千代田区丸の内1-2-3テストビル4階401号",
        "prefecture": "東京都", "phone": "03-1234-5678", "uuid": "UUID-PREF",
    }
    store.import_master([clinic])
    with store.connect() as conn:
        omitted_prefix = match_record(conn, {
            "uuid": "UUID-PREF", "clinic_name": "青空医院", "phone": clinic["phone"],
            "address": "千代田区丸の内1-2-3テストビル4階401号",
        }, strict_identity=True)
        wrong_explicit_prefix = match_record(conn, {
            "uuid": "UUID-PREF", "clinic_name": "青空医院", "phone": clinic["phone"],
            "prefecture": "大阪府", "address": "大阪府千代田区丸の内1-2-3テストビル4階401号",
        }, strict_identity=True)
    assert omitted_prefix.status == "MATCHED"
    assert wrong_explicit_prefix.status == "AMBIGUOUS"
    assert not _strict_address_equivalent(
        "千代田区丸の内1-2-3テストビル4階401号",
        "東京都千代田区丸の内1-2-3テストビル4階401号",
        "",
        "大阪府",
    )


def test_strict_normalization_rescue_preserves_shared_phone_and_room_number_holds(tmp_path):
    store = ClinicStore(tmp_path / "normalization-address-safety.sqlite3")
    base = sample_records()[0]
    address = "東京都千代田区丸の内1丁目2番3号テストビル4階401号"
    clinic = {**base, "clinic_name": "青空医院", "address": address, "phone": "03-1234-5678", "uuid": ""}
    store.import_master([clinic])
    with store.connect() as conn:
        room_mismatch = match_record(conn, {
            "uuid": "", "clinic_name": "青空医院", "phone": clinic["phone"],
            "address": "東京都千代田区丸の内1-2-3テストビル4階402号",
        }, strict_identity=True)
    assert room_mismatch.status == "AMBIGUOUS"
    assert "住所差異" in room_mismatch.reason

    store = ClinicStore(tmp_path / "normalization-shared-phone.sqlite3")
    second = {**base, "clinic_id": "SYNTHETIC-2", "clinic_name": "緑医院", "address": address,
              "phone": "03-1234-5678", "uuid": ""}
    store.import_master([clinic, second])
    with store.connect() as conn:
        shared_phone = match_record(conn, {
            "uuid": "", "clinic_name": "青空クリニック", "phone": clinic["phone"],
            "address": clinic["address"],
        }, strict_identity=True)
    assert shared_phone.status == "AMBIGUOUS"
    assert len(shared_phone.candidates) == 2


def test_strict_shared_phone_is_resolved_only_by_unique_exact_name_and_address(tmp_path):
    store = ClinicStore(tmp_path / "shared-phone-exact.sqlite3")
    base = sample_records()[0]
    address = "東京都千代田区丸の内1-2-3テストビル4階401号"
    first = {**base, "clinic_name": "青空医院", "address": address, "phone": "03-1234-5678", "uuid": ""}
    second = {**base, "clinic_id": "SYNTHETIC-2", "clinic_name": "緑医院", "address": address,
              "phone": "03-1234-5678", "uuid": ""}
    store.import_master([first, second])
    with store.connect() as conn:
        result = match_record(conn, {
            "uuid": "", "clinic_name": "青空医院", "phone": first["phone"], "address": address,
        }, strict_identity=True)
    assert result.status == "MATCHED"
    assert len(result.candidates) == 1


def test_strict_normalization_rescue_does_not_override_uuid_or_merge_conflicts(tmp_path):
    store = ClinicStore(tmp_path / "normalization-identity-safety.sqlite3")
    clinic = {**sample_records()[0], "clinic_name": "青空医院", "uuid": "EXISTING-UUID"}
    store.import_master([clinic])
    with store.connect() as conn:
        uuid_conflict = match_record(conn, {
            "uuid": "OTHER-UUID", "clinic_name": "青空クリニック", "phone": clinic["phone"],
            "address": clinic["address"],
        }, strict_identity=True)
        target_id = conn.execute("SELECT id FROM clinics").fetchone()[0]
        conn.execute("UPDATE clinics SET merge_hold=1 WHERE id=?", (target_id,))
        merge_hold = match_record(conn, {
            "uuid": "EXISTING-UUID", "clinic_name": "青空クリニック", "phone": clinic["phone"],
            "address": clinic["address"],
        }, strict_identity=True)
    assert uuid_conflict.status == "AMBIGUOUS"
    assert merge_hold.status == "AMBIGUOUS"


def test_strict_uuid_match_is_sufficient_when_clinic_fields_do_not_conflict(tmp_path):
    store = ClinicStore(tmp_path / "clinic.sqlite3")
    clinic = {**sample_records()[0], "uuid": "UUID-TEST"}
    store.import_master([clinic])
    with store.connect() as conn:
        uuid_only = match_record(conn, {"uuid": clinic["uuid"]}, strict_identity=True)
        uuid_with_name_and_phone = match_record(conn, {
            "uuid": clinic["uuid"], "clinic_name": clinic["clinic_name"], "phone": clinic["phone"],
        }, strict_identity=True)
        uuid_with_name_and_address = match_record(conn, {
            "uuid": clinic["uuid"], "clinic_name": clinic["clinic_name"], "address": clinic["address"],
        }, strict_identity=True)
    assert uuid_only.status == "MATCHED"
    assert uuid_with_name_and_phone.status == "MATCHED"
    assert uuid_with_name_and_address.status == "MATCHED"


def test_strict_uuid_match_with_conflicting_clinic_evidence_is_held(tmp_path):
    store = ClinicStore(tmp_path / "clinic.sqlite3")
    clinic = {**sample_records()[0], "uuid": "UUID-TEST"}
    store.import_master([clinic])
    with store.connect() as conn:
        result = match_record(conn, {
            "uuid": clinic["uuid"], "clinic_name": "別の医院", "phone": clinic["phone"],
            "address": clinic["address"],
        }, strict_identity=True)
    assert result.status == "AMBIGUOUS"


def test_in_memory_full_master_index_uses_same_strict_match_rules(tmp_path):
    store = ClinicStore(tmp_path / "clinic.sqlite3")
    clinic = sample_records()[0]
    store.import_master([clinic])
    record = {
        "uuid": "", "clinic_name": clinic["clinic_name"], "phone": clinic["phone"],
        "address": clinic["address"], "prefecture": clinic["prefecture"], "medical_type": clinic["medical_type"],
    }
    with store.connect() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM clinics WHERE merged_into IS NULL")]
        sql_result = match_record(conn, record, strict_identity=True)
    indexed_result = match_record(None, record, strict_identity=True, candidate_index=ClinicMatchingIndex(rows))
    assert (indexed_result.status, indexed_result.candidates, indexed_result.reason) == (
        sql_result.status, sql_result.candidates, sql_result.reason,
    )


def test_comdesk_import_never_creates_clinic_and_never_auto_assigns_uuid_or_hp(tmp_path):
    store = ClinicStore(tmp_path / "safe-comdesk.sqlite3")
    clinic = {**sample_records()[0], "uuid": "", "hp_rank": "A", "hp_url": "https://official.example/"}
    store.import_master([clinic])
    headers = ["UUID", "名前", "Tel1", "住所１", "URL", "HPランク", "メールアドレス"]
    row = ["COMDESK-1", clinic["clinic_name"], clinic["phone"], clinic["address"],
           "https://portal.example/", "C", "lead@example.test"]
    table = _table(headers, [row])
    first = store.import_comdesk(table)
    second = store.import_comdesk(table)
    assert first["MATCHED"] == 1 and second["already_imported"] is True
    assert len(store.query(__import__("src.master.filters", fromlist=["Filters"]).Filters(
        active_only=False, hp_only=False))) == 1
    saved = store.query(__import__("src.master.filters", fromlist=["Filters"]).Filters(
        active_only=False, hp_only=False))[0]
    assert saved["uuid"] == ""
    assert saved["hp_rank"] == "A" and saved["hp_url"] == "https://official.example/"


def test_uuidless_match_is_separate_from_uuid_assignment_candidate():
    from src.master.comdesk_reconciliation import project_rows

    item = {
        "record": {"uuid": "COMDESK-1"}, "clinic_id": 7, "status": "AUTO_MATCH",
        "source_file": "project.csv", "source_hash": "h", "source_row": 2,
        "project_scope": "P", "project_scope_source": "CSV_PROJECT_COLUMN",
        "reason": "identity", "candidate_clinic_ids": [7], "exact_row_copies": [], "input_conflict": "",
    }
    output = project_rows([item], {7: {"id": 7, "uuid": ""}})[0]
    assert output["Clinic_UUID_binding_status"] == "UUID_ASSIGNMENT_CANDIDATE"
    assert output["Clinic_UUID_after_plan"] == ""
