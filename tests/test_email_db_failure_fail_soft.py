"""Email DB / cohort failures must fail soft, not crash the whole Streamlit app.

Root cause this locks in: ``email_repositories_for_ui()`` (the UI-facing cached accessor
in src/repository/email_database.py) used to call ``build_email_repositories()`` with no
try/except at all. Any exception out of that call chain -- a bad credential
(``psycopg.OperationalError``), a dropped connection (``psycopg.InterfaceError``), or a
missing/invalid cohort file (``ValueError`` from ``email_cohort.load_cohort``) -- propagated
straight through every caller, including ``email_human_review_sidebar()``/
``email_human_review_card()`` in src/email_human_review_ui.py, which are called
unconditionally on every page render and have zero exception handling of their own. That
crashed the entire app (Clinic Master and HP Research included), not just the Email
feature.

``build_email_repositories()`` itself must keep raising exactly as before (it is
intentionally fail-closed and used directly by other tests/callers) -- only the UI
accessor gets the new try/except boundary.
"""
import psycopg
import pytest

import src.email_human_review_ui as human_review_ui
from src.repository.email_database import (
    build_email_repositories,
    clear_email_repository_cache_for_tests,
    email_repositories_for_ui,
)


PRODUCTION = "postgresql://runtime:secret@prod.example.net:5432/clinic"
EMAIL = "postgresql://email:secret@email.example.net:5432/email_state"
READONLY = "postgresql://reader:secret@prod.example.net:5432/clinic"


def _set_safe_env(monkeypatch):
    monkeypatch.setenv("EMAIL_RESEARCH_DB_URL", EMAIL)
    monkeypatch.setenv("SUPABASE_RUNTIME_DB_URL", PRODUCTION)
    monkeypatch.setenv("CLINIC_READONLY_DB_URL", READONLY)
    monkeypatch.delenv("EMAIL_RESEARCH_COHORT_FILE", raising=False)


class _FakeHumanReviewRepo:
    def __init__(self, *, unreviewed=3, reviewed=5, unreviewed_clinics=2):
        self._summary = {
            "unreviewed": unreviewed, "reviewed": reviewed, "unreviewed_clinics": unreviewed_clinics,
        }

    def available(self):
        return True

    def summary(self):
        return self._summary


class _FakeBundle:
    def __init__(self, human_review):
        self.email_human_review = human_review
        self.email_research = None


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_email_repository_cache_for_tests()
    yield
    clear_email_repository_cache_for_tests()


# -- 1. Healthy path: no regression ------------------------------------------------------

def test_email_db_healthy_card_and_sidebar_render_normally(monkeypatch):
    _set_safe_env(monkeypatch)
    fake_bundle = _FakeBundle(_FakeHumanReviewRepo())
    monkeypatch.setattr(human_review_ui, "email_repositories_for_ui", lambda: fake_bundle)

    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        """
import src.email_human_review_ui as ui
ui.email_human_review_card(None)
"""
    )
    app.run()
    assert not app.exception
    assert any("Email要確認" in m.value for m in app.markdown)

    app2 = AppTest.from_string(
        """
import src.email_human_review_ui as ui
ui.email_human_review_sidebar(None)
"""
    )
    app2.run()
    assert not app2.exception
    assert any("Email Human Review" in m.value for m in app2.markdown)


# -- 2/3. OperationalError / InterfaceError from build_email_repositories -----------------

@pytest.mark.parametrize("exc", [
    psycopg.OperationalError("could not connect to server"),
    psycopg.InterfaceError("connection already closed"),
])
def test_connection_failure_returns_none_and_ui_fails_soft(monkeypatch, exc):
    _set_safe_env(monkeypatch)

    def raising_build(**kwargs):
        raise exc

    monkeypatch.setattr(
        "src.repository.email_database.build_email_repositories", raising_build)

    assert email_repositories_for_ui() is None

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


# -- 4. Cohort file missing/invalid (ValueError from load_cohort_from_env) ---------------

def test_cohort_value_error_returns_none_and_ui_fails_soft(monkeypatch, tmp_path):
    _set_safe_env(monkeypatch)
    missing = tmp_path / "does_not_exist.csv"
    monkeypatch.setenv("EMAIL_RESEARCH_COHORT_FILE", str(missing))

    def fake_connect(url, *, autocommit=False):
        class _Conn:
            closed = False
            def cursor(self):
                class _Cur:
                    def __enter__(self): return self
                    def __exit__(self, *a): pass
                    def execute(self, *a, **k): pass
                return _Cur()
            def close(self): pass
        return _Conn()

    monkeypatch.setattr("src.repository.supabase_adapter.connect", fake_connect)

    # Confirm the raw, direct call still raises (fail-closed contract preserved).
    with pytest.raises(ValueError):
        build_email_repositories()

    # The UI accessor must not propagate it.
    assert email_repositories_for_ui() is None

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


# -- 5. Credential/auth failure specifically: no secret leakage into the UI --------------

def test_password_auth_failure_does_not_leak_secrets_to_ui(monkeypatch):
    _set_safe_env(monkeypatch)
    secret_message = (
        "connection failed: password authentication failed for user \"email\" "
        f"host=email.example.net dsn='{EMAIL}' SERPAPI_API_KEY=shhh"
    )

    def raising_build(**kwargs):
        raise psycopg.OperationalError(secret_message)

    monkeypatch.setattr(
        "src.repository.email_database.build_email_repositories", raising_build)

    assert email_repositories_for_ui() is None

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
    for leaked in (secret_message, "password", EMAIL, "email.example.net", "SERPAPI_API_KEY"):
        assert leaked not in rendered


# -- 6. Cache is not permanently poisoned by a transient failure -------------------------

def test_cache_is_not_poisoned_by_a_failure_and_recovers_on_next_call(monkeypatch):
    _set_safe_env(monkeypatch)
    calls = {"n": 0}

    class _FakeConn:
        closed = False
        def cursor(self):
            class _Cur:
                def __enter__(self): return self
                def __exit__(self, *a): pass
                def execute(self, *a, **k): pass
            return _Cur()
        def close(self): pass

    def flaky_connect(url, *, autocommit=False):
        calls["n"] += 1
        if calls["n"] == 1:
            raise psycopg.OperationalError("could not connect to server")
        return _FakeConn()

    monkeypatch.setattr("src.repository.supabase_adapter.connect", flaky_connect)

    first = email_repositories_for_ui()
    assert first is None

    second = email_repositories_for_ui()
    assert second is not None
    assert second.email_research is not None
    assert calls["n"] == 2
