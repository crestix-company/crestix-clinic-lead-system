from src.repository.email_research_repository import SupabaseEmailResearchRepository


def valid_candidate(**changes):
    row = dict(clinic_id=1, email="Info@Example.jp", discovery_source="OFFICIAL_HP",
               role="GENERAL", ownership="CLINIC_OWNED", verification_status="VERIFIED_EMAIL",
               auto_identity_decision="TRUE", confidence="HIGH", clinic_name_match=True,
               prefecture_match=True, city_match=True, rule_version="test")
    row.update(changes)
    return row


class Cursor:
    def __init__(self, conn): self.conn, self.row = conn, None
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, sql, params=None):
        if "INSERT INTO email_research.email_candidates" in sql:
            key = (params["clinic_id"], params["normalized_email"])
            self.conn.ids.setdefault(key, len(self.conn.ids) + 100)
            self.row = (self.conn.ids[key],)
        elif "SELECT clinic_id,final_sales_eligible" in sql:
            self.row = self.conn.candidate_row
        elif "UPDATE email_research.clinic_email_status" in sql:
            self.rowcount = 1
    def fetchone(self): return self.row


class Conn:
    def __init__(self):
        self.ids = {}; self.candidate_row = (1, True); self.commits = 0; self.rollbacks = 0
    def cursor(self): return Cursor(self)
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1


def test_candidate_id_stable_for_same_clinic_and_normalized_email():
    conn = Conn(); repo = SupabaseEmailResearchRepository(conn)
    first = repo.upsert_candidate(valid_candidate())
    second = repo.upsert_candidate(valid_candidate(email=" info@example.jp "))
    changed = repo.upsert_candidate(valid_candidate(email="office@example.jp"))
    assert first == second
    assert changed != first


def test_repository_persists_source_and_email_ownership_separately():
    payload = SupabaseEmailResearchRepository._candidate_payload(valid_candidate(
        discovery_source="SEARCH_QUERY",
        source_url="https://www.iryou.teikyouseido.mhlw.go.jp/clinic/detail",
        source_owner_type="TRUSTED_PUBLIC_DIRECTORY",
        email_ownership="CLINIC_OWNED",
        ownership="THIRD_PARTY_OWNED",
    ))
    # The explicit email ownership field is canonical for eligibility; the legacy
    # alias is normalized to the same value rather than reviving the old source flag.
    assert payload["source_owner_type"] == "TRUSTED_PUBLIC_DIRECTORY"
    assert payload["email_ownership"] == "CLINIC_OWNED"
    assert payload["ownership"] == "CLINIC_OWNED"


def test_email_repository_has_no_production_clinic_dependency():
    source = open("src/repository/email_research_repository.py", encoding="utf-8").read()
    assert "public.clinics" not in source
    assert "def sync_targets(self, targets" in source
    assert "email_research.clinic_targets" in source


def test_email_sidecar_migration_has_no_unconditional_production_clinic_foreign_keys():
    """The cross_database sidecar topology must stay FK-free from public.clinics -- it is
    a different database and such an FK would be physically impossible there. The
    same_database topology (same file, auto-detected via to_regclass('public.clinics'))
    intentionally does add one; this test pins that FK to inside the guarded
    same_database do-block, not the unconditional top level of the file."""
    migration = open("scripts/supabase_migration/email_research_schema.sql", encoding="utf-8").read().lower()
    human = open("scripts/supabase_migration/email_human_review_schema.sql", encoding="utf-8").read().lower()
    assert "references public.clinics" not in human
    same_database_block = migration.split("if to_regclass('public.clinics') is not null then", 1)[1]
    cross_database_block = migration.split("if to_regclass('public.clinics') is null then", 1)[1].split(
        "end if;\nend $$;", 1)[0]
    assert "references public.clinics" in same_database_block
    assert "references public.clinics" not in cross_database_block
    assert "create table email_research.clinic_targets" in cross_database_block


