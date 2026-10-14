"""A second, narrower fail-soft gap on top of the first fix in
tests/test_email_db_failure_fail_soft.py.

That first fix only covers the *initial* ``build_email_repositories()`` call inside
``email_repositories_for_ui()``. It does not cover failures on an *already-cached*
repository -- i.e. a connection that was healthy when ``_cached_bundle`` was built, but
goes dead afterward (DB restart, network blip, pooler circuit breaker, ...) and then also
fails to reconnect.

Two concrete gaps, both in ``SupabaseEmailHumanReviewRepository``
(src/repository/email_human_review_repository.py):

1. ``available()`` called ``self._ensure_read_connection()`` *before* its own try/except.
   If the connection was already marked dead from an earlier failure and the reconnect
   attempt inside ``_ensure_read_connection()`` -> ``_reconnect_read()`` itself raised
   ``psycopg.OperationalError``/``psycopg.InterfaceError`` (DB still unreachable), that
   exception propagated straight out of ``available()`` -- a bool API -- instead of
   resolving to ``False``. Fixed directly in the repository.

2. ``summary()``/``queue()`` (both decorated with
   ``src.repository.email_research_repository._retry_closed_read_connection``) can raise
   the same way: the connection is healthy when ``available()`` is checked internally,
   then dies on the main query; the decorator's own retry-after-reconnect also calls
   ``_reconnect_read()`` with no enclosing try/except, so a reconnect failure at that
   point also propagates uncaught. Fixed at the UI boundary in
   src/email_human_review_ui.py (``_fail_soft_on_reconnect_failure``), which wraps every
   UI entry point into this repository -- ``email_human_review_card()``,
   ``email_human_review_sidebar()``, and ``_render_one()`` (used by both the review
   dialog and the full page) -- so a mid-session reconnect failure degrades Email Human
   Review only, never the whole app.

The write path (``save_review``) is handled differently on purpose: a write must never be
silently treated as success. ``_render_one()``'s button handler now catches only
connectivity-shaped exceptions around ``save_review()`` and shows a clear retry error
instead of the success flash -- it never proceeds as if the review was saved.
"""
from types import SimpleNamespace

import psycopg
import pytest

import src.email_human_review_ui as human_review_ui
from src.repository.email_human_review_repository import SupabaseEmailHumanReviewRepository


EMAIL_URL = "postgresql://email:s3cr3t@email.example.net:5432/email_state"


def _is_health_check_sql(sql: str) -> bool:
    """``available()``'s own query and ``_reconnect_read()``'s post-connect verification
    query ("SELECT to_regclass(...)" / "SELECT 1") are both "is this connection alive"
    checks, issued independently of (and sometimes nested inside) the method the test is
    actually exercising. Keying outcomes off SQL shape -- rather than a strict pop-in-order
    plan -- keeps these tests honest about which *specific* query the scenario means to
    fail, instead of being fragile to how many incidental health checks happen around it.
    """
    return "to_regclass" in sql or sql.strip() == "SELECT 1"


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, params=None):
        if _is_health_check_sql(sql):
            if not self.conn.health_check_ok:
                self.conn.closed = True
                raise self.conn.health_check_exc
            self._result = (1,)
            return
        outcome = self.conn.main_outcomes.pop(0)
        if isinstance(outcome, Exception):
            # Real psycopg marks a connection closed/broken after a fatal connection
            # error; the fake reproduces that so the dead-connection checks see it too.
            self.conn.closed = True
            raise outcome
        self._result = outcome

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._result or []

    @property
    def description(self):
        return []


class _ScriptedConn:
    """A connection whose health-check queries (to_regclass / SELECT 1) succeed or fail
    as a group (``health_check_ok``), independently of the main-query outcomes queued in
    ``main_outcomes`` (consumed in order, one per non-health-check execute() call).
    """

    def __init__(self, *, main_outcomes=(), health_check_ok=True, health_check_exc=None, closed=False):
        self.main_outcomes = list(main_outcomes)
        self.health_check_ok = health_check_ok
        self.health_check_exc = health_check_exc
        self.closed = closed
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def _connect_fn_raising(exc):
    def connect_fn(url, *, autocommit=False):
        raise exc

    return connect_fn


