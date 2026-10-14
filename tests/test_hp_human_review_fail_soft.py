"""HP Human Review DB connectivity failures must fail soft, not crash the whole app.

Root cause this locks in (real production incident): ``write_repositories_for(store)``
(src/repository/write_backend.py) opens a brand new ``psycopg.connect()`` on every single
call for the Supabase write backend -- no caching, no pooling. Against the shared Supabase
Session-mode pooler, that connection attempt can transiently time out
(``psycopg.errors.ConnectionTimeout``, confirmed below to be a subclass of
``psycopg.OperationalError``) even while Supabase itself is healthy and reachable via a plain
read-only SELECT. Before this fix, that exception propagated straight out of
``_repo_for()``/``repo.available()``/``repo.summary()``/``repo.queue()`` in
src/hp_human_review_ui.py -- called unconditionally on every page render by
``hp_human_review_card()`` (a 10s auto-refreshing ``st.fragment``) and
``hp_human_review_sidebar()`` (rendered in the sidebar on every page) -- and crashed the
entire Streamlit app (Clinic Master and Email Research included), exactly matching the
production traceback:　``hp_human_review_card()`` -> ``_repo_for()`` ->
``write_repositories_for()`` -> ``build_write_repositories()`` -> ``connect()`` ->
``psycopg.connect()``.

This test module follows the idiom established in tests/test_email_db_failure_fail_soft.py
and tests/test_email_human_review_cached_reconnect_fail_soft.py (PR #82, the analogous fix
for Email Human Review): a fail-soft UI boundary catches only connectivity-shaped exceptions
(``psycopg.OperationalError``/``psycopg.InterfaceError`` -- ``ConnectionTimeout`` is a subclass
of ``OperationalError``, so no separate clause is needed) and degrades HP Human Review only,
never the whole app. The write path (``_save_decision``) is handled separately: a connectivity
failure there must never be silently treated as success.
"""
from types import SimpleNamespace

import psycopg
import pytest

import src.hp_human_review_ui as human_review_ui


# -- 0. Exception hierarchy sanity check (informs the catch clause used throughout) ----------

def test_connection_timeout_is_a_subclass_of_operational_error():
    """Confirms psycopg3's SQLSTATE class 08 hierarchy: ConnectionTimeout IS-A
    OperationalError, so catching (OperationalError, InterfaceError) already covers it --
    no redundant separate clause is needed anywhere in src/hp_human_review_ui.py.
    """
    assert issubclass(psycopg.errors.ConnectionTimeout, psycopg.OperationalError)


_SNAPSHOT = {
    "priority": "HIGH",
    "score": 80,
    "name_match": False,
    "phone_match": True,
    "address_match": True,
    "manager_match": False,
    "bucket": "PHONE_ADDRESS_NO_NAME",
    "rule_version": "v1",
    "hp_match_reason": [],
    "candidate_urls": ["https://clinic.example/"],
    "best_candidate_url": "https://clinic.example/",
    "candidates": [],
    "crawl_errors": [],
    "hp_checked_at": "2026-10-08T06:00:00+00:00",
}

_ROW = {
    "clinic_id": 1,
    "clinic_name": "Test Clinic",
    "prefecture": "東京都",
    "phone": "03-0000-0000",
    "address": "東京都千代田区1-1",
    "reviewed": False,
    "snapshot": dict(_SNAPSHOT),
    "job_id": "",
    "auto_run_id": "",
}