def test_target_sync_is_sidecar_only_and_accepts_unchanged_snapshots():
    class SyncCursor:
        rowcount = 0
        def __init__(self): self.row = None
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, sql, params=None):
            self.conn.statements.append(sql)
            if "SELECT clinic_uuid FROM email_research.clinic_targets" in sql:
                self.row = ("uuid-1",)
            elif "INSERT INTO email_research.clinic_targets" in sql:
                self.rowcount = 0  # ON CONFLICT ... WHERE unchanged means no UPDATE.
        def fetchone(self): return self.row
    class SyncConn(Conn):
        def __init__(self): super().__init__(); self.statements=[]
        def cursor(self):
            cursor=SyncCursor(); cursor.conn=self; return cursor
    conn=SyncConn()
    repo=SupabaseEmailResearchRepository(conn)
    count=repo.sync_targets([{
        "clinic_id": 1, "clinic_uuid": "uuid-1", "clinic_name": "A",
        "official_hp_url": "https://a.example/", "hp_verified_at": None,
    }])
    assert count == 1
    assert conn.commits == 1 and conn.rollbacks == 0
    assert all("public.clinics" not in sql for sql in conn.statements)


def test_clinic_candidate_fk_mismatch_is_rejected_before_primary_update():
    conn = Conn(); conn.candidate_row = (2, True)
    repo = SupabaseEmailResearchRepository(conn)
    import pytest
    with pytest.raises(ValueError, match="別Clinic"):
        repo.set_primary_candidate(clinic_id=1, candidate_id=100)
    assert conn.rollbacks == 1


def test_status_and_primary_schema_invariants_are_explicit():
    sql = open("scripts/supabase_migration/email_research_schema.sql", encoding="utf-8").read()
    assert "NOT_RESEARCHED','RUNNING','FOUND','NO_EMAIL_FOUND','REVIEW','ERROR" in sql
    assert "unique(clinic_id,normalized_email)" in sql
    assert "foreign key(clinic_id,primary_email_candidate_id)" in sql
    assert "is_primary" not in sql


def test_background_targets_require_verified_official_hp_and_no_exclude_reason():
    source = open("src/repository/email_research_repository.py", encoding="utf-8").read()
    target_reader = open("src/repository/email_target_reader.py", encoding="utf-8").read()
    assert "c.hp_status='VERIFIED'" in target_reader
    assert "c.maps_presence_status='MAPS_MATCHED_WEBSITE'" in target_reader
    assert "COALESCE(BTRIM(c.exclude_reason),'')=''" in target_reader
    assert "c.eligible=true" in source
    assert "c.manual_exclude=false" in source
    assert "s.primary_email_candidate_id IS NULL" in source
    assert "FOR UPDATE OF s,c SKIP LOCKED" in source
    assert "status='RUNNING' AND stop_requested=false" in source
    assert "FOR SHARE" in source
    assert "s.research_status='NOT_RESEARCHED'" in source


def test_summary_definitions_keep_sales_primary_and_new_verified_targets_separate():
    source = open("src/repository/email_research_repository.py", encoding="utf-8").read()
    assert "p.final_sales_eligible=true" in source
    assert '"email_researched": found + reviewed_count + no_email_count' in source
    assert '"not_researched": newly_eligible' in source


def test_email_auto_run_status_supports_stop_without_production_side_effects():
    sql = open("scripts/supabase_migration/email_research_schema.sql", encoding="utf-8").read()
    assert "'STOPPED'" in sql
    ui = open("src/email_research_ui.py", encoding="utf-8").read()
    assert 'os.environ.get("EMAIL_AUTO_RUN_ENABLED", "false")' in ui


class _NoDBTouchConn:
    """Raises if .cursor() is ever called -- proves the cohort guard rejects before any
    SQL is issued, not merely that the eventual write happens to fail."""
    def cursor(self):
        raise AssertionError("cohort guard did not short-circuit before touching the DB")
    def commit(self): raise AssertionError("must not commit a cohort violation")
    def rollback(self): pass


