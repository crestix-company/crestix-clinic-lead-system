"""Screen-level Mock API rehearsal for the existing Clinic Lead outreach page."""
from streamlit.testing.v1 import AppTest


def test_email_send_text_screen_reviews_and_saves_to_mock_api(monkeypatch):
    monkeypatch.setenv("SHARED_DRAFT_API_ENABLED", "true")
    monkeypatch.setenv("SHARED_DRAFT_API_URL", "http://mock-api.invalid")
    monkeypatch.setenv("SHARED_DRAFT_API_TOKEN", "fixture-token")
    monkeypatch.setenv("OUTREACH_GMAIL_DRAFTS_ENABLED", "false")

    script = r'''
import streamlit as st
from src.outreach_ui import outreach_email_page
from src.outreach.targets import ClinicTarget, ArticleState, EMAIL_FOUND
from src.outreach.generation import OutreachResult, GenerationSettings, READY
from datetime import date
import src.outreach_ui as ui

class MockResponse:
    status_code = 200
    def json(self):
        return {"status":"DRAFT", "draftId":"mock-draft-1", "messageId":"mock-message-1",
                "threadId":"mock-thread-1", "correlationId":"mock-correlation-1"}
def mock_post(url, **kwargs):
    assert url == "http://mock-api.invalid/v1/drafts"
    assert kwargs["json"]["clinicUuid"] == "fixture-clinic-uuid-1"
    assert kwargs["headers"]["Authorization"] == "Bearer fixture-token"
    return MockResponse()
ui.requests.post = mock_post

class Store:
    is_supabase_runtime = False
    def query(self, *args, **kwargs): return []
    def prefectures(self): return ["東京都"]

target = ClinicTarget(
    clinic_id=901, clinic_uuid="fixture-clinic-uuid-1", clinic_name="テスト医院",
    prefecture="東京都", municipality="千代田区", departments=(), treatments=(), hp_rank="A",
    director_name="確認済み院長", email="clinic@example.invalid", email_status=EMAIL_FOUND,
    director_name_confirmed=True, clinic_address="東京都千代田区1-1",
    article=ArticleState("LISTED", listed_url="https://example.invalid/article",
                         clinic_profile_url="https://example.invalid/clinic"))
result = OutreachResult(target=target, status=READY, subject="確認用件名",
                        body="確認用の正式本文です。", listed_url="https://example.invalid/article")
settings = GenerationSettings(staff="テスト担当", dates=(date(2026, 10, 21),), generation_batch_id="fixture-batch")
st.session_state["outreach_results"] = (settings, [result])
st.session_state["outreach_campaign_id"] = "fixture-campaign"
st.session_state["outreach_gmail_states"] = {901: "CLEAR"}
outreach_email_page(Store(), demo=False)
'''
    app = AppTest.from_string(script, default_timeout=30).run()
    assert not app.exception

    app.multiselect(key="outreach_draft_selection").set_value([0])
    app.run()
    app.text_input(key="outreach_reviewer").set_value("Mock UI確認者")
    for key in ("outreach_review_identity_901", "outreach_review_body_901",
                "outreach_review_article_901", "outreach_review_profile_901",
                "outreach_review_duplicate_901"):
        app.checkbox(key=key).set_value(True)
    app.text_input(key="outreach_draft_confirmation").set_value("CREATE_GMAIL_DRAFTS")
    app.run()
    assert not app.exception
    assert any(item.value == "確認用の正式本文です。" for item in app.text_area)
    assert any(item.value == "CREATE_GMAIL_DRAFTS" for item in app.text_input)
    app.button(key="outreach_create_drafts").click().run()
    assert not app.exception
    assert any("共通Draft APIの処理が終了" in item.value for item in app.success)
    saved = app.session_state["outreach_shared_api_results"]
    assert saved[0]["Draft ID"] == "mock-draft-1"
    assert saved[0]["保存結果"] == "DRAFT"
