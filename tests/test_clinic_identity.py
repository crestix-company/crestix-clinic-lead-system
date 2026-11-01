import pytest

from src.master.clinic_identity import (
    common_identity_enabled,
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


def test_common_identity_feature_is_opt_in():
    assert common_identity_enabled({}) is False
    assert common_identity_enabled({"CLINIC_COMMON_IDENTITY_ENABLED": "0"}) is False
    assert common_identity_enabled({"CLINIC_COMMON_IDENTITY_ENABLED": "true"}) is True


def test_schema_probe_is_skipped_when_feature_is_disabled(monkeypatch):
    from src.repository.supabase_write_adapter import SupabaseClinicWriteRepository

    class NoQueryCursor:
        def execute(self, *_args):
            raise AssertionError("disabled identity feature must not query identity tables")

    monkeypatch.delenv("CLINIC_COMMON_IDENTITY_ENABLED", raising=False)
    assert SupabaseClinicWriteRepository._common_identity_schema_available(NoQueryCursor()) is False


def test_schema_probe_falls_back_when_feature_enabled_but_migration_is_absent(monkeypatch):
    from src.repository.supabase_write_adapter import SupabaseClinicWriteRepository

    class MissingSchemaCursor:
        executed = False

        def execute(self, query):
            self.executed = True
            assert "to_regclass" in query

        def fetchone(self):
            return (None, None, None)

    cursor = MissingSchemaCursor()
    monkeypatch.setenv("CLINIC_COMMON_IDENTITY_ENABLED", "1")
    assert SupabaseClinicWriteRepository._common_identity_schema_available(cursor) is False
    assert cursor.executed is True


def test_runtime_export_skips_identity_lookup_when_feature_is_disabled(monkeypatch):
    from src.repository.runtime_store import SupabaseRuntimeStore

    class NoConnection:
        def cursor(self):
            raise AssertionError("disabled identity feature must not query identity tables")

    monkeypatch.delenv("CLINIC_COMMON_IDENTITY_ENABLED", raising=False)
    store = object.__new__(SupabaseRuntimeStore)
    store._owns_runtime_connection = False
    store._conn = NoConnection()
    assert store.comdesk_identity_for_clinics([1]) == {}


def test_same_uuid_resolves_to_existing_common_clinic_id():
    target = clinic(2, common_clinic_id=77, canonical_comdesk_uuid="comdesk-1")
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


def test_medical_and_dental_different_codes_can_share_verified_sales_entity():
    medical = clinic(2, common_clinic_id=77, medical_type="医科", medical_code="A-1")
    result = resolve_clinic_identity(
        clinic(4, medical_type="歯科", medical_code="B-2"), [medical]
    )
    assert result.status == "MATCH"
    assert result.clinic_id == 2
    assert result.common_clinic_id == 77


def test_uncertain_different_medical_codes_are_held():
    candidate = clinic(2, medical_type="医科", medical_code="A-1")
    result = resolve_clinic_identity(
        clinic(3, medical_type="医科", medical_code="A-2"), [candidate]
    )
    assert result.status == "HOLD"


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
        clinic(1, common_clinic_id=77, uuid="", medical_type="医科"),
        clinic(2, common_clinic_id=77, uuid="", medical_type="歯科"),
    ]
    canonical = {77: "shared-uuid"}
    internal = project_export_rows(rows, "内科", canonical)
    endoscopy = project_export_rows(rows, "内視鏡", canonical)
    assert len(internal) == len(endoscopy) == 1
    assert internal[0]["uuid"] == endoscopy[0]["uuid"] == "shared-uuid"


def test_comdesk_30_column_csv_uses_same_canonical_uuid_across_projects():
    from app_v2 import comdesk_sync_csv
    from src.io.input_loader import load_table
    from src.master.comdesk import COMDESK_EXPORT_HEADERS

    class Store:
        def comdesk_identity_for_clinics(self, clinic_ids):
            assert set(clinic_ids) == {11, 12}
            return {
                11: {"common_clinic_id": 901, "canonical_comdesk_uuid": "shared-uuid"},
                12: {"common_clinic_id": 901, "canonical_comdesk_uuid": "shared-uuid"},
            }

    rows = [
        {"id": 11, "uuid": "", "clinic_name": "さくら内科", "phone": "03-1234-5678", "address": "東京都千代田区1-1", "prefecture": "東京都", "effective_hp_rank": "A"},
        {"id": 12, "uuid": "", "clinic_name": "さくら歯科", "phone": "03-1234-5678", "address": "東京都千代田区1-1", "prefecture": "東京都", "effective_hp_rank": "A"},
    ]
    store = Store()
    internal = load_table(comdesk_sync_csv(store, rows, project_key="内科"), "internal.csv")
    endoscopy = load_table(comdesk_sync_csv(store, rows, project_key="内視鏡"), "endoscopy.csv")
    assert internal.headers == endoscopy.headers == COMDESK_EXPORT_HEADERS
    assert len(internal.headers) == len(endoscopy.headers) == 30
    assert len(internal.data) == len(endoscopy.data) == 1
    assert internal.data.iloc[0, 0] == endoscopy.data.iloc[0, 0] == "shared-uuid"


