"""Cloud Run READ ONLY（CLINIC_READ_ONLY_UI=true）時にGmail下書き作成系の2つの
エントリポイント（手動下書き保存ボタン・自動一括保存バッチ）が確実に無効化されて
いることのソース検証。

OUTREACH_GMAIL_DRAFTS_ENABLEDは既定falseのフェイルクローズだが、それ単独に
依存すると「READ ONLY画面なのにFeature Flagの設定ミスだけでGmail書込系ボタンが
有効になる」という単一障害点になる。tests/test_cloud_run_read_only_isolation.py
がHP Job制御ボタンに適用している「write_disabled相当のread_only二重ゲート」と
同じパターンをGmail下書き系にも適用し、ここで検証する。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _app_source():
    return (ROOT / "app_v2.py").read_text(encoding="utf-8")


def _outreach_ui_source():
    return (ROOT / "src/outreach_ui.py").read_text(encoding="utf-8")


def _auto_draft_ui_source():
    return (ROOT / "src/outreach/auto_draft_ui.py").read_text(encoding="utf-8")


def test_nav_hides_outreach_pages_when_read_only():
    """Issue #130 PR #131 follow-up: nav_locked now also includes
    is_cloud_run_deployment() (any Cloud Run deployment is nav-locked
    regardless of CLINIC_READ_ONLY_UI -- see app_v2.py's write_disabled
    comment), but read_only must still be one of the ORed conditions, so
    CLINIC_READ_ONLY_UI=true alone still guarantees NAV[:2] exactly as
    before this change."""
    text = _app_source()
    assert "nav_locked = read_only or is_cloud_run_deployment() or hp_normal_runtime" in text
    assert "([NAV[0]] if hp_normal_runtime else NAV[:2]) if nav_locked else NAV" in text
    nav_start = text.index("NAV = [")
    nav_line = text[nav_start:text.index("]", nav_start) + 1]
    assert "メール送付文章" in nav_line
    assert "Email調査" in nav_line


def test_outreach_email_page_call_passes_read_only():
    text = _app_source()
    call_start = text.index("outreach_email_page(store")
    call = text[call_start:call_start + 80]
    assert "read_only=read_only" in call


def test_outreach_email_page_threads_read_only_to_both_draft_surfaces():
    text = _outreach_ui_source()
    sig_start = text.index("def outreach_email_page(")
    sig = text[sig_start:text.index(":", sig_start)]
    assert "read_only=False" in sig
    body_start = text.index(":", sig_start)
    body = text[body_start:]
    assert "_tab_results(read_only=read_only)" in body
    assert "auto_draft_save_page(demo=demo, read_only=read_only)" in body


def test_manual_draft_save_button_disabled_when_read_only():
    text = _outreach_ui_source()
    start = text.index("def _tab_results(")
    end = text.index("\ndef outreach_email_page", start)
    block = text[start:end]
    assert "read_only=False" in block[: block.index(":")]
    button_idx = block.index('key=K + "create_drafts"')
    window = block[button_idx:button_idx + 400]
    assert "disabled=(read_only or not draft_enabled" in window


def test_auto_draft_batch_can_save_excludes_read_only():
    text = _auto_draft_ui_source()
    sig_start = text.index("def auto_draft_save_page(")
    sig = text[sig_start:text.index(")", sig_start)]
    assert "read_only: bool = False" in sig
    can_save_start = text.index("can_save = (")
    can_save_end = text.index(")\n", can_save_start)
    block = text[can_save_start:can_save_end]
    assert "not read_only" in block