# -- 1. available(): a reconnect attempt on an already-dead connection that itself fails
#    must resolve to False, not raise. --------------------------------------------------

@pytest.mark.parametrize("exc", [
    psycopg.OperationalError("could not connect to server"),
    psycopg.InterfaceError("connection already closed"),
])
def test_available_returns_false_when_already_dead_and_reconnect_also_fails(monkeypatch, exc):
    monkeypatch.setenv("EMAIL_RESEARCH_DB_URL", EMAIL_URL)
    dead = _ScriptedConn(closed=True)
    repo = SupabaseEmailHumanReviewRepository(dead, connect_fn=_connect_fn_raising(exc))

    assert repo.available() is False


def test_available_still_raises_on_a_non_connectivity_reconnect_bug(monkeypatch):
    """A real bug in the reconnect path (not a connectivity error) must not be hidden."""
    monkeypatch.setenv("EMAIL_RESEARCH_DB_URL", EMAIL_URL)
    dead = _ScriptedConn(closed=True)
    repo = SupabaseEmailHumanReviewRepository(
        dead, connect_fn=_connect_fn_raising(TypeError("programming error, not connectivity")))

    with pytest.raises(TypeError):
        repo.available()


# -- 2. summary()/queue(): connection dies between the internal available() check and the
#    main query; the decorator's own reconnect-and-retry also fails. ---------------------

def test_summary_raises_at_repo_level_when_reconnect_after_failure_also_fails(monkeypatch):
    """Locks in the exact gap: at the repository level (no UI boundary), this still
    raises -- the UI boundary (tested below) is what makes it fail soft for callers."""
    monkeypatch.setenv("EMAIL_RESEARCH_DB_URL", EMAIL_URL)
    conn = _ScriptedConn(main_outcomes=[
        psycopg.OperationalError("server closed the connection unexpectedly"),  # main query dies
    ])
    repo = SupabaseEmailHumanReviewRepository(
        conn, connect_fn=_connect_fn_raising(psycopg.OperationalError("could not connect to server")))

    with pytest.raises(psycopg.OperationalError):
        repo.summary()


def test_queue_raises_at_repo_level_when_reconnect_after_failure_also_fails(monkeypatch):
    monkeypatch.setenv("EMAIL_RESEARCH_DB_URL", EMAIL_URL)
    conn = _ScriptedConn(main_outcomes=[
        psycopg.InterfaceError("connection already closed"),  # main query dies
    ])
    repo = SupabaseEmailHumanReviewRepository(
        conn, connect_fn=_connect_fn_raising(psycopg.InterfaceError("connection already closed")))

    with pytest.raises(psycopg.InterfaceError):
        repo.queue()


# -- 3. UI boundary: card / sidebar / dialog (_render_one) must not crash even though the
#    repository calls above can still raise at their own level. -------------------------

def _bundle_for(repo):
    return SimpleNamespace(email_human_review=repo, email_research=None)


@pytest.mark.parametrize("exc", [
    psycopg.OperationalError("could not connect to server"),
    psycopg.InterfaceError("connection already closed"),
])
def test_card_and_sidebar_fail_soft_when_available_reconnect_fails(monkeypatch, exc):
    monkeypatch.setenv("EMAIL_RESEARCH_DB_URL", EMAIL_URL)
    dead = _ScriptedConn(closed=True)
    repo = SupabaseEmailHumanReviewRepository(dead, connect_fn=_connect_fn_raising(exc))
    monkeypatch.setattr(human_review_ui, "email_repositories_for_ui", lambda: _bundle_for(repo))

    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        """
import src.email_human_review_ui as ui
ui.email_human_review_card(None)
"""
    )
    app.run()
    assert not app.exception
    assert any(human_review_ui.EMAIL_HUMAN_REVIEW_UNAVAILABLE_MESSAGE in c.value for c in app.caption)

    app2 = AppTest.from_string(
        """
import src.email_human_review_ui as ui
ui.email_human_review_sidebar(None)
"""
    )
    app2.run()
    assert not app2.exception
    assert any(human_review_ui.EMAIL_HUMAN_REVIEW_UNAVAILABLE_MESSAGE in c.value for c in app2.caption)