# C: cohort-outside candidate write -> reject before any DB access
def test_upsert_candidate_outside_cohort_is_rejected_without_db_access():
    repo = SupabaseEmailResearchRepository(_NoDBTouchConn(), cohort=frozenset({1, 2, 3}))
    try:
        repo.upsert_candidate(valid_candidate(clinic_id=999))
    except ValueError as exc:
        assert "cohort" in str(exc) or "Canary" in str(exc)
    else:
        raise AssertionError("expected ValueError for a clinic_id outside the cohort")


def test_upsert_candidate_inside_cohort_is_not_rejected_by_the_guard():
    conn = Conn()
    repo = SupabaseEmailResearchRepository(conn, cohort=frozenset({1, 2, 3}))
    # Reaching the real Conn/Cursor fake (which only understands this one statement
    # shape) proves the guard let it through rather than raising first.
    candidate_id = repo.upsert_candidate(valid_candidate(clinic_id=1))
    assert candidate_id is not None


# D: cohort-outside status/claim write (complete_claim) -> reject before any DB access
def test_complete_claim_outside_cohort_is_rejected_without_db_access():
    repo = SupabaseEmailResearchRepository(_NoDBTouchConn(), cohort=frozenset({1, 2, 3}))
    try:
        repo.complete_claim(clinic_id=999, claim_token="t", candidates=[], research_status="NO_EMAIL_FOUND")
    except ValueError as exc:
        assert "cohort" in str(exc) or "Canary" in str(exc)
    else:
        raise AssertionError("expected ValueError for a clinic_id outside the cohort")


def test_set_primary_candidate_outside_cohort_is_rejected_without_db_access():
    repo = SupabaseEmailResearchRepository(_NoDBTouchConn(), cohort=frozenset({1, 2, 3}))
    try:
        repo.set_primary_candidate(clinic_id=999, candidate_id=1)
    except ValueError as exc:
        assert "cohort" in str(exc) or "Canary" in str(exc)
    else:
        raise AssertionError("expected ValueError for a clinic_id outside the cohort")


# A: sync only ever seeds the cohort, never the full live-eligible pool
def test_same_database_sync_targets_scopes_to_cohort_when_set():
    class RecordingCursor:
        def __init__(self, sink): self.sink = sink; self.rowcount = 0
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, sql, params=None): self.sink.append((sql, params))
    class RecordingConn:
        def __init__(self): self.statements = []
        def cursor(self): return RecordingCursor(self.statements)
        def commit(self): pass
        def rollback(self): pass

    conn = RecordingConn()
    repo = SupabaseEmailResearchRepository(conn, same_database=True, cohort=frozenset({10, 20, 30}))
    repo.sync_targets(())  # targets argument is ignored in same_database mode either way
    sql, params = conn.statements[-1]
    assert "clinic_id=ANY(%s::bigint[])" in sql
    assert set(params[0]) == {10, 20, 30}


def test_same_database_sync_targets_unscoped_when_cohort_is_none():
    class RecordingCursor:
        def __init__(self, sink): self.sink = sink; self.rowcount = 0
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, sql, params=None): self.sink.append((sql, params))
    class RecordingConn:
        def __init__(self): self.statements = []
        def cursor(self): return RecordingCursor(self.statements)
        def commit(self): pass
        def rollback(self): pass

    conn = RecordingConn()
    repo = SupabaseEmailResearchRepository(conn, same_database=True)  # cohort=None: unchanged normal path
    repo.sync_targets(())
    sql, params = conn.statements[-1]
    assert "clinic_id=ANY" not in sql
    assert params is None


# B: claim_next adds a cohort filter to its WHERE clause only when a cohort is set
def test_claim_next_sql_includes_cohort_filter_when_set():
    source = open("src/repository/email_research_repository.py", encoding="utf-8").read()
    assert 'cohort_clause = "AND s.clinic_id=ANY(%s::bigint[])\\n" if self._cohort is not None else ""' in source
    assert "cohort_params + (str(run_id or \"\"), claim_token, worker_id" in source


