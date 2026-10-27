"""Issue #126 P1: ローカル版の5メニュー構成・処理順序をREAD ONLY画面でも
「存在と順序が分かる」ようにする一方で、Email調査／メール送付文章／詳細設定の
Gmail下書き・Job操作ボタンは絶対に新たな到達経路を作らないことを検証する。

tests/test_simple_workflow_cleanup.py の
test_read_only_ui_skips_startup_writes_and_auto_resume と
tests/test_outreach_read_only_isolation.py の
test_nav_hides_outreach_pages_when_read_only は、
"nav_options = NAV[:2] if nav_locked else NAV" を意図的にピン留めしている
（Gmail下書き系ボタンを含むページをnav自体から隠す、二重ゲートの一枚）。
元々はread_onlyのみで判定していたが、Issue #130 PR #131のレビューで
「CLINIC_READ_ONLY_UI=falseのCloud Runデプロイが既存の取込・HP Job・Email・
設定画面を意図せず全開放してしまう」という指摘を受け、
nav_locked = read_only or is_cloud_run_deployment() に拡張した
（app_v2.pyのwrite_disabledコメント参照）。read_only単体がNAV[:2]を
保証する性質そのものは変わらない。メニュー構成の可視化は、新しい
st.radio/st.button等の操作可能ウィジェットではなく、静的なst.caption/
st.expanderのテキストのみで行う。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _app_source():
    return (ROOT / "app_v2.py").read_text(encoding="utf-8")


def _main_source():
    source = _app_source()
    start = source.index("def main()")
    return source[start:]


def _simple_workflow_read_only_block():
    source = _app_source()
    start = source.index("def simple_workflow_ui")
    end = source.index("def simple_sales_ui", start)
    simple = source[start:end]
    ro_start = simple.index("if read_only:")
    ro_end = simple.index("return", ro_start)
    return simple[ro_start:ro_end]


def test_nav_pin_still_intact_after_menu_visibility_change():
    main = _main_source()
    assert "nav_locked = read_only or is_cloud_run_deployment() or hp_normal_runtime" in main
    assert "([NAV[0]] if hp_normal_runtime else NAV[:2]) if nav_locked else NAV" in main
    assert 'nav = st.radio("メニュー", nav_options, key="navigation")' in main


def test_sidebar_shows_full_local_menu_structure_as_static_text_only():
    main = _main_source()
    expander_start = main.index("ローカル版のメニュー構成（参考）")
    block_start = main.rindex("with st.expander(", 0, expander_start)
    block_end = main.index("else:", block_start)
    block = main[block_start:block_end]

    # Renders every NAV entry (all 5 local menu items), not a hardcoded subset.
    assert "for name in NAV:" in block
    assert "st.radio" not in block
    assert "st.button" not in block
    assert "st.file_uploader" not in block


def test_locked_menu_items_are_labeled_as_requiring_separate_authorization():
    main = _main_source()
    assert "未有効・別権限が必要" in main


def test_read_only_step_overview_is_static_text_with_no_write_calls():
    block = _simple_workflow_read_only_block()
    assert "①データ準備" in block
    assert "②Google Maps" in block
    assert "③HP・Email自動調査" in block
    assert "④営業対象検索・Comdesk出力" in block
    # Must stay read-only: no buttons, uploaders, or write-repository calls
    # were introduced in this branch.
    assert "st.button(" not in block
    assert "st.file_uploader(" not in block
    assert "write_repositories_for(" not in block


def test_population_denominators_are_distinguished_with_help_text():
    block = _simple_workflow_read_only_block()
    assert '"医院マスター（営業対象外施設を除く）"' in block
    assert "help=" in block
    assert "既存営業リスト" in block
    assert "レガシーコホート" in block or "別の件数" in block
