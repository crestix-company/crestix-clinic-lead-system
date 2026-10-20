"""Mirrors tests/test_cloud_comdesk_import_apptest.py exactly -- see that
file's docstring for the full reasoning (AppTest limitations, env var
leak-prevention via monkeypatch) not repeated here. Since AppTest cannot
simulate an actual file upload, this covers every gating condition up to and
including the operator-identity error message / reaching the file uploader;
the writer-connection/preview/apply sequence that only runs after a real
upload remains covered by the source-level ordering tests in
test_cloud_run_maps_import_ui.py and the real-matching-logic tests in
test_maps_cloud_import.py."""
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

app_v2.cloud_maps_import_ui(_StubStore(), key_prefix="apptest")
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
    monkeypatch.setenv("CLINIC_CLOUD_MAPS_IMPORT_ENABLED", flag_enabled)
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
    assert "CLINIC_CLOUD_MAPS_IMPORT_ENABLED" in at.expander[0].caption[0].value


def test_cloud_run_deployment_with_flag_on_is_not_blocked_by_a_demo_argument(monkeypatch, _real_operator_identity):
    at = _run(
        monkeypatch, k_service="test-service", read_only_ui="false", flag_enabled="1",
        operator_email="ops@crestix-inc.com", authorized=True,
    )
    assert not at.exception
    assert len(at.expander) == 1
    assert len(at.file_uploader) == 1


def test_unauthorized_operator_is_rejected_with_a_real_rendered_error(monkeypatch, _real_operator_identity):
    at = _run(
        monkeypatch, k_service="test-service", read_only_ui="false", flag_enabled="1",
        operator_email="stranger@example.com", authorized=False,
    )
    assert not at.exception
    assert len(at.error) == 1
    assert "権限" in at.error[0].value
    assert len(at.file_uploader) == 0


def test_authorized_operator_reaches_the_file_uploader(monkeypatch, _real_operator_identity):
    at = _run(
        monkeypatch, k_service="test-service", read_only_ui="false", flag_enabled="1",
        operator_email="ops@crestix-inc.com", authorized=True,
    )
    assert not at.exception
    assert len(at.error) == 0
    assert len(at.file_uploader) == 1
    assert any("操作者" in c.value for c in at.caption)
