"""Issue #130 PR #131 review point 8: "ソース文字列だけでなく、実際のStreamlit
画面遷移・実行フローを検証するテストを追加する". The other test files in this
PR verify source structure/ordering (the repo's established pattern, since
app_v2.py's full startup needs a real Supabase connection this suite doesn't
set up). This file instead drives cloud_comdesk_import_ui() through a REAL
Streamlit script run via streamlit.testing.v1.AppTest -- actual widget tree,
actual conditional rendering, not text position.

Known limitation, stated rather than silently skipped: AppTest has no
supported way to (a) inject IAP request headers (st.context.headers is tied
to a real HTTP request, which AppTest does not simulate) or (b) simulate an
actual file upload interaction. So this covers every gating condition up to
and including the operator-identity error message, using a stubbed
operator_email_from_headers so the identity/authorization branch is also
exercised with real rendering -- but not the full upload -> preview ->
confirm -> submit sequence, which remains covered by the source-level
ordering tests in test_cloud_run_comdesk_import_ui.py.

AppTest.from_string executes the script in THIS process (confirmed: a
monkeypatch applied to src.cloud_run.operator_identity before at.run() is
visible to cloud_comdesk_import_ui()'s lazy import). That means os.environ
mutations are real, process-wide mutations -- every env var this file
touches is set via pytest's monkeypatch fixture (auto-restored after each
test), never via a bare os.environ[...] = ... inside the embedded script,
to avoid leaking CLINIC_READ_ONLY_UI/K_SERVICE into unrelated tests that
run later in the same pytest session.
"""
import pytest

from streamlit.testing.v1 import AppTest

_SCRIPT = """
import app_v2
from src.cloud_run import operator_identity as _operator_identity

# cloud_comdesk_import_ui() does `from src.cloud_run.operator_identity import
# operator_email_from_headers, operator_authorized_for_cloud_write` freshly
# on every call -- patching the source module's attributes (not app_v2's,
# which never holds these names at module level) is what actually takes
# effect.
_operator_identity.operator_email_from_headers = lambda headers: __OPERATOR_EMAIL__
_operator_identity.operator_authorized_for_cloud_write = (
    lambda email, env=None: __AUTHORIZED__ if email == __OPERATOR_EMAIL__ else False
)

class _StubStore:
    pass

app_v2.cloud_comdesk_import_ui(_StubStore(), demo=__DEMO__, key_prefix="apptest")
"""


def _script(*, operator_email, authorized, demo):
    return (
        _SCRIPT
        .replace("__OPERATOR_EMAIL__", repr(operator_email))
        .replace("__AUTHORIZED__", repr(authorized))
        .replace("__DEMO__", repr(demo))
    )


@pytest.fixture
def _real_operator_identity():
    """Restores the real (non-monkeypatched) functions after each test, so a
    leaked lambda can never affect a later, unrelated test module."""
    from src.cloud_run import operator_identity as module
    original_parse = module.operator_email_from_headers
    original_authorize = module.operator_authorized_for_cloud_write
    yield
    module.operator_email_from_headers = original_parse
    module.operator_authorized_for_cloud_write = original_authorize


def _run(monkeypatch, *, k_service, read_only_ui, flag_enabled, demo, operator_email, authorized):
    monkeypatch.setenv("K_SERVICE", k_service)
    monkeypatch.setenv("CLINIC_READ_ONLY_UI", read_only_ui)
    monkeypatch.setenv("CLINIC_CLOUD_COMDESK_IMPORT_ENABLED", flag_enabled)
    at = AppTest.from_string(_script(operator_email=operator_email, authorized=authorized, demo=demo))
    at.run(timeout=30)
    return at


def test_panel_entirely_absent_under_read_only_even_on_cloud_run(monkeypatch, _real_operator_identity):
    at = _run(
        monkeypatch, k_service="test-service", read_only_ui="true", flag_enabled="1",
        demo=False, operator_email="ops@crestix-inc.com", authorized=True,
    )
    assert not at.exception
    assert len(at.expander) == 0  # the whole "Comdesk取込（Cloud Run Writer用...）" panel never renders


def test_panel_entirely_absent_when_not_a_cloud_run_process(monkeypatch, _real_operator_identity):
    at = _run(
        monkeypatch, k_service="", read_only_ui="false", flag_enabled="1",
        demo=False, operator_email="ops@crestix-inc.com", authorized=True,
    )
    assert not at.exception
    assert len(at.expander) == 0


def test_panel_renders_but_shows_flag_disabled_caption_when_flag_unset(monkeypatch, _real_operator_identity):
    at = _run(
        monkeypatch, k_service="test-service", read_only_ui="false", flag_enabled="0",
        demo=False, operator_email="ops@crestix-inc.com", authorized=True,
    )
    assert not at.exception
    assert len(at.expander) == 1
    assert "CLINIC_CLOUD_COMDESK_IMPORT_ENABLED" in at.expander[0].caption[0].value


def test_demo_mode_blocks_with_an_info_message(monkeypatch, _real_operator_identity):
    at = _run(
        monkeypatch, k_service="test-service", read_only_ui="false", flag_enabled="1",
        demo=True, operator_email="ops@crestix-inc.com", authorized=True,
    )
    assert not at.exception
    assert len(at.info) == 1
    assert "デモ" in at.info[0].value


def test_unauthorized_operator_is_rejected_with_a_real_rendered_error(monkeypatch, _real_operator_identity):
    at = _run(
        monkeypatch, k_service="test-service", read_only_ui="false", flag_enabled="1",
        demo=False, operator_email="stranger@example.com", authorized=False,
    )
    assert not at.exception
    assert len(at.error) == 1
    assert "権限" in at.error[0].value
    assert len(at.file_uploader) == 0  # never reaches the uploader


def test_authorized_operator_reaches_the_file_uploader(monkeypatch, _real_operator_identity):
    at = _run(
        monkeypatch, k_service="test-service", read_only_ui="false", flag_enabled="1",
        demo=False, operator_email="ops@crestix-inc.com", authorized=True,
    )
    assert not at.exception
    assert len(at.error) == 0
    assert len(at.file_uploader) == 1
    assert any("操作者" in c.value for c in at.caption)