class _FakeRepo:
    """Healthy fake repository -- used for the "no regression" (G) assertions and as the
    base for the write-path (H) tests below.
    """

    def __init__(self, *, rows=None, summary=None, save_review_exc=None, save_review_result="review-id-1"):
        self._rows = [dict(r) for r in (rows if rows is not None else [_ROW])]
        self._summary = summary or {
            "unreviewed": 1, "reviewed": 0, "HIGH": 1, "MEDIUM": 0, "LOW": 0, "total": 1,
        }
        self._save_review_exc = save_review_exc
        self._save_review_result = save_review_result
        self.save_review_calls = 0

    def available(self):
        return True

    def summary(self):
        return self._summary

    def queue(self, **kwargs):
        limit = kwargs.get("limit")
        return self._rows if limit is None else self._rows[: max(0, int(limit))]

    def save_review(self, **kwargs):
        self.save_review_calls += 1
        if self._save_review_exc is not None:
            raise self._save_review_exc
        return self._save_review_result

    def analytics(self):
        return {
            "available": True, "reviewed": 0, "positive": 0,
            "negative": 0, "uncertain": 0, "by_bucket": [],
        }

    def training_rows(self):
        return []


def _raising_repo_for(exc):
    def _repo_for(store):
        raise exc
    return _repo_for


# -- A/B/C + D/E/F: connectivity failures on first acquisition fail soft at every UI entry
#    point, no traceback. ----------------------------------------------------------------

_CONNECTIVITY_EXCEPTIONS = [
    psycopg.errors.ConnectionTimeout("connection timeout expired"),
    psycopg.OperationalError("could not connect to server"),
    psycopg.InterfaceError("connection already closed"),
]


@pytest.mark.parametrize("exc", _CONNECTIVITY_EXCEPTIONS)
def test_sidebar_fails_soft_on_connectivity_failure(monkeypatch, exc):
    monkeypatch.setattr(human_review_ui, "_repo_for", _raising_repo_for(exc))

    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        """
import src.hp_human_review_ui as ui
ui.hp_human_review_sidebar(None)
"""
    )
    app.run()
    assert not app.exception
    assert any(human_review_ui.HP_HUMAN_REVIEW_UNAVAILABLE_MESSAGE in c.value for c in app.caption)


@pytest.mark.parametrize("exc", _CONNECTIVITY_EXCEPTIONS)
def test_card_fails_soft_on_connectivity_failure(monkeypatch, exc):
    monkeypatch.setattr(human_review_ui, "_repo_for", _raising_repo_for(exc))

    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        """
import src.hp_human_review_ui as ui
ui.hp_human_review_card(None)
"""
    )
    app.run()
    assert not app.exception
    assert any(human_review_ui.HP_HUMAN_REVIEW_UNAVAILABLE_MESSAGE in c.value for c in app.caption)


@pytest.mark.parametrize("exc", _CONNECTIVITY_EXCEPTIONS)
def test_dialog_fails_soft_on_connectivity_failure(monkeypatch, exc):
    monkeypatch.setattr(human_review_ui, "_repo_for", _raising_repo_for(exc))

    from streamlit.testing.v1 import AppTest

    # Mirrors how app_v2.py actually invokes the @st.dialog-decorated function: from inside a
    # button handler, not at the top level of the script.
    app = AppTest.from_string(
        """
import streamlit as st
import src.hp_human_review_ui as ui
if st.button("open"):
    ui.hp_human_review_dialog(None)
"""
    )
    app.run()
    assert not app.exception
    app.button[0].click()
    app.run()
    assert not app.exception
    assert any(human_review_ui.HP_HUMAN_REVIEW_UNAVAILABLE_MESSAGE in w.value for w in app.warning)


@pytest.mark.parametrize("exc", _CONNECTIVITY_EXCEPTIONS)
def test_page_fails_soft_on_connectivity_failure(monkeypatch, exc):
    monkeypatch.setattr(human_review_ui, "_repo_for", _raising_repo_for(exc))

    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        """
import src.hp_human_review_ui as ui
ui.hp_human_review_page(None)
"""
    )
    app.run()
    assert not app.exception
    assert any(human_review_ui.HP_HUMAN_REVIEW_UNAVAILABLE_MESSAGE in w.value for w in app.warning)


# -- G. Healthy DB: no regression, all entry points render exactly as before -----------------

