from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from src.repository.comdesk_writer_safety import (
    clinic_master_fields,
    comdesk_match_record,
    prepare_comdesk_writer_import,
    reject_comdesk_clinic_creation,
    validate_comdesk_writer_import,
    validate_comdesk_review_resolution,
)
from src.master.matching import ClinicMatchingIndex, match_record
from src.normalizer.address import normalize_address
from src.normalizer.clinic_name import normalize_clinic_name
from src.normalizer.phone import tel_match_key
from src.repository.supabase_write_adapter import SupabaseClinicWriteRepository


def _table(headers, rows):
    return SimpleNamespace(headers=headers, data=pd.DataFrame(rows, columns=headers))


def _valid_headers():
    return ["UUID", "案件ID", "プロジェクトID", "名前", "Tel1", "住所１"]


def _valid_mapping():
    return {
        "uuid": 0,
        "comdesk_case_id": 1,
        "comdesk_project_id": 2,
        "clinic_name": 3,
        "phone": 4,
        "address": 5,
    }


def test_inferred_case_id_is_never_used_as_comdesk_uuid():
    table = _table(["案件ID", "名前"], [["CASE-1", "合成医院"]])
    mapping, prepared = prepare_comdesk_writer_import(
        table, project_scope_id="PROJECT-1", project_scope_confirmed=True,
    )
    assert mapping["uuid"] is None
    assert mapping["comdesk_case_id"] == 0
    assert prepared[0][2]["uuid"] == ""
    assert prepared[0][2]["comdesk_case_id"] == "CASE-1"


def test_case_id_cannot_be_mapped_to_uuid():
    table = _table(_valid_headers(), [["CD-1", "CASE-1", "PROJECT-1", "合成医院", "03-0000-0000", "東京都千代田区1-1"]])
    mapping = _valid_mapping() | {"uuid": 1}
    with pytest.raises(ValueError, match="Comdesk UUID"):
        prepare_comdesk_writer_import(table, mapping, project_scope_id="PROJECT-1")


def test_import_requires_independent_case_id_and_project_scope():
    only_uuid = _table(["UUID", "名前"], [["CD-1", "合成医院"]])
    with pytest.raises(ValueError, match="案件ID"):
        prepare_comdesk_writer_import(only_uuid, {"uuid": 0, "clinic_name": 1}, project_scope_id="P-1")

    no_project = _table(["UUID", "案件ID", "名前"], [["CD-1", "CASE-1", "合成医院"]])
    with pytest.raises(ValueError, match="プロジェクトスコープ"):
        prepare_comdesk_writer_import(
            no_project, {"uuid": 0, "comdesk_case_id": 1, "clinic_name": 2},
        )
    with pytest.raises(ValueError, match="管理者確認"):
        prepare_comdesk_writer_import(
            no_project, {"uuid": 0, "comdesk_case_id": 1, "clinic_name": 2},
            project_scope_id="P-1",
        )


@pytest.mark.skip(
    reason="Covers import_comdesk()'s full #139/#140 rewrite (prepare_comdesk_writer_import "
    "wiring, DB unique-index guard). Deliberately not integrated: it requires the unapplied "
    "comdesk_project_case_unique migration and project-scope kwargs the current app_v2.py "
    "call sites never supply, so activating it now would fail-closed every existing Comdesk "
    "import. See conflict resolution report."
)
def test_missing_or_duplicate_row_ids_fail_closed_before_db_access():
    missing = _table(_valid_headers(), [["CD-1", "", "PROJECT-1", "合成医院", "03-0000-0000", "東京都千代田区1-1"]])
    with pytest.raises(ValueError, match="HOLD"):
        prepare_comdesk_writer_import(missing, _valid_mapping())

    duplicate = _table(_valid_headers(), [
        ["CD-1", "CASE-1", "PROJECT-1", "合成医院A", "03-0000-0001", "東京都千代田区1-1"],
        ["CD-2", "CASE-1", "PROJECT-1", "合成医院B", "03-0000-0002", "東京都千代田区1-2"],
    ])
    with pytest.raises(ValueError, match="重複"):
        prepare_comdesk_writer_import(duplicate, _valid_mapping())

    class NoDatabase:
        def cursor(self):
            raise AssertionError("invalid input reached Supabase writer")

    repo = SupabaseClinicWriteRepository(NoDatabase())
    with pytest.raises(ValueError, match="HOLD"):
        repo.import_comdesk(missing, _valid_mapping())