@pytest.mark.parametrize("exc", [
    psycopg.OperationalError("could not connect to server"),
    psycopg.InterfaceError("connection already closed"),
])
def test_card_and_sidebar_fail_soft_when_summary_reconnect_fails_mid_session(monkeypatch, exc):
    """The connection was healthy when the bundle was cached (available() succeeds), then
    dies during summary()'s main query, and the follow-up reconnect also fails -- the
    exact gap the first fix did not cover."""
    monkeypatch.setenv("EMAIL_RESEARCH_DB_URL", EMAIL_URL)

    def make_repo():
        conn = _ScriptedConn(main_outcomes=[exc.__class__(str(exc))])
        return SupabaseEmailHumanReviewRepository(conn, connect_fn=_connect_fn_raising(exc.__class__(str(exc))))

    monkeypatch.setattr(human_review_ui, "email_repositories_for_ui", lambda: _bundle_for(make_repo()))

    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        """
import src.email_human_review_ui as ui
ui.email_human_review_card(None)
"""
    )
    app.run()
    assert not app.exception
    assert any(human_review_ui.EMAIL_HUMAN_REVIEW_UNAVAILABLE_MESSAGE in c.value for c in app.caption)

    app2 = AppTest.from_string(
        """
import src.email_human_review_ui as ui
ui.email_human_review_sidebar(None)
"""
    )
    app2.run()
    assert not app2.exception
    assert any(human_review_ui.EMAIL_HUMAN_REVIEW_UNAVAILABLE_MESSAGE in c.value for c in app2.caption)


def test_review_dialog_render_one_fails_soft_when_queue_reconnect_fails(monkeypatch):
    monkeypatch.setenv("EMAIL_RESEARCH_DB_URL", EMAIL_URL)
    conn = _ScriptedConn(main_outcomes=[
        psycopg.OperationalError("server closed the connection unexpectedly"),  # queue's main query dies
    ])
    repo = SupabaseEmailHumanReviewRepository(
        conn, connect_fn=_connect_fn_raising(psycopg.OperationalError("could not connect to server")))
    monkeypatch.setattr(human_review_ui, "email_repositories_for_ui", lambda: _bundle_for(repo))

    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        """
import src.email_human_review_ui as ui
ui._render_one(None)
"""
    )
    app.run()
    assert not app.exception
    assert any("Email Human Reviewを利用できません。" in w.value for w in app.warning)


# -- 4. Recovery: once the DB (or the reconnect) is healthy again, Human Review becomes
#    available once more -- it must not get stuck unavailable forever. ------------------

def test_available_recovers_once_reconnect_succeeds_after_a_prior_failure(monkeypatch):
    monkeypatch.setenv("EMAIL_RESEARCH_DB_URL", EMAIL_URL)
    dead = _ScriptedConn(closed=True)
    attempts = {"n": 0}
    healthy = _ScriptedConn()

    def flaky_connect(url, *, autocommit=False):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise psycopg.OperationalError("could not connect to server")
        return healthy

    repo = SupabaseEmailHumanReviewRepository(dead, connect_fn=flaky_connect)

    assert repo.available() is False  # first attempt: reconnect fails
    assert repo.available() is True   # second attempt: DB is back, reconnect succeeds
    assert repo._conn is healthy


# -- 5. No secret/credential/DSN/host leaks into anything rendered to the UI across these
#    failure paths. ------------------------------------------------------------------------

