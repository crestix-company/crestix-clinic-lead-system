from app_v2 import comdesk_sync_candidates, comdesk_sync_csv
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
        assert filters.effective_ranks == []
        assert filters.active_only is True
        assert filters.hp_only is True
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
                # Synchronization deliberately accepts every HP rank.
                "effective_hp_rank": "D" if clinic_id % 2 else "UNKNOWN",
            }
            for clinic_id in range(offset + 1, end + 1)
        ]

    def comdesk_linkage_state(self):
        return {"imported_clinic_ids": {2}, "held_clinic_ids": {3}}


def test_sync_candidates_page_through_all_hp_confirmed_clinics_and_exclude_linkage_states():
    store = _LargeClinicStore(164_199)
    candidates = comdesk_sync_candidates(store)

    assert store.calls == [(50_000, 0), (50_000, 50_000), (50_000, 100_000), (50_000, 150_000)]
    assert len(candidates) == 164_195
    assert [item["id"] for item in candidates[:3]] == [1, 6, 7]
    assert candidates[-1]["id"] == 164_199


class _SupabaseHpSuccessStore:
    is_supabase_runtime = True

    def latest_completed_hp_job(self):
        return {"id": 81}

    def hp_job_export_summary(self, job_id):
        assert job_id == 81
        return {"export_ids": [11, 12, 13, 14]}

    def records_by_ids(self, clinic_ids):
        assert clinic_ids == [11, 12, 13, 14]
        return [
            {"id": clinic_id, "uuid": "SYNCED" if clinic_id == 14 else "", "clinic_name": f"医院{clinic_id}"}
            for clinic_id in clinic_ids
        ]

    def comdesk_linkage_state(self):
        return {"imported_clinic_ids": {11}, "held_clinic_ids": {12}}


def test_supabase_sync_reuses_canonical_hp_success_ids_before_linkage_exclusions():
    assert [row["id"] for row in comdesk_sync_candidates(_SupabaseHpSuccessStore())] == [13]


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


def test_sync_csv_row_count_matches_candidate_count_and_keeps_thirty_columns():
    candidates = [
        {"id": 1, "clinic_name": "A医院", "effective_hp_rank": "D"},
        {"id": 2, "clinic_name": "B医院", "effective_hp_rank": "UNKNOWN"},
    ]

    table = load_table(comdesk_sync_csv(object(), candidates), "sync.csv")

    assert table.headers == COMDESK_EXPORT_HEADERS
    assert len(table.headers) == 30
    assert len(table.data) == len(candidates) == 2


def test_uuid_added_after_unmatched_comdesk_import_stays_pending_without_clinic_creation(tmp_path):
    store = ClinicStore(tmp_path / "comdesk-reimport.db")
    initial = [""] * len(COMDESK_HEADERS)
    initial[COMDESK_HEADERS.index("名前")] = "再取込テスト医院"
    initial[COMDESK_HEADERS.index("Tel1")] = "03-1234-5678"
    initial[COMDESK_HEADERS.index("都道府県")] = "東京都"
    initial[COMDESK_HEADERS.index("住所１")] = "千代田区1-1"
    first = load_table(csv_bytes(COMDESK_HEADERS, [initial]), "initial.csv")

    assert store.import_comdesk(first)["AMBIGUOUS"] == 1
    assert store.query(Filters(active_only=False, hp_only=False)) == []

    with_uuid = initial.copy()
    with_uuid[COMDESK_HEADERS.index("UUID")] = "COMDESK-UUID-1"
    second = load_table(csv_bytes(COMDESK_HEADERS, [with_uuid]), "reimport.csv")
    result = store.import_comdesk(second)
    repeated = store.import_comdesk(second)

    clinics = store.query(Filters(active_only=False, hp_only=False))
    assert result["AMBIGUOUS"] == 1
    assert repeated["already_imported"] is True
    assert clinics == []
    with store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM source_records WHERE source='コムデスク'").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM comdesk_original_rows").fetchone()[0] == 2
    assert store.comdesk_linkage_state()["imported_clinic_ids"] == set()


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