def test_card_renders_normally_when_healthy(monkeypatch):
    monkeypatch.setattr(human_review_ui, "_repo_for", lambda store: _FakeRepo())

    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        """
import src.hp_human_review_ui as ui
ui.hp_human_review_card(None)
"""
    )
    app.run()
    assert not app.exception
    assert any("HP要確認" in m.value for m in app.markdown)
    assert not any(human_review_ui.HP_HUMAN_REVIEW_UNAVAILABLE_MESSAGE in c.value for c in app.caption)


def test_sidebar_renders_normally_when_healthy(monkeypatch):
    monkeypatch.setattr(human_review_ui, "_repo_for", lambda store: _FakeRepo())

    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        """
import src.hp_human_review_ui as ui
ui.hp_human_review_sidebar(None)
"""
    )
    app.run()
    assert not app.exception
    assert any("HP Human Review" in m.value for m in app.markdown)
    assert not any(human_review_ui.HP_HUMAN_REVIEW_UNAVAILABLE_MESSAGE in c.value for c in app.caption)


def test_dialog_renders_normally_when_healthy(monkeypatch):
    monkeypatch.setattr(human_review_ui, "_repo_for", lambda store: _FakeRepo())

    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        """
import streamlit as st
import src.hp_human_review_ui as ui
if st.button("open"):
    ui.hp_human_review_dialog(None)
"""
    )
    app.run()
    app.button[0].click()
    app.run()
    assert not app.exception
    assert any("Test Clinic" in m.value for m in app.markdown)
    assert not any(human_review_ui.HP_HUMAN_REVIEW_UNAVAILABLE_MESSAGE in w.value for w in app.warning)


# -- H. Write path: a connectivity failure during repo.save_review() must never be silently
#    treated as success. ----------------------------------------------------------------------
#
# Driven through hp_human_review_page() -> _review_tab() rather than the @st.dialog-decorated
# hp_human_review_dialog(): AppTest's bare-mode dialog simulation only renders dialog content
# for the single rerun immediately following the "open" button's click (it does not persist
# across a *second* widget interaction the way a real running Streamlit app's dialog does), so
# it cannot drive "fill in the reviewer name, then click a decision button" as two separate
# steps. _save_decision() is the single shared function behind both the dialog's and the full
# page's decision buttons (see src/hp_human_review_ui.py), so this still exercises exactly the
# code path described in the task.

def _open_review_page(repo_factory):
    import streamlit.testing.v1 as st_testing

    app = st_testing.AppTest.from_string(
        """
import src.hp_human_review_ui as ui
ui.hp_human_review_page(None)
"""
    )
    return app


@pytest.mark.parametrize("exc", [
    psycopg.OperationalError("could not connect to server"),
    psycopg.InterfaceError("connection already closed"),
])
def test_save_decision_connectivity_failure_shows_retry_not_success(monkeypatch, exc):
    repo = _FakeRepo(save_review_exc=exc)
    monkeypatch.setattr(human_review_ui, "_repo_for", lambda store: repo)

    app = _open_review_page(lambda: repo)
    app.run()
    app.text_input(key="hp_human_reviewer").input("tester")
    app.run()
    # NOT_OFFICIAL is not a Positive decision, so this isolates the failure to
    # repo.save_review() itself (reanalyze_human_verified_hp is never reached).
    app.button(key="hp_human_review_decision_1_NOT_OFFICIAL").click()
    app.run()

    assert not app.exception
    assert repo.save_review_calls == 1
    assert any(human_review_ui.HP_HUMAN_REVIEW_SAVE_FAILED_MESSAGE in e.value for e in app.error)
    # Never claims success: no success flash/toast for this click.
    assert not any("判定を保存しました" in s.value for s in app.success)


def test_save_decision_non_connectivity_bug_still_raises(monkeypatch):
    """A real programming bug inside save_review (not connectivity) must not be hidden
    behind the generic retry message -- it must still surface as a crash/visible failure."""
    repo = _FakeRepo(save_review_exc=TypeError("not a connectivity problem"))
    monkeypatch.setattr(human_review_ui, "_repo_for", lambda store: repo)

    app = _open_review_page(lambda: repo)
    app.run()
    app.text_input(key="hp_human_reviewer").input("tester")
    app.run()
    app.button(key="hp_human_review_decision_1_NOT_OFFICIAL").click()
    app.run()

    assert app.exception
    assert any("not a connectivity problem" in str(e.value) for e in app.exception)


