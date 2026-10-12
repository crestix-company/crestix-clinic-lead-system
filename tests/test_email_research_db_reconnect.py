"""Email Research Repository resilience to a transient DB connection loss.

Root cause this locks in: ``available()`` (and every other read/write method) used to call
the raw ``self._conn.rollback()`` in its ``except`` block. If the connection itself was
already dead (server closed it, network dropped), that rollback call raised
``psycopg.OperationalError`` on top of the original error, masking it and crashing the
caller -- including Streamlit UI fragments that have no try/except of their own.
"""
import pytest
import psycopg

import src.email_research_ui as email_research_ui
from src.repository.email_research_repository import SupabaseEmailResearchRepository
from src.repository.email_human_review_repository import SupabaseEmailHumanReviewRepository


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, params=None):
        if self.conn.raise_on_execute is not None:
            # Real psycopg marks a connection closed/broken after a fatal connection
            # error; the fake reproduces that so reconnect-guard logic sees it too.
            self.conn.closed = True
            raise self.conn.raise_on_execute
        self.conn.calls.append(sql)
        self._result = self.conn.response

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._result or []

    @property
    def description(self):
        return [type("Col", (), {"name": n}) for n in self.conn.description_names]


class _FakeConn:
    """A connection whose cursor() always raises ``raise_on_execute`` when set."""

    def __init__(self, *, response=(1, 1, 1), raise_on_execute=None, closed=False,
                 description_names=()):
        self.response = response
        self.raise_on_execute = raise_on_execute
        self.closed = closed
        self.description_names = description_names
        self.calls = []
        self.commits = 0
        self.rollbacks = 0
        self.rollback_raises = None

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1
        if self.rollback_raises is not None:
            raise self.rollback_raises

    def close(self):
        self.closed = True


def _connect_fn_returning(conn, calls):
    def connect_fn(url, *, autocommit=False):
        calls.append(url)
        return conn

    return connect_fn


def _connect_fn_raising(exc, calls):
    def connect_fn(url, *, autocommit=False):
        calls.append(url)
        raise exc

    return connect_fn


# A. available() with a dead connection reconnects and reports True.
def test_available_reconnects_after_dead_connection_and_reports_true(monkeypatch):
    monkeypatch.setenv("EMAIL_RESEARCH_DB_URL", "postgresql://fake:fake@localhost/fake")
    dead = _FakeConn(raise_on_execute=psycopg.OperationalError("server closed the connection unexpectedly"))
    fresh = _FakeConn(response=(1, 1, 1))
    reconnect_calls = []
    repo = SupabaseEmailResearchRepository(
        dead, connect_fn=_connect_fn_returning(fresh, reconnect_calls))

    assert repo.available() is True
    assert len(reconnect_calls) == 1
    assert repo._conn is fresh


# B. A rollback on an already-dead connection never surfaces OperationalError, and never
# masks the original (non-connection) error.
def test_safe_rollback_swallows_operational_error_on_dead_connection():
    dead = _FakeConn()
    dead.closed = True
    dead.rollback_raises = psycopg.OperationalError("the connection is lost")
    repo = SupabaseEmailResearchRepository(dead)

    repo._safe_rollback()  # must not raise
    assert dead.rollbacks == 0  # closed=True short-circuits before ever calling rollback()


def test_non_connection_error_is_not_masked_by_a_failing_rollback(monkeypatch):
    monkeypatch.setenv("EMAIL_RESEARCH_DB_URL", "postgresql://fake:fake@localhost/fake")
    conn = _FakeConn(raise_on_execute=ValueError("not a connection problem"))
    conn.rollback_raises = psycopg.OperationalError("the connection is lost")
    # ValueError's side effect in the fake also flips closed=True (mirrors real psycopg's
    # behavior), so _safe_rollback must still not reach the raising rollback() --
    # confirming the original ValueError, not rollback's OperationalError, propagates.
    repo = SupabaseEmailResearchRepository(conn)

    with pytest.raises(ValueError, match="not a connection problem"):
        repo.available()


# C. A read query fails once with OperationalError, reconnects, and succeeds on retry.
def test_read_query_retries_once_after_reconnect_then_succeeds(monkeypatch):
    monkeypatch.setenv("EMAIL_RESEARCH_DB_URL", "postgresql://fake:fake@localhost/fake")
    dead = _FakeConn(raise_on_execute=psycopg.OperationalError("server closed the connection unexpectedly"))
    fresh = _FakeConn(response=(1,))  # 1 of 3 cohort clinics terminal -> pending=2
    reconnect_calls = []
    repo = SupabaseEmailResearchRepository(
        dead, cohort=frozenset({1, 2, 3}),
        connect_fn=_connect_fn_returning(fresh, reconnect_calls))

    assert repo.cohort_pending_count() == 2
    assert len(reconnect_calls) == 1
    assert repo._conn is fresh


def test_read_query_does_not_retry_more_than_once(monkeypatch):
    """A second consecutive failure (even after reconnecting) must propagate, not loop."""
    monkeypatch.setenv("EMAIL_RESEARCH_DB_URL", "postgresql://fake:fake@localhost/fake")
    dead = _FakeConn(raise_on_execute=psycopg.OperationalError("server closed the connection unexpectedly"))
    still_dead = _FakeConn(raise_on_execute=psycopg.OperationalError("server closed the connection unexpectedly"))
    reconnect_calls = []
    repo = SupabaseEmailResearchRepository(
        dead, cohort=frozenset({1, 2, 3}),
        connect_fn=_connect_fn_returning(still_dead, reconnect_calls))

    with pytest.raises(psycopg.OperationalError):
        repo.cohort_pending_count()
    assert len(reconnect_calls) == 1  # exactly one reconnect attempt, no retry loop


