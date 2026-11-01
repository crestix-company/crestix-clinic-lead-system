from pathlib import Path
from streamlit.testing.v1 import AppTest


def test_outreach_page_shows_single_call_followup_screen_with_send_disabled():
    script = Path(__file__).with_name("_outreach_app_test.py")
    script.write_text(
        "from src.outreach_ui import outreach_email_page\n"
        "outreach_email_page(object())\n",
        encoding="utf-8",
    )
    try:
        at = AppTest.from_file(str(script)).run()
    finally:
        script.unlink(missing_ok=True)

    assert not at.exception
    assert any("メール送付文章" in heading.value for heading in at.header)
    assert any("担当者" in item.label for item in at.text_input)
    assert any("メール本文" in item.label for item in at.text_area)
    send_button = next(button for button in at.button if button.label == "送信")
    assert send_button.disabled
    assert not any("①" in item.label or "②" in item.label for item in at.button)
