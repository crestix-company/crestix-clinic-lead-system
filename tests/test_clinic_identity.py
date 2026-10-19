import pytest

from src.master.clinic_identity import (
    project_export_rows,
    resolve_clinic_identity,
    validate_entity_uuid_assignments,
)


def clinic(i, **values):
    return {
        "id": i, "common_clinic_id": None, "uuid": "", "medical_code": "",
        "name": "さくら内科", "address": "東京都千代田区1-1", "phone": "03-1234-5678",
        "medical_type": "医科", **values,
    }


def test_same_uuid_resolves_to_existing_common_clinic_id():
    target = clinic(2, common_clinic_id=77, uuid="comdesk-1")
    result = resolve_clinic_identity(clinic(3, uuid="comdesk-1"), [target])
    assert (result.status, result.clinic_id, result.common_clinic_id) == ("MATCH", 2, 77)
    assert result.preserved_comdesk_uuid == "comdesk-1"


def test_conflicting_uuid_for_same_entity_is_held():
    target = clinic(2, common_clinic_id=77, uuid="existing")
    result = resolve_clinic_identity(clinic(3, common_clinic_id=77, uuid="different"), [target])
    assert result.status == "HOLD"
    assert "異なるUUID" in result.reason
    with pytest.raises(ValueError, match="multiple Comdesk UUIDs"):
        validate_entity_uuid_assignments([
            {"common_clinic_id": 77, "uuid": "existing"},
            {"common_clinic_id": 77, "uuid": "different"},
        ])


def test_medical_and_dental_source_rows_are_not_collapsed_or_mutated():
    medical = clinic(2, medical_type="医科", medical_code="A-1")
    dental = clinic(3, medical_type="歯科", medical_code="B-2")
    before = [medical.copy(), dental.copy()]
    result = resolve_clinic_identity(clinic(4, medical_type="医科", medical_code="A-1"), [medical, dental])
    assert result.clinic_id == 2
    assert [medical, dental] == before
    assert {row["medical_type"] for row in (medical, dental)} == {"医科", "歯科"}


def test_unassigned_uuid_is_valid_and_never_generated():
    result = resolve_clinic_identity(clinic(1, name="新規医院", address="別住所"), [])
    assert result.status == "NEW"
    assert result.preserved_comdesk_uuid == ""


def test_same_csv_reimport_is_idempotent_at_existing_import_batch_boundary(tmp_path):
    from src.master.store import ClinicStore
    from src.master.comdesk import COMDESK_HEADERS
    from src.io.input_loader import load_table
    from src.io.output_writer import csv_bytes

    store = ClinicStore(tmp_path / "identity-reimport.sqlite3")
    raw = [""] * len(COMDESK_HEADERS)
    raw[COMDESK_HEADERS.index("名前")] = "再取込医院"
    raw[COMDESK_HEADERS.index("Tel1")] = "03-1234-5678"
    table = load_table(csv_bytes(COMDESK_HEADERS, [raw]), "same.csv")
    first = store.import_comdesk(table)
    second = store.import_comdesk(table)
    assert first["NEW"] == 1
    assert second["already_imported"] is True
    assert len(store.query(__import__("src.master.filters", fromlist=["Filters"]).Filters(
        active_only=False, hp_only=False))) == 1


def test_same_uuid_is_reused_in_separate_project_exports_and_each_project_dedupes():
    rows = [
        clinic(1, common_clinic_id=77, uuid="shared-uuid"),
        clinic(2, common_clinic_id=77, uuid="shared-uuid", medical_type="歯科"),
    ]
    internal = project_export_rows(rows, "内科")
    endoscopy = project_export_rows(rows, "内視鏡")
    assert len(internal) == len(endoscopy) == 1
    assert internal[0]["uuid"] == endoscopy[0]["uuid"] == "shared-uuid"


def test_different_clinics_are_not_merged_by_name_only():
    source = clinic(3, name="さくら内科", address="東京都千代田区2-2", phone="03-9999-0000")
    candidate = clinic(2, name="さくら内科", address="東京都千代田区1-1", phone="03-1234-5678")
    result = resolve_clinic_identity(source, [candidate])
    assert result.status == "NEW"
    assert result.clinic_id is None


def test_conflicting_same_entity_uuid_fails_project_export_closed():
    with pytest.raises(ValueError, match="conflicting Comdesk UUIDs"):
        project_export_rows([
            clinic(1, common_clinic_id=77, uuid="A"),
            clinic(2, common_clinic_id=77, uuid="B"),
        ], "内科")