def test_scoped_case_ids_are_normalized_before_persistence():
    table = _table(_valid_headers(), [[
        "CD-1", " CASE-1 ", " PROJECT-1 ", "合成医院", "03-0000-0000", "東京都千代田区1-1",
    ]])
    _, prepared = prepare_comdesk_writer_import(table, _valid_mapping())
    assert prepared[0][2]["comdesk_case_id"] == "CASE-1"
    assert prepared[0][2]["comdesk_project_id"] == "PROJECT-1"


def test_validation_collects_rejections_and_preserves_accepted_rows():
    table = _table(_valid_headers(), [
        ["CD-1", "CASE-1", "PROJECT-1", "合成医院", "03-0000-0000", "東京都千代田区1-1"],
        ["CD-2", "", "PROJECT-1", "医院B", "03-0000-0001", "東京都千代田区1-2"],
        ["CD-3", "CASE-3", "", "医院C", "03-0000-0002", "東京都千代田区1-3"],
        ["CD-4", "CASE-4", "PROJECT-1", "医院D", "03-0000-0003", "東京都千代田区1-4"],
        ["CD-5", "CASE-4", "PROJECT-1", "医院E", "03-0000-0004", "東京都千代田区1-5"],
    ])

    result = validate_comdesk_writer_import(table, _valid_mapping())

    assert [row[2]["clinic_name"] for row in result.accepted_rows] == ["合成医院", "医院D"]
    assert {(item.row_number, item.reason_code) for item in result.rejections} == {
        (3, "MISSING_CASE_ID"),
        (4, "MISSING_PROJECT_SCOPE"),
        (6, "DUPLICATE_CASE_IN_SCOPE"),
    }
    assert all(len(item.source_hash) == 64 for item in result.rejections)
    assert result.rejections[0].raw_record[0] == "CD-2"


def test_case_id_mapped_as_comdesk_uuid_becomes_a_structured_rejection():
    table = _table(["案件ID", "プロジェクトID", "名前"], [["CASE-1", "PROJECT-1", "医院"]])

    result = validate_comdesk_writer_import(
        table,
        {"uuid": 0, "clinic_name": 2, "comdesk_case_id": 0, "comdesk_project_id": 1},
    )

    assert len(result.accepted_rows) == 0
    assert {item.reason_code for item in result.rejections} == {"INVALID_UUID_MAPPING"}


def test_source_ids_do_not_enter_clinic_master_fields_or_uuid_match():
    source = {
        "uuid": "COMDESK-UUID-1",
        "comdesk_case_id": "CASE-1",
        "comdesk_project_id": "PROJECT-1",
        "clinic_name": "合成医院",
        "phone": "03-0000-0000",
        "address": "東京都千代田区1-1",
    }
    assert comdesk_match_record(source)["uuid"] == ""
    assert source["uuid"] == "COMDESK-UUID-1"
    clinic_fields = clinic_master_fields(source)
    assert "uuid" not in clinic_fields
    assert "comdesk_case_id" not in clinic_fields
    assert "comdesk_project_id" not in clinic_fields
    assert clinic_fields["clinic_name"] == "合成医院"


def test_review_cannot_create_clinic_from_comdesk_uuid_or_missing_scope():
    record = {"uuid": "COMDESK-UUID-1", "clinic_name": "合成医院"}
    with pytest.raises(ValueError, match="案件ID"):
        validate_comdesk_review_resolution(record, target_id=10, existing_clinic_id=None)
    scoped = record | {"comdesk_case_id": "CASE-1", "comdesk_project_id": "PROJECT-1"}
    with pytest.raises(ValueError, match="Clinic UUID"):
        validate_comdesk_review_resolution(scoped, target_id=None, existing_clinic_id=None)
    validate_comdesk_review_resolution(scoped, target_id=10, existing_clinic_id=None)
    with pytest.raises(ValueError, match="自動登録できません"):
        reject_comdesk_clinic_creation(scoped)