# D. Reconnect itself fails -> available() reports False, never raises.
def test_available_returns_false_when_reconnect_fails(monkeypatch):
    monkeypatch.setenv("EMAIL_RESEARCH_DB_URL", "postgresql://fake:fake@localhost/fake")
    dead = _FakeConn(raise_on_execute=psycopg.OperationalError("server closed the connection unexpectedly"))
    reconnect_calls = []
    repo = SupabaseEmailResearchRepository(
        dead, connect_fn=_connect_fn_raising(
            psycopg.OperationalError("could not connect to server"), reconnect_calls))

    assert repo.available() is False
    assert len(reconnect_calls) == 1


def test_available_returns_false_without_db_url_configured(monkeypatch):
    monkeypatch.delenv("EMAIL_RESEARCH_DB_URL", raising=False)
    dead = _FakeConn(raise_on_execute=psycopg.OperationalError("server closed the connection unexpectedly"))
    repo = SupabaseEmailResearchRepository(dead, connect_fn=lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("connect_fn must not be called when the DB URL is unset")))

    assert repo.available() is False


# E. Write methods never auto-retry on a connection loss.
def test_write_method_does_not_auto_retry_on_connection_loss(monkeypatch):
    monkeypatch.setenv("EMAIL_RESEARCH_DB_URL", "postgresql://fake:fake@localhost/fake")
    dead = _FakeConn(raise_on_execute=psycopg.OperationalError("server closed the connection unexpectedly"))

    def connect_fn_must_not_be_called(url, *, autocommit=False):
        raise AssertionError("a write method must never attempt a reconnect-and-retry")

    repo = SupabaseEmailResearchRepository(dead, connect_fn=connect_fn_must_not_be_called)

    with pytest.raises(psycopg.OperationalError):
        repo.create_auto_run(workers=1, timeout=1, retries=1, search_limit=1)


# The Human Review repository is constructed from the same shared connection as Email
# Research (see email_database._bundle_for_connection) but is a separate Python object with
# its own self._conn reference, so it needs this fix applied independently to actually
# recover -- proven live during the Production smoke test, where email_research's own
# metrics healed on reconnect but email_human_review kept failing (safely, via the UI
# fail-soft warning) until this same pattern was applied here too.
def test_human_review_available_reconnects_after_dead_connection(monkeypatch):
    monkeypatch.setenv("EMAIL_RESEARCH_DB_URL", "postgresql://fake:fake@localhost/fake")
    dead = _FakeConn(raise_on_execute=psycopg.OperationalError("server closed the connection unexpectedly"))
    fresh = _FakeConn(response=(1,))
    reconnect_calls = []
    repo = SupabaseEmailHumanReviewRepository(
        dead, connect_fn=_connect_fn_returning(fresh, reconnect_calls))

    assert repo.available() is True
    assert len(reconnect_calls) == 1
    assert repo._conn is fresh


def test_human_review_read_query_retries_once_after_reconnect_then_succeeds(monkeypatch):
    monkeypatch.setenv("EMAIL_RESEARCH_DB_URL", "postgresql://fake:fake@localhost/fake")
    dead = _FakeConn(raise_on_execute=psycopg.OperationalError("server closed the connection unexpectedly"))
    fresh = _FakeConn(response=(3, 5, 2), description_names=())
    reconnect_calls = []
    repo = SupabaseEmailHumanReviewRepository(
        dead, connect_fn=_connect_fn_returning(fresh, reconnect_calls))

    out = repo.summary()
    assert out == {"available": True, "unreviewed": 3, "reviewed": 5, "unreviewed_clinics": 2}
    assert len(reconnect_calls) == 1


def test_human_review_available_returns_false_when_reconnect_fails(monkeypatch):
    monkeypatch.setenv("EMAIL_RESEARCH_DB_URL", "postgresql://fake:fake@localhost/fake")
    dead = _FakeConn(raise_on_execute=psycopg.OperationalError("server closed the connection unexpectedly"))
    repo = SupabaseEmailHumanReviewRepository(
        dead, connect_fn=_connect_fn_raising(psycopg.OperationalError("could not connect to server"), []))

    assert repo.available() is False


def test_human_review_write_method_does_not_auto_retry_on_connection_loss(monkeypatch):
    monkeypatch.setenv("EMAIL_RESEARCH_DB_URL", "postgresql://fake:fake@localhost/fake")
    dead = _FakeConn(raise_on_execute=psycopg.OperationalError("server closed the connection unexpectedly"))

    def connect_fn_must_not_be_called(url, *, autocommit=False):
        raise AssertionError("a write method must never attempt a reconnect-and-retry")

    repo = SupabaseEmailHumanReviewRepository(dead, connect_fn=connect_fn_must_not_be_called)

    with pytest.raises(psycopg.OperationalError):
        repo.save_review(candidate_id=1, human_decision="TRUE_POSITIVE", reviewer="tester")


# F. A UI fragment shows a warning instead of a raw traceback on a DB connection loss.
def test_ui_fragment_shows_warning_instead_of_traceback_on_connection_loss():
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_string('''
import streamlit as st
from types import SimpleNamespace
import src.email_research_ui as ui
import psycopg
class DeadRepo:
    def available(self):
        raise psycopg.OperationalError("the connection is lost")
orig_repo = ui.email_repositories_for_ui
ui.email_repositories_for_ui = lambda: SimpleNamespace(
    email_research=DeadRepo(), email_human_review=None)
try:
    ui.email_research_sidebar_status(None)
finally:
    ui.email_repositories_for_ui = orig_repo
''')
    app.run()
    assert not app.exception
    assert any(email_research_ui.DB_CONNECTION_LOST_MESSAGE in w.value for w in app.warning)