def test_save_decision_value_error_still_shown_as_before(monkeypatch):
    """Existing business-validation ValueError behavior from repo.save_review() must be
    unchanged by this fix (it is not a connectivity error, so the new except clause must
    not intercept it)."""
    repo = _FakeRepo(save_review_exc=ValueError("HP調査結果が更新されています。"))
    monkeypatch.setattr(human_review_ui, "_repo_for", lambda store: repo)

    app = _open_review_page(lambda: repo)
    app.run()
    app.text_input(key="hp_human_reviewer").input("tester")
    app.run()
    app.button(key="hp_human_review_decision_1_NOT_OFFICIAL").click()
    app.run()

    assert not app.exception
    assert any("HP調査結果が更新されています。" in e.value for e in app.error)


def test_save_decision_healthy_path_still_saves_and_flashes(monkeypatch):
    """No-regression companion to the failure-path tests above: a healthy save_review()
    still flashes success and reruns exactly as before this fix."""
    repo = _FakeRepo()
    monkeypatch.setattr(human_review_ui, "_repo_for", lambda store: repo)

    app = _open_review_page(lambda: repo)
    app.run()
    app.text_input(key="hp_human_reviewer").input("tester")
    app.run()
    app.button(key="hp_human_review_decision_1_NOT_OFFICIAL").click()
    app.run()

    assert not app.exception
    assert repo.save_review_calls == 1
    assert not any(human_review_ui.HP_HUMAN_REVIEW_SAVE_FAILED_MESSAGE in e.value for e in app.error)


# -- I. No secret/host/DSN/credential leaks into anything rendered to the UI -----------------

_SECRET_DSN = "postgresql://runtime:s3cr3t@aws-0-ap-northeast-1.pooler.supabase.com:5432/postgres"
_SECRET_MESSAGE = (
    "connection failed: password authentication failed for user \"runtime\" "
    f"host=aws-0-ap-northeast-1.pooler.supabase.com dsn='{_SECRET_DSN}' SERPAPI_API_KEY=shhh"
)


@pytest.mark.parametrize("entry_point", ["card", "sidebar"])
def test_no_secret_leak_on_connectivity_failure(monkeypatch, entry_point):
    monkeypatch.setattr(
        human_review_ui, "_repo_for",
        _raising_repo_for(psycopg.OperationalError(_SECRET_MESSAGE)),
    )

    from streamlit.testing.v1 import AppTest

    call = f"ui.hp_human_review_{entry_point}(None)"
    app = AppTest.from_string(f"import src.hp_human_review_ui as ui\n{call}\n")
    app.run()
    assert not app.exception
    rendered = " ".join(c.value for c in app.caption)
    for leaked in (_SECRET_MESSAGE, "password", _SECRET_DSN,
                   "aws-0-ap-northeast-1.pooler.supabase.com", "SERPAPI_API_KEY", "s3cr3t"):
        assert leaked not in rendered


def test_no_secret_leak_on_write_failure(monkeypatch):
    exc = psycopg.OperationalError(_SECRET_MESSAGE)
    repo = _FakeRepo(save_review_exc=exc)
    monkeypatch.setattr(human_review_ui, "_repo_for", lambda store: repo)

    app = _open_review_page(lambda: repo)
    app.run()
    app.text_input(key="hp_human_reviewer").input("tester")
    app.run()
    app.button(key="hp_human_review_decision_1_NOT_OFFICIAL").click()
    app.run()

    assert not app.exception
    rendered = " ".join(e.value for e in app.error)
    for leaked in (_SECRET_MESSAGE, "password", _SECRET_DSN,
                   "aws-0-ap-northeast-1.pooler.supabase.com", "SERPAPI_API_KEY", "s3cr3t"):
        assert leaked not in rendered