def test_common_id_number_equal_to_legacy_clinic_id_does_not_merge():
    rows = [
        clinic(7, common_clinic_id=7, uuid="canonical-a"),
        clinic(7, common_clinic_id=None, uuid="legacy-b", name="別医院"),
    ]
    output = project_export_rows(rows, "内科", {7: "canonical-a"})
    assert len(output) == 2
    assert {row["uuid"] for row in output} == {"canonical-a", "legacy-b"}


def test_canonical_uuid_conflict_is_held_without_replacing_clinic_uuid():
    row = clinic(7, common_clinic_id=7, uuid="existing-clinic-uuid")
    with pytest.raises(ValueError, match="conflicts with canonical"):
        project_export_rows([row], "内科", {7: "different-canonical-uuid"})


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


def test_comdesk_import_resolves_unlinked_row_by_canonical_uuid_without_copying_it():
    from src.repository.supabase_write_adapter import SupabaseClinicWriteRepository

    candidate = clinic(
        55,
        common_clinic_id=901,
        uuid="",
        canonical_comdesk_uuid="shared-uuid",
        medical_key="",
        base_json={"clinic_name": "さくら内科", "address": "東京都千代田区1-1", "phone": "03-1234-5678"},
        medical_type="医科",
    )
    record = {"uuid": "shared-uuid", "clinic_name": "さくら内科", "address": "東京都千代田区1-1", "phone": "03-1234-5678"}
    match, canonical_uuid_match = SupabaseClinicWriteRepository._match_common_identity(record, [candidate])
    assert match.status == "MATCHED"
    assert match.candidates == [55]
    assert canonical_uuid_match is True
    assert candidate["uuid"] == ""


def test_comdesk_import_holds_different_uuid_for_same_canonical_entity():
    from src.repository.supabase_write_adapter import SupabaseClinicWriteRepository

    candidate = clinic(
        55,
        common_clinic_id=901,
        uuid="",
        canonical_comdesk_uuid="existing-uuid",
        medical_key="",
        base_json={"clinic_name": "さくら内科", "address": "東京都千代田区1-1", "phone": "03-1234-5678"},
        medical_type="医科",
    )
    record = {"uuid": "different-uuid", "clinic_name": "さくら内科", "address": "東京都千代田区1-1", "phone": "03-1234-5678"}
    match, _ = SupabaseClinicWriteRepository._match_common_identity(record, [candidate])
    assert match.status == "AMBIGUOUS"
    assert "UUID" in match.reason


def test_comdesk_sync_apply_skips_dropped_sales_entity_tables_when_schema_unavailable(monkeypatch):
    """apply_comdesk_sync's identity re-check must not unconditionally query
    public.sales_entity_comdesk_identity/sales_entity_clinics: those tables were
    intentionally dropped on 2026-10-31 (remove_unused_future_public_tables_and_
    cloudrun_readonly_view_20261031) and CLINIC_COMMON_IDENTITY_ENABLED is 0 in
    production. Querying them unconditionally would raise
    "relation ... does not exist" on every Step7 apply."""
    from src.repository.supabase_write_adapter import SupabaseClinicWriteRepository

    class OriginalRowsOnlyCursor:
        def __init__(self):
            self.queries = []

        def execute(self, query, params=()):
            self.queries.append(query)
            assert "sales_entity" not in query

        def fetchone(self):
            return (1,)

    monkeypatch.delenv("CLINIC_COMMON_IDENTITY_ENABLED", raising=False)
    cursor = OriginalRowsOnlyCursor()
    result = SupabaseClinicWriteRepository._comdesk_identity_match_exists(cursor, "comdesk-uuid-1", 42)
    assert result == (1,)
    assert len(cursor.queries) == 1
    assert "provenance.comdesk_original_rows" in cursor.queries[0]


def test_comdesk_sync_apply_also_checks_canonical_identity_when_schema_available(monkeypatch):
    from src.repository.supabase_write_adapter import SupabaseClinicWriteRepository

    class FullSchemaCursor:
        def __init__(self):
            self.queries = []

        def execute(self, query, params=()):
            self.queries.append(query)
            if "to_regclass" in query:
                self._regclass_query = True

        def fetchone(self):
            if getattr(self, "_regclass_query", False):
                self._regclass_query = False
                return ("public.sales_entity_clinics", "public.sales_entities", "public.sales_entity_comdesk_identity")
            return (1,)

    monkeypatch.setenv("CLINIC_COMMON_IDENTITY_ENABLED", "1")
    cursor = FullSchemaCursor()
    result = SupabaseClinicWriteRepository._comdesk_identity_match_exists(cursor, "comdesk-uuid-1", 42)
    assert result == (1,)
    assert any("sales_entity_comdesk_identity" in q for q in cursor.queries)