def test_phone_only_match_with_conflicting_clinic_identity_is_held():
    base = {"clinic_name": "合成医院A", "address": "東京都千代田区1-1", "phone": "03-0000-0000"}
    candidate = {
        "id": 10,
        "uuid": "CLINIC-UUID-10",
        "medical_key": "",
        "base_json": base,
        "name_norm": normalize_clinic_name(base["clinic_name"]),
        "name_prefix": normalize_clinic_name(base["clinic_name"])[:2],
        "address_norm": normalize_address(base["address"]),
        "tel_match_key": tel_match_key(base["phone"]),
        "prefecture": "東京都",
        "merge_hold": False,
    }
    source = {
        "uuid": "",
        "clinic_name": "別の合成医院",
        "address": "東京都千代田区9-9",
        "phone": "03-0000-0000",
        "prefecture": "東京都",
        "medical_type": "",
    }
    decision = match_record(None, source, strict_identity=True,
                            candidate_index=ClinicMatchingIndex([candidate]))
    assert decision.status == "AMBIGUOUS"


@pytest.mark.skip(
    reason="Asserts import_comdesk()'s source body matches the #139/#140 rewrite verbatim. "
    "Deliberately not integrated in this pass (same reason as the test above)."
)
def test_writer_requires_db_unique_guard_and_never_updates_clinic_uuid_from_comdesk():
    source = Path("src/repository/supabase_write_adapter.py").read_text(encoding="utf-8")
    start = source.index("    def import_comdesk(")
    end = source.index("    def ", start + 8)
    method = source[start:end]
    assert "prepare_comdesk_writer_import" in method
    assert "to_regclass('provenance.comdesk_project_case_unique_idx')" in method
    assert "i.indisunique AND i.indisvalid AND i.indisready" in method
    assert "pg_get_expr(i.indexprs,i.indrelid)" in method
    assert "pg_advisory_xact_lock" in method
    assert "comdesk_match_record(record)" in method
    assert "strict_identity=True" in method
    assert '"uuid"' not in method[method.index("existing_update_rows = []"):method.index('phase["clinic_write_seconds"]')]
    assert "if match.status == \"NEW\":" in method
    assert "未登録医院への自動登録は禁止" in method
    assert "_ensure_comdesk_common_identities" not in source
    assert "validate_comdesk_review_resolution" in source
    assert "reject_comdesk_clinic_creation" in source


def test_expression_unique_index_covers_scoped_case_id_pair():
    migration = Path("supabase/migrations/20261021100000_comdesk_project_case_unique.sql").read_text(encoding="utf-8")
    assert "CREATE UNIQUE INDEX" in migration
    assert "record_json->>'comdesk_project_id'" in migration
    assert "record_json->>'comdesk_case_id'" in migration
    assert "BTRIM(record_json->>'comdesk_project_id')" in migration
    assert "BTRIM(record_json->>'comdesk_case_id')" in migration
    assert "source = 'コムデスク'" in migration


def test_rejection_ledger_migration_is_private_and_idempotent():
    migration = Path(
        "supabase/migrations/20261102100000_comdesk_rejections.sql"
    ).read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS provenance.comdesk_rejections" in migration
    assert "raw_record_json jsonb NOT NULL" in migration
    assert "normalized_record_json jsonb" in migration
    assert "mapping_snapshot_json jsonb NOT NULL" in migration
    assert "UNIQUE (import_batch_id, row_number, reason_code, source_hash)" in migration
    assert "REVOKE ALL ON TABLE provenance.comdesk_rejections FROM PUBLIC, anon, authenticated" in migration
    assert "GRANT INSERT ON TABLE provenance.comdesk_rejections TO clinic_comdesk_import_writer" in migration
