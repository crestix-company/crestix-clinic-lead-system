from src.repository.email_database import (
    build_email_repositories,
    clear_email_repository_cache_for_tests,
    email_database_status,
    email_target_reader_status,
)


PRODUCTION = "postgresql://runtime:secret@prod.example.net:5432/clinic"
EMAIL = "postgresql://email:secret@email.example.net:5432/email_state"
READONLY = "postgresql://reader:secret@prod.example.net:5432/clinic"

def safe_env():
    return {"EMAIL_RESEARCH_DB_URL": EMAIL, "SUPABASE_RUNTIME_DB_URL": PRODUCTION,
            "CLINIC_READONLY_DB_URL": READONLY}


def test_email_db_missing_fails_closed_without_runtime_fallback():
    called = []
    bundle, status = build_email_repositories(
        environ={"SUPABASE_RUNTIME_DB_URL": PRODUCTION},
        connect_fn=lambda *a, **k: called.append((a, k)),
    )
    assert bundle is None
    assert not status.ready
    assert "共有DBが未接続" in status.message
    assert called == []


def test_same_database_target_fails_closed_without_opening_connection():
    called = []
    bundle, status = build_email_repositories(
        environ={"EMAIL_RESEARCH_DB_URL": PRODUCTION, "SUPABASE_RUNTIME_DB_URL": PRODUCTION,
                 "CLINIC_READONLY_DB_URL": READONLY},
        connect_fn=lambda *a, **k: called.append((a, k)),
    )
    assert bundle is None
    assert not status.ready
    assert "Production Schema適用前" in status.message
    assert called == []


def test_same_supabase_project_direct_and_pooler_urls_are_rejected():
    project = "abcdefghijklmnopqrst"
    status = email_database_status({
        "EMAIL_RESEARCH_DB_URL": f"postgresql://postgres.{project}:secret@aws-0-us-west-1.pooler.supabase.com:6543/postgres",
        "SUPABASE_RUNTIME_DB_URL": f"postgresql://postgres:secret@db.{project}.supabase.co:5432/postgres",
        "CLINIC_READONLY_DB_URL": f"postgresql://reader:secret@db.{project}.supabase.co:5432/postgres",
    })
    assert not status.ready
    assert "Production Schema適用前" in status.message


def test_uncomparable_database_identity_fails_closed():
    status = email_database_status({
        "EMAIL_RESEARCH_DB_URL": "not-a-postgres-url",
        "SUPABASE_RUNTIME_DB_URL": PRODUCTION,
        "CLINIC_READONLY_DB_URL": READONLY,
    })
    assert not status.ready
    assert "分離を確認できない" in status.message


def test_distinct_database_uses_only_email_url_and_builds_both_email_repositories():
    clear_email_repository_cache_for_tests()

    class FakeConnection:
        closed = False
        def close(self): self.closed = True

    calls = []
    connection = FakeConnection()
    def fake_connect(url, *, autocommit):
        calls.append((url, autocommit))
        return connection

    bundle, status = build_email_repositories(
        environ=safe_env(),
        connect_fn=fake_connect,
    )
    assert status.ready
    assert bundle.email_research._conn is connection
    assert bundle.email_human_review._conn is connection
    assert calls == [(EMAIL, False)]


def test_each_auto_worker_gets_a_separate_email_connection():
    clear_email_repository_cache_for_tests()
    from src.repository.email_database import build_email_repositories
    calls = []
    class FakeConnection:
        def close(self): pass
    def fake_connect(url, *, autocommit):
        conn = FakeConnection()
        calls.append((url, autocommit, conn))
        return conn
    env = safe_env()
    one, _ = build_email_repositories(environ=env, connect_fn=fake_connect)
    two, _ = build_email_repositories(environ=env, connect_fn=fake_connect)
    assert one.email_research._conn is not two.email_research._conn
    assert len(calls) == 2 and all(call[0] == EMAIL and call[1] is False for call in calls)


def test_general_runtime_write_bundle_never_uses_runtime_conn_for_email(monkeypatch):
    from src.repository import supabase_adapter
    from src.repository.write_backend import build_write_repositories
    calls = []
    class FakeConnection:
        def __init__(self, url): self.url = url
    monkeypatch.setenv("SUPABASE_RUNTIME_DB_URL", PRODUCTION)
    monkeypatch.delenv("EMAIL_RESEARCH_DB_URL", raising=False)
    monkeypatch.setattr(supabase_adapter, "connect", lambda url, **_: calls.append(url) or FakeConnection(url))
    bundle = build_write_repositories("supabase")
    assert calls == [PRODUCTION]
    assert bundle.email_research is None
    assert bundle.email_human_review is None




def test_password_is_never_in_safety_message():
    status = email_database_status({
        "EMAIL_RESEARCH_DB_URL": EMAIL,
        "SUPABASE_RUNTIME_DB_URL": EMAIL,
        "CLINIC_READONLY_DB_URL": READONLY,
    })
    assert "secret" not in status.message


def test_missing_read_only_source_fails_closed():
    status = email_target_reader_status({"EMAIL_RESEARCH_DB_URL": EMAIL,
                                         "CLINIC_READONLY_DB_URL": ""})
    assert not status.ready
    assert "READ ONLY接続が未設定" in status.message


def test_email_db_matching_read_only_source_is_rejected():
    status = email_database_status({"EMAIL_RESEARCH_DB_URL": PRODUCTION,
                                    "SUPABASE_RUNTIME_DB_URL": PRODUCTION,
        "CLINIC_READONLY_DB_URL": READONLY,
                                    "CLINIC_READONLY_DB_URL": READONLY})
    assert not status.ready
    assert "Production Schema適用前" in status.message


def test_read_only_target_url_must_match_clinic_runtime_source():
    status = email_target_reader_status({
        "EMAIL_RESEARCH_DB_URL": EMAIL,
        "SUPABASE_RUNTIME_DB_URL": PRODUCTION,
        "CLINIC_READONLY_DB_URL": "postgresql://reader@other.example.net:5432/clinic",
    })
    assert not status.ready
    assert "一致しません" in status.message