def test_no_secret_leaks_across_cached_reconnect_failure_paths(monkeypatch):
    monkeypatch.setenv("EMAIL_RESEARCH_DB_URL", EMAIL_URL)
    secret_message = (
        "connection failed: password authentication failed for user \"email\" "
        f"host=email.example.net dsn='{EMAIL_URL}' SERPAPI_API_KEY=shhh"
    )
    dead = _ScriptedConn(closed=True)
    repo = SupabaseEmailHumanReviewRepository(
        dead, connect_fn=_connect_fn_raising(psycopg.OperationalError(secret_message)))
    monkeypatch.setattr(human_review_ui, "email_repositories_for_ui", lambda: _bundle_for(repo))

    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        """
import src.email_human_review_ui as ui
ui.email_human_review_sidebar(None)
"""
    )
    app.run()
    assert not app.exception
    rendered = " ".join(c.value for c in app.caption)
    for leaked in (secret_message, "password", EMAIL_URL, "email.example.net", "SERPAPI_API_KEY"):
        assert leaked not in rendered


# -- 6. Write path: a connectivity failure during save_review() must never be silently
#    treated as success. -----------------------------------------------------------------

_QUEUE_ROW = {
    "clinic_name": "Test Clinic", "clinic_id": 1, "clinic_uuid": "uuid-1", "candidate_id": 100,
    "prefecture": "Tokyo", "address": "Some address", "phone": "03-0000-0000", "hp_url": "",
    "email": "test@example.com", "auto_decision": "REVIEW", "confidence": "HIGH",
    "verification_status": "VERIFIED_EMAIL", "role": "GENERAL", "ownership": "CLINIC_OWNED",
    "email_ownership": None, "source_owner_type": "CLINIC", "clinic_name_match": True,
    "prefecture_match": True, "city_match": True, "discovery_source": "OFFICIAL_HP",
    "search_query": None, "verification_reason": None, "rejection_reason": None,
    "hard_block_codes": [], "source_url": None,
}


class _FakeRepoWriteFails:
    def __init__(self, exc):
        self._exc = exc
        self.save_review_calls = 0

    def available(self):
        return True

    def queue(self, **kwargs):
        return [_QUEUE_ROW]

    def save_review(self, **kwargs):
        self.save_review_calls += 1
        raise self._exc


@pytest.mark.parametrize("exc", [
    psycopg.OperationalError("could not connect to server password=shhh host=email.example.net"),
    psycopg.InterfaceError("connection already closed"),
])
def test_save_review_connectivity_failure_shows_retry_error_not_success(monkeypatch, exc):
    repo = _FakeRepoWriteFails(exc)
    monkeypatch.setattr(human_review_ui, "email_repositories_for_ui", lambda: _bundle_for(repo))

    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        """
import src.email_human_review_ui as ui
ui._render_one(None)
"""
    )
    app.run()
    app.text_input(key="email_human_reviewer").input("tester")
    app.run()
    app.button(key="email_review_100_FALSE_POSITIVE").click()
    app.run()

    assert not app.exception
    assert repo.save_review_calls == 1
    assert any(human_review_ui.EMAIL_HUMAN_REVIEW_SAVE_FAILED_MESSAGE in e.value for e in app.error)
    # Never claims success: no success flash, no success toast rendered for this click.
    assert not any("として保存しました" in s.value for s in app.success)
    # No secret leaked into the error shown to the user.
    rendered = " ".join(e.value for e in app.error)
    assert "password" not in rendered and "shhh" not in rendered and "host=" not in rendered


def test_save_review_non_connectivity_bug_still_raises(monkeypatch):
    """A real programming bug in save_review (not connectivity) must not be hidden behind
    the generic retry message."""
    repo = _FakeRepoWriteFails(ValueError("not a connectivity problem"))
    monkeypatch.setattr(human_review_ui, "email_repositories_for_ui", lambda: _bundle_for(repo))

    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        """
import src.email_human_review_ui as ui
ui._render_one(None)
"""
    )
    app.run()
    app.text_input(key="email_human_reviewer").input("tester")
    app.run()
    app.button(key="email_review_100_FALSE_POSITIVE").click()
    app.run()

    assert app.exception
    assert any("not a connectivity problem" in str(e.value) for e in app.exception)
