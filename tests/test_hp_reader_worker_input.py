from src.repository.runtime_store import SupabaseRuntimeStore
from src.repository.supabase_adapter import SupabaseClinicRepository


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.conn.calls.append((" ".join(sql.split()), tuple(params)))

    def fetchone(self):
        return self.conn.row


class _Connection:
    def __init__(self, row):
        self.row = row
        self.calls = []

    def cursor(self):
        return _Cursor(self)


def test_hp_worker_input_uses_minimal_reader_projection_and_identity_fields():
    conn = _Connection((42, "医院A", "03-1234-5678", "東京都千代田区1-2-3", "東京都",
                        "医科", "https://clinic.example/", "MAPS_MATCHED_WEBSITE", ["内科", "小児科"]))
    clinic_repo = SupabaseClinicRepository(conn, hp_repository=None)
    store = SupabaseRuntimeStore(type("Repositories", (), {"clinics": clinic_repo})())

    record = store.get_hp_research_clinic(42)

    assert record == {
        "id": 42, "clinic_name": "医院A", "phone": "03-1234-5678", "address": "東京都千代田区1-2-3",
        "prefecture": "東京都", "medical_type": "医科", "maps_website_url": "https://clinic.example/",
        "maps_presence_status": "MAPS_MATCHED_WEBSITE", "departments": "内科 / 小児科",
    }
    sql, params = conn.calls[0]
    assert "FROM public.clinic_cloudrun_readonly WHERE id=%s" in sql
    assert "public.clinics" not in sql
    assert all(column in sql for column in ("clinic_name", "phone", "address", "maps_website_url", "departments_json"))
    assert params == (42,)


def test_hp_worker_input_fails_closed_when_projection_row_is_missing():
    import pytest

    conn = _Connection(None)
    clinic_repo = SupabaseClinicRepository(conn, hp_repository=None)
    with pytest.raises(ValueError, match="医院が見つかりません"):
        clinic_repo.get_hp_research_clinic(999)
