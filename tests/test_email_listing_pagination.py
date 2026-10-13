from types import SimpleNamespace

from src.repository.email_research_repository import SupabaseEmailResearchRepository


class _Cursor:
    def __init__(self, conn):
        self.conn, self._result, self.description = conn, None, []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, params=None):
        self.conn.calls.append((sql, params))
        if "to_regclass" in sql:
            self._result = [(1, 1, 1)]
        elif sql.startswith("SELECT count(*) FROM email_research.clinic_email_status s"):
            self._result = [(self.conn.total,)]
        elif sql.startswith("SELECT s.clinic_id"):
            self.description = [SimpleNamespace(name=n) for n in ("clinic_id", "email")]
            self._result = [(1, "info@sample.example")]
        else:
            self._result = []

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return self._result


class _Conn:
    def __init__(self, total):
        self.total, self.calls, self.commits = total, [], 0

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def _listing_calls(conn):
    return [(sql, params) for sql, params in conn.calls if "to_regclass" not in sql]


def test_default_page_lists_primary_clinics_with_limit_offset_in_sql():
    conn = _Conn(total=1284)
    out = SupabaseEmailResearchRepository(conn).primary_listing_page(page=1, page_size=100)
    (count_sql, count_args), (rows_sql, rows_args) = _listing_calls(conn)
    assert "s.primary_email_candidate_id IS NOT NULL" in count_sql
    assert count_args == []
    assert rows_sql.endswith("LIMIT %s OFFSET %s")
    assert rows_args == [100, 0]
    assert out["total"] == 1284 and out["pages"] == 13 and out["page"] == 1
    assert out["rows"] == [{"clinic_id": 1, "email": "info@sample.example"}]


def test_page_is_clamped_to_last_page_and_offset_follows():
    conn = _Conn(total=1284)
    out = SupabaseEmailResearchRepository(conn).primary_listing_page(page=99, page_size=100)
    assert out["page"] == 13
    assert _listing_calls(conn)[1][1] == [100, 1200]


def test_all_filters_are_bound_parameters_in_one_where_clause():
    conn = _Conn(total=3)
    SupabaseEmailResearchRepository(conn).primary_listing_page(filters={
        "has_email": "all", "prefecture": "東京都", "clinic_name": "50%_off\\",
        "research_status": ["FOUND", "REVIEW"], "auto_decision": ["AUTO_TRUE"],
        "confidence": ["HIGH"], "role": ["GENERAL"], "ownership": ["CLINIC_OWNED"],
    }, page=1, page_size=100)
    count_sql, count_args = _listing_calls(conn)[0]
    assert "primary_email_candidate_id IS" not in count_sql
    for fragment in ("t.prefecture=%s", "t.clinic_name ILIKE %s", "s.research_status=ANY(%s::text[])",
                     "p.confidence=ANY(%s::text[])", "p.role=ANY(%s::text[])", "p.ownership=ANY(%s::text[])",
                     "d.auto_decision=ANY(%s::text[])"):
        assert fragment in count_sql
    assert count_args == ["東京都", "%50\\%\\_off\\\\%", ["FOUND", "REVIEW"], ["HIGH"], ["GENERAL"],
                          ["CLINIC_OWNED"], ["AUTO_TRUE"]]
    assert "東京都" not in count_sql and "50" not in count_sql


def test_unknown_filter_values_are_dropped_not_interpolated():
    conn = _Conn(total=0)
    SupabaseEmailResearchRepository(conn).primary_listing_page(
        filters={"has_email": "no", "role": ["GENERAL'; DROP TABLE x;--"], "confidence": ["BOGUS"]})
    count_sql, count_args = _listing_calls(conn)[0]
    assert "s.primary_email_candidate_id IS NULL" in count_sql
    assert "p.role" not in count_sql and "p.confidence" not in count_sql
    assert count_args == []


def test_page_size_is_bounded():
    conn = _Conn(total=5000)
    out = SupabaseEmailResearchRepository(conn).primary_listing_page(page_size=10_000)
    assert out["page_size"] == 500
    assert _listing_calls(conn)[1][1] == [500, 0]


def test_email_page_render_never_starts_workers_or_touches_hp():
    from src.utils.config import ROOT

    ui = (ROOT / "src/email_research_ui.py").read_text(encoding="utf-8")
    # The only start() call is inside the explicit "メール自動調査を開始" button branch.
    assert ui.count("runner.start(") == 1
    start_button = ui.index('"メール自動調査を開始", key="simple_email_auto_start"')
    assert start_button < ui.index("runner.start(") < ui.index('"一時停止", key="simple_email_auto_pause"')
    assert "auto_hp" not in ui and "hp_auto_run" not in ui
    assert "primary_listing(limit=500)" not in ui
