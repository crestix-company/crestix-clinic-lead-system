from src.repository.email_research_repository import SupabaseEmailResearchRepository
import src.outreach_ui as outreach_ui


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = [type("Col", (), {"name": "clinic_id"})(), type("Col", (), {"name": "email"})()]
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=()):
        self.conn.queries.append((sql, params))
        if sql.startswith("SELECT count(*)"):
            self.rows = [(1001,)]
        elif sql.startswith("SELECT to_regclass"):
            self.rows = [("candidates", "status", "runs")]
        else:
            if "clinic_id>%s" in sql:
                after, limit = params
                self.rows = [(i, f"{i}@example.jp") for i in range(after + 1, min(after + limit, 1001) + 1)]
            else:
                limit, offset = params
                self.rows = [(i, f"{i}@example.jp") for i in range(offset + 1, min(offset + limit, 1001) + 1)]

    def fetchone(self):
        return self.rows[0]

    def fetchall(self):
        return self.rows


class Conn:
    def __init__(self):
        self.queries = []

    def cursor(self):
        return Cursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass


def test_formal_email_targets_have_stable_complete_pages_and_exact_gate():
    conn = Conn()
    repo = SupabaseEmailResearchRepository(conn)
    page1 = repo.formal_sales_eligible_page(page=1, page_size=500)
    page2 = repo.formal_sales_eligible_page(page=2, page_size=500)
    page3 = repo.formal_sales_eligible_page(page=3, page_size=500)
    assert page1["total"] == 1001 and page1["pages"] == 3
    assert len(page1["rows"]) == len(page2["rows"]) == 500
    assert page3["rows"] == [(1001, "1001@example.jp")]
    sql = " ".join(query for query, _ in conn.queries)
    assert "p.final_sales_eligible=true" in sql
    assert "ORDER BY s.clinic_id ASC" in sql


def test_formal_email_keyset_page_does_not_use_offset():
    conn = Conn()
    repo = SupabaseEmailResearchRepository(conn)
    first = repo.formal_sales_eligible_page(after_clinic_id=0, page_size=500)
    second = repo.formal_sales_eligible_page(after_clinic_id=first["next_after_clinic_id"], page_size=500)
    assert first["rows"][0][0] == 1 and second["rows"][0][0] == 501
    page_sql = [sql for sql, _ in conn.queries if sql.startswith("SELECT s.clinic_id,p.email")]
    assert "clinic_id>%s" in page_sql[-1] and "OFFSET" not in page_sql[-1]


def test_all_formal_email_rows_collects_160005_without_scan_pool(monkeypatch):
    total = 160005
    def page(_page, after_clinic_id=None):
        start = int(after_clinic_id or 0) + 1
        rows = [(cid, f"{cid}@example.jp") for cid in range(start, min(start + 500, total + 1))]
        return {"rows": rows, "total": total, "page_size": 500,
                "next_after_clinic_id": rows[-1][0] if rows else after_clinic_id}
    monkeypatch.setattr(outreach_ui, "_formal_email_page", page)
    count, rows = outreach_ui._all_formal_email_rows()
    assert count == total and len(rows) == total
    assert rows[0][0] == 1 and rows[-1][0] == total
