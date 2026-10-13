from types import SimpleNamespace

import pytest

from src.repository.email_target_reader import (
    FORBIDDEN_WRITE_PRIVILEGES,
    REQUIRED_SELECT_TABLES,
    SupabaseEmailTargetReader,
    _verify_read_only_role,
    connect_email_target_reader,
)


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = []
        self.row = None
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def execute(self, sql, params=None):
        self.conn.statements.append((sql, params))
        if "FROM public.clinics" in sql:
            self.description = [SimpleNamespace(name=name) for name in (
                "id", "uuid", "clinic_name", "prefecture", "address", "phone",
                "official_hp_url", "researched_at",
            )]
            self.conn.rows = [(1001, "uuid-1001", "テスト医院", "東京都", "東京都新宿区1",
                               "03-0000-0000", "https://clinic.example/", None)]
    def fetchone(self): return self.row
    def fetchall(self): return self.conn.rows


class Connection:
    def __init__(self):
        self.statements = []
        self.rows = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
    def cursor(self): return Cursor(self)
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1
    def close(self): self.closed = True


def test_target_reader_is_select_only_and_returns_minimal_verified_snapshot():
    conn = Connection()
    targets = SupabaseEmailTargetReader(conn).eligible_targets()
    assert len(targets) == 1
    target = targets[0]
    assert target["clinic_id"] == 1001
    assert target["clinic_uuid"] == "uuid-1001"
    assert target["city"] == "新宿区"
    assert target["official_hp_url"] == "https://clinic.example/"
    assert all(sql.lstrip().upper().startswith("SELECT") for sql, _ in conn.statements)
    assert conn.commits == 1


# --- Role/grant-based read-only verification -----------------------------
# Pooled Supabase connections don't reliably honor session-level
# default_transaction_read_only, so the safety check is role/grant-based
# (has_table_privilege), not SHOW-based. These fakes simulate that.

class PrivilegeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = None
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def execute(self, sql, params=None):
        self.conn.statements.append((sql, params))
        if "has_table_privilege" in sql:
            if len(params) == 2:
                table, privilege = params
            else:
                (table,), privilege = params, "SELECT"
            self._result = self.conn.privileges.get((table, privilege), False)
    def fetchone(self):
        return (self._result,)


class PrivilegeConnection:
    def __init__(self, privileges):
        self.privileges = privileges
        self.statements = []
        self.commits = 0
        self.closed = False
    def cursor(self): return PrivilegeCursor(self)
    def commit(self): self.commits += 1
    def close(self): self.closed = True


def _privileges(*, select_tables=REQUIRED_SELECT_TABLES, write_tables=()):
    privileges = {}
    for table in REQUIRED_SELECT_TABLES:
        privileges[(table, "SELECT")] = table in select_tables
        for privilege in FORBIDDEN_WRITE_PRIVILEGES:
            privileges[(table, privilege)] = table in write_tables
    return privileges


def test_verify_read_only_role_passes_for_clinic_readonly_shaped_role():
    conn = PrivilegeConnection(_privileges())
    _verify_read_only_role(conn)
    assert conn.commits == 1


@pytest.mark.parametrize("label,privileges", [
    ("clinic_runtime-shaped (SELECT+WRITE)",
     _privileges(write_tables=REQUIRED_SELECT_TABLES)),
    ("owner/postgres-shaped (SELECT+WRITE)",
     _privileges(write_tables=REQUIRED_SELECT_TABLES)),
    ("clinic_email_runtime-shaped (no SELECT on these tables)",
     _privileges(select_tables=())),
])
def test_verify_read_only_role_fails_closed_for_unsafe_roles(label, privileges):
    conn = PrivilegeConnection(privileges)
    with pytest.raises(RuntimeError):
        _verify_read_only_role(conn)


def _factory_env(readonly_label):
    return {
        "EMAIL_RESEARCH_DB_URL": "postgresql://email@email.example.net:5432/email",
        "SUPABASE_RUNTIME_DB_URL": "postgresql://runtime@prod.example.net:5432/clinic",
        "CLINIC_READONLY_DB_URL": f"postgresql://{readonly_label}@prod.example.net:5432/clinic",
    }


def test_reader_factory_accepts_properly_scoped_readonly_role():
    conn = PrivilegeConnection(_privileges())
    env = _factory_env("clinic_readonly")
    reader = connect_email_target_reader(
        environ=env, connect_fn=lambda url, *, read_only: conn)
    assert isinstance(reader, SupabaseEmailTargetReader)
    assert not conn.closed


@pytest.mark.parametrize("readonly_label,privileges", [
    ("clinic_runtime", _privileges(write_tables=REQUIRED_SELECT_TABLES)),
    ("postgres", _privileges(write_tables=REQUIRED_SELECT_TABLES)),
    ("clinic_email_runtime", _privileges(select_tables=())),
])
def test_reader_factory_rejects_unsafe_roles_and_closes_connection(readonly_label, privileges):
    conn = PrivilegeConnection(privileges)
    env = _factory_env(readonly_label)
    with pytest.raises(RuntimeError):
        connect_email_target_reader(
            environ=env, connect_fn=lambda url, *, read_only: conn)
    assert conn.closed


def test_reader_factory_uses_only_dedicated_read_only_url():
    conn = PrivilegeConnection(_privileges())
    calls = []
    env = _factory_env("clinic_readonly")
    reader = connect_email_target_reader(environ=env,
        connect_fn=lambda url, *, read_only: calls.append((url, read_only)) or conn)
    assert isinstance(reader, SupabaseEmailTargetReader)
    assert calls == [(env["CLINIC_READONLY_DB_URL"], True)]