class DispatchCursor:
    """Routes execute() to a canned fetchone/fetchall result by matching a SQL substring key."""
    def __init__(self, conn): self.conn = conn; self._result = None
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, sql, params=None):
        self.conn.calls.append((sql, params))
        for key, result in self.conn.responses.items():
            if key in sql:
                self._result = result
                return
        self._result = None
    def fetchone(self): return self._result
    def fetchall(self): return self._result or []
    @property
    def description(self):
        names = self.conn.descriptions.get(self._description_key(), [])
        return [type("Col", (), {"name": n}) for n in names]
    def _description_key(self):
        for key in self.conn.descriptions:
            if self.conn.calls and key in self.conn.calls[-1][0]:
                return key
        return None


class DispatchConn:
    def __init__(self, responses, descriptions=None):
        self.responses = responses
        self.descriptions = descriptions or {}
        self.calls = []
        self.commits = 0
    def cursor(self): return DispatchCursor(self)
    def commit(self): self.commits += 1
    def rollback(self): pass


def test_cohort_batch_summary_is_none_outside_canary_mode():
    repo = SupabaseEmailResearchRepository(DispatchConn({}))
    assert repo.cohort_batch_summary() is None


def test_cohort_batch_summary_scopes_counts_to_cohort_and_computes_pending():
    conn = DispatchConn({
        "FROM email_research.clinic_email_status\n                    WHERE clinic_id=ANY": (3, 2, 4, 0, 9),
    })
    repo = SupabaseEmailResearchRepository(conn, cohort=frozenset(range(1, 11)))
    out = repo.cohort_batch_summary()
    assert out == {"cohort_size": 10, "processed": 9, "pending": 1,
                    "found": 3, "review": 2, "no_email_found": 4, "error": 0}


def test_status_for_ids_returns_empty_dict_for_empty_input():
    repo = SupabaseEmailResearchRepository(DispatchConn({}))
    assert repo.status_for_ids([]) == {}


def test_status_for_ids_bulk_lookup_single_query():
    conn = DispatchConn({
        "to_regclass": (1, 1, 1),
        "SELECT clinic_id, research_status, primary_email_candidate_id IS NOT NULL":
            [(1, "FOUND", True), (2, "REVIEW", False)],
    })
    repo = SupabaseEmailResearchRepository(conn)
    out = repo.status_for_ids([1, 2, 3])
    assert out == {1: {"research_status": "FOUND", "has_primary": True},
                    2: {"research_status": "REVIEW", "has_primary": False}}
    # exactly one query for availability + one bulk lookup -- never per-id
    assert len(conn.calls) == 2


def test_primary_listing_returns_clinic_unit_rows_not_candidate_count():
    conn = DispatchConn(
        {"to_regclass": (1, 1, 1),
         "SELECT s.clinic_id, t.clinic_name": [
            (1, "サンプル医院", "東京都", "千代田区", "info@sample.example", "GENERAL",
             "CLINIC_OWNED", "CLINIC_OWNED", "OFFICIAL_HP", "OFFICIAL_CLINIC",
             "https://sample.example/", "HIGH", [], True, "2026-10-11T00:00:00+00:00"),
        ]},
        descriptions={"SELECT s.clinic_id, t.clinic_name": [
            "clinic_id", "clinic_name", "prefecture", "city", "email", "role", "ownership",
            "email_ownership", "discovery_source", "source_owner_type", "source_url",
            "confidence", "hard_block_codes", "final_sales_eligible", "updated_at",
        ]},
    )
    repo = SupabaseEmailResearchRepository(conn)
    rows = repo.primary_listing(limit=10)
    assert len(rows) == 1
    assert rows[0]["clinic_id"] == 1
    assert rows[0]["email"] == "info@sample.example"
    assert rows[0]["hard_block_codes"] == []
