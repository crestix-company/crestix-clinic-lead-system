"""Mirrors tests/test_cloud_comdesk_import_apptest.py's structure -- real
Streamlit script execution via streamlit.testing.v1.AppTest for
cloud_maps_collection_admin_ui(), covering gating up through operator
authorization (same AppTest limitations as the sibling files: no real IAP
headers, no real file upload)."""
import pytest

from streamlit.testing.v1 import AppTest

_SCRIPT = """
import app_v2
from src.cloud_run import operator_identity as _operator_identity

_operator_identity.operator_email_from_headers = lambda headers: __OPERATOR_EMAIL__
_operator_identity.operator_authorized_for_cloud_write = (
    lambda email, env=None: __AUTHORIZED__ if email == __OPERATOR_EMAIL__ else False
)

class _StubStore:
    pass

app_v2.cloud_maps_collection_admin_ui(_StubStore(), key_prefix="apptest")
"""


def _script(*, operator_email, authorized):
    return (
        _SCRIPT
        .replace("__OPERATOR_EMAIL__", repr(operator_email))
        .replace("__AUTHORIZED__", repr(authorized))
    )


@pytest.fixture
def _real_operator_identity():
    from src.cloud_run import operator_identity as module
    original_parse = module.operator_email_from_headers
    original_authorize = module.operator_authorized_for_cloud_write
    yield
    module.operator_email_from_headers = original_parse
    module.operator_authorized_for_cloud_write = original_authorize


def _run(monkeypatch, *, k_service, read_only_ui, flag_enabled, operator_email, authorized):
    monkeypatch.setenv("K_SERVICE", k_service)
    monkeypatch.setenv("CLINIC_READ_ONLY_UI", read_only_ui)
    monkeypatch.setenv("CLINIC_CLOUD_MAPS_COLLECTION_ADMIN_ENABLED", flag_enabled)
    at = AppTest.from_string(_script(operator_email=operator_email, authorized=authorized))
    at.run(timeout=30)
    return at


def test_panel_entirely_absent_under_read_only_even_on_cloud_run(monkeypatch, _real_operator_identity):
    at = _run(
        monkeypatch, k_service="test-service", read_only_ui="true", flag_enabled="1",
        operator_email="ops@crestix-inc.com", authorized=True,
    )
    assert not at.exception
    assert len(at.expander) == 0


def test_panel_entirely_absent_when_not_a_cloud_run_process(monkeypatch, _real_operator_identity):
    at = _run(
        monkeypatch, k_service="", read_only_ui="false", flag_enabled="1",
        operator_email="ops@crestix-inc.com", authorized=True,
    )
    assert not at.exception
    assert len(at.expander) == 0


def test_panel_renders_but_shows_flag_disabled_caption_when_flag_unset(monkeypatch, _real_operator_identity):
    at = _run(
        monkeypatch, k_service="test-service", read_only_ui="false", flag_enabled="0",
        operator_email="ops@crestix-inc.com", authorized=True,
    )
    assert not at.exception
    assert len(at.expander) == 1
    assert "CLINIC_CLOUD_MAPS_COLLECTION_ADMIN_ENABLED" in at.expander[0].caption[0].value


def test_unauthorized_operator_is_rejected_with_a_real_rendered_error(monkeypatch, _real_operator_identity):
    at = _run(
        monkeypatch, k_service="test-service", read_only_ui="false", flag_enabled="1",
        operator_email="stranger@example.com", authorized=False,
    )
    assert not at.exception
    assert len(at.error) == 1
    assert "権限" in at.error[0].value


def test_authorized_operator_without_writer_configured_shows_a_readable_error(monkeypatch, _real_operator_identity):
    """Since SUPABASE_MAPS_COLLECTION_WRITER_DB_URL is never set in this test
    process, an authorized operator must still see a clear fail-closed
    error, not an unhandled exception."""
    monkeypatch.delenv("SUPABASE_MAPS_COLLECTION_WRITER_DB_URL", raising=False)
    at = _run(
        monkeypatch, k_service="test-service", read_only_ui="false", flag_enabled="1",
        operator_email="ops@crestix-inc.com", authorized=True,
    )
    assert not at.exception
    assert len(at.error) == 1
    assert "SUPABASE_MAPS_COLLECTION_WRITER_DB_URL" in at.error[0].value
