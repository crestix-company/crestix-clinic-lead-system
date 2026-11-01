from pathlib import Path
from streamlit.testing.v1 import AppTest

from src.call_desk_ui import _render_clinic


def test_call_desk_renders_search_results_without_call_or_ai_simulation():
    script = Path(__file__).with_name("_call_desk_app_test.py")
    script.write_text(
        "from src.call_desk_ui import call_desk_page\n"
        "class Store:\n"
        "    def query(self, filters=None, limit=100, offset=0, as_of=None):\n"
        "        return [{'clinic_name': 'テスト医院', 'phone': '03-1234-5678', "
        "'address': '東京都', 'normalized_departments': ['内科']}]\n"
        "call_desk_page(Store())\n",
        encoding="utf-8",
    )
    try:
        at = AppTest.from_file(str(script)).run()
        at.text_input[0].set_value("テスト医院").run()
    finally:
        script.unlink(missing_ok=True)

    assert not at.exception
    assert any("メール確認待ちに追加" == button.label and button.disabled for button in at.button)


def test_call_desk_html_uses_database_values_and_safe_tel_link():
    rendered = _render_clinic({
        "clinic_name": '<script>alert("x")</script>',
        "phone": "+81 (03) 1234-5678",
        "address": "東京都千代田区",
        "normalized_departments": ["内科", "皮膚科"],
    })

    assert "&lt;script&gt;" in rendered
    assert 'href="tel:+810312345678"' in rendered
    assert "東京都千代田区" in rendered
    assert "電話番号は未登録" not in rendered
    assert "音声を認識しています" not in rendered
    assert "通話状態・録音・文字起こしは連携していません。" in rendered
