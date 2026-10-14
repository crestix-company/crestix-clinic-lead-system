import pytest

from src.outreach.email_readonly_access import (
    EmailReadOnlyAccessError, EmailReadOnlyReader, open_readonly_email_reader,
    read_primary_emails_readonly, readonly_url, verify_read_only_role,
)


class FakeCursor:
    def __init__(self, privileges, exists=True, rows=()):
        self.privileges, self.exists, self.rows, self.result = privileges, exists, rows, None
        self.last_sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.last_sql = sql
        if "to_regclass" in sql:
            self.result = self.exists
        elif "has_table_privilege" in sql:
            privilege = params[-1] if len(params) == 2 else "SELECT"
            self.result = privilege in self.privileges
        else:
            self.result = None

    def fetchone(self):
        return (self.result,)

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self, privileges, exists=True, rows=()):
        self.cursor_obj, self.closed = FakeCursor(privileges, exists, rows), False

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed = True


# --- 接続前ゲート: 環境変数 ---
def test_readonly_url_required_and_not_runtime():
    with pytest.raises(EmailReadOnlyAccessError, match="EMAIL_READONLY_DB_URL"):
        readonly_url({})
    with pytest.raises(EmailReadOnlyAccessError):
        readonly_url({"EMAIL_READONLY_DB_URL": "postgres://x", "SUPABASE_RUNTIME_DB_URL": "postgres://x"})


def test_readonly_url_independent_of_clinic_and_email_research_env_vars():
    # CLINIC_READONLY_DB_URL / EMAIL_RESEARCH_DB_URL が設定されていなくても、
    # 本モジュール専用のEMAIL_READONLY_DB_URLだけで動作する(既存Gateとは無関係)。
    url = readonly_url({"EMAIL_READONLY_DB_URL": "postgres://ro-email"})
    assert url == "postgres://ro-email"


# --- 権限ゲート ---
def test_role_with_write_privilege_is_rejected_and_closed():
    conn = FakeConn({"SELECT", "UPDATE"})
    with pytest.raises(EmailReadOnlyAccessError, match="UPDATE"):
        open_readonly_email_reader({"EMAIL_READONLY_DB_URL": "postgres://ro"}, connect_fn=lambda url: conn)
    assert conn.closed


def test_role_without_select_or_table_is_rejected():
    with pytest.raises(EmailReadOnlyAccessError, match="SELECT権限なし"):
        verify_read_only_role(FakeConn(set()))
    with pytest.raises(EmailReadOnlyAccessError, match="参照できません"):
        verify_read_only_role(FakeConn({"SELECT"}, exists=False))


def test_select_only_role_passes():
    verify_read_only_role(FakeConn({"SELECT"}))


def test_missing_env_var_stops_safely_without_connecting():
    called = []
    with pytest.raises(EmailReadOnlyAccessError):
        read_primary_emails_readonly([1, 2], environ={}, connect_fn=lambda url: called.append(url))
    assert called == []  # 接続試行そのものが発生しない


# --- final_sales_eligible / Human Review遵守(SQLに明示条件があることを確認) ---
def test_query_requires_final_sales_eligible_true():
    conn = FakeConn({"SELECT"}, rows=[(10, "info@example.jp")])
    reader = EmailReadOnlyReader(conn)
    result = reader.primary_emails_by_clinic_id([10])
    assert result == {10: "info@example.jp"}
    assert "final_sales_eligible = true" in conn.cursor_obj.last_sql
    assert "primary_email_candidate_id IS NOT NULL" in conn.cursor_obj.last_sql


def test_empty_clinic_ids_returns_empty_without_query():
    conn = FakeConn({"SELECT"})
    reader = EmailReadOnlyReader(conn)
    assert reader.primary_emails_by_clinic_id([]) == {}
    assert conn.cursor_obj.last_sql == ""  # クエリ自体を発行しない


# --- 書き込みメソッド不在の確認(設計の安全性の直接的な証跡) ---
def test_reader_has_no_write_methods():
    public_methods = {name for name in dir(EmailReadOnlyReader) if not name.startswith("_")}
    assert public_methods == {"primary_emails_by_clinic_id"}


def test_open_reader_sets_transaction_read_only_and_returns_reader():
    conn = FakeConn({"SELECT"}, rows=[(1, "a@example.jp")])
    reader, returned_conn = open_readonly_email_reader(
        {"EMAIL_READONLY_DB_URL": "postgres://ro"}, connect_fn=lambda url: conn)
    assert returned_conn is conn
    assert isinstance(reader, EmailReadOnlyReader)
    assert "default_transaction_read_only" in conn.cursor_obj.last_sql


def test_read_primary_emails_readonly_closes_connection_on_success():
    conn = FakeConn({"SELECT"}, rows=[(1, "a@example.jp")])
    result = read_primary_emails_readonly(
        [1], environ={"EMAIL_READONLY_DB_URL": "postgres://ro"}, connect_fn=lambda url: conn)
    assert result == {1: "a@example.jp"}
    assert conn.closed
