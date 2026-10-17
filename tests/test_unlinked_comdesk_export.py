from app_v2 import unlinked_comdesk_candidates
from src.master.comdesk import COMDESK_EXPORT_HEADERS, COMDESK_HEADERS, new_export_row
from src.io.input_loader import load_table
from src.io.output_writer import csv_bytes
from src.master.filters import Filters
from src.master.store import ClinicStore
from src.repository.supabase_filters import raise_if_unsupported


class _LargeClinicStore:
    def __init__(self, total):
        self.total = total
        self.calls = []

    def count(self, filters):
        assert filters.sales_tiers == []
        assert filters.effective_ranks == ["A", "B"]
        assert filters.active_only is True
        assert filters.hp_only is False
        raise_if_unsupported(filters)
        return self.total

    def query(self, filters, *, limit, offset):
        self.calls.append((limit, offset))
        end = min(self.total, offset + limit)
        return [
            {
                "id": clinic_id,
                "uuid": "UUID-4" if clinic_id == 4 else "",
                "clinic_name": f"医院{clinic_id}",
                "merge_hold": clinic_id == 5,
            }
            for clinic_id in range(offset + 1, end + 1)
        ]

    def comdesk_linkage_state(self):
        return {"imported_clinic_ids": {2}, "held_clinic_ids": {3}}


def test_unlinked_candidates_page_through_full_clinic_master_and_exclude_linkage_states():
    store = _LargeClinicStore(164_199)
    candidates = unlinked_comdesk_candidates(store)

    assert store.calls == [(50_000, 0), (50_000, 50_000), (50_000, 100_000), (50_000, 150_000)]
    assert len(candidates) == 164_195
    assert [item["id"] for item in candidates[:3]] == [1, 6, 7]
    assert candidates[-1]["id"] == 164_199


def test_unlinked_export_row_keeps_formal_thirty_column_contract():
    row = new_export_row(
        {"clinic_name": "対象医院", "phone": "03-1234-5678", "prefecture": "東京都", "address": "東京都千代田区1-1"},
        COMDESK_HEADERS,
        {"clinic_name": 2, "phone": 9, "prefecture": 5, "address": 6},
    )
    output = [*row, "A", "clinic@example.jp"]

    assert len(output) == len(COMDESK_EXPORT_HEADERS) == 30
    assert output[COMDESK_EXPORT_HEADERS.index("HPランク")] == "A"
    assert output[COMDESK_EXPORT_HEADERS.index("メールアドレス")] == "clinic@example.jp"


def test_uuid_added_after_initial_comdesk_import_reimport_updates_same_clinic(tmp_path):
    store = ClinicStore(tmp_path / "comdesk-reimport.db")
    initial = [""] * len(COMDESK_HEADERS)
    initial[COMDESK_HEADERS.index("名前")] = "再取込テスト医院"
    initial[COMDESK_HEADERS.index("Tel1")] = "03-1234-5678"
    initial[COMDESK_HEADERS.index("都道府県")] = "東京都"
    initial[COMDESK_HEADERS.index("住所１")] = "千代田区1-1"
    first = load_table(csv_bytes(COMDESK_HEADERS, [initial]), "initial.csv")

    assert store.import_comdesk(first)["NEW"] == 1
    original_id = store.query(Filters(active_only=False, hp_only=False))[0]["id"]

    with_uuid = initial.copy()
    with_uuid[COMDESK_HEADERS.index("UUID")] = "COMDESK-UUID-1"
    second = load_table(csv_bytes(COMDESK_HEADERS, [with_uuid]), "reimport.csv")
    result = store.import_comdesk(second)
    repeated = store.import_comdesk(second)

    clinics = store.query(Filters(active_only=False, hp_only=False))
    assert result["MATCHED"] == 1
    assert repeated["already_imported"] is True
    assert len(clinics) == 1
    assert clinics[0]["id"] == original_id
    assert clinics[0]["uuid"] == "COMDESK-UUID-1"
    with store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM source_records WHERE source='コムデスク'").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM comdesk_original_rows").fetchone()[0] == 2
    assert store.comdesk_linkage_state()["imported_clinic_ids"] == {original_id}


def test_comdesk_linkage_state_includes_pending_review_candidates(tmp_path):
    store = ClinicStore(tmp_path / "comdesk-linkage.db")
    with store.connect() as connection:
        for clinic_id in (1, 2, 3, 4):
            connection.execute(
                "INSERT INTO clinics(id,clinic_name,first_seen_at,last_seen_at) VALUES(?,?,?,?)",
                (clinic_id, f"医院{clinic_id}", "2026-10-16", "2026-10-16"),
            )
        connection.execute(
            "INSERT INTO source_records(clinic_id,source,record_json,source_hash,row_number,match_status,match_reason,match_score,created_at) "
            "VALUES(1,'コムデスク','{}','imported',0,'NEW','',0,'2026-10-16')"
        )
        connection.execute(
            "INSERT INTO source_records(clinic_id,source,record_json,source_hash,row_number,match_status,match_reason,match_score,created_at) "
            "VALUES(4,'コムデスク','{}','pending',1,'AMBIGUOUS','',0,'2026-10-16')"
        )
        pending_source_id = connection.execute(
            "SELECT id FROM source_records WHERE source_hash='pending'"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO match_reviews(source_record_id,candidates_json,updated_at) VALUES(?,?,?)",
            (pending_source_id, "[2,3]", "2026-10-16"),
        )

    assert store.comdesk_linkage_state() == {
        "imported_clinic_ids": {1, 4},
        "held_clinic_ids": {2, 3, 4},
    }
