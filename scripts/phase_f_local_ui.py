"""Local-only Clinic Lead outreach screen with synthetic clinic and Gmail data."""
from __future__ import annotations

import os
import streamlit as st
from datetime import date

# This entry point is an isolated local harness. It cannot use an HTTP/Gmail/DB backend.
os.environ["OUTREACH_LOCAL_MOCK_MODE"] = "true"
os.environ["SHARED_DRAFT_API_ENABLED"] = "true"
os.environ["SHARED_DRAFT_API_URL"] = "mock://phase-f-local"
os.environ["SHARED_DRAFT_API_TOKEN"] = "local-mock-only"
os.environ["OUTREACH_GMAIL_DRAFTS_ENABLED"] = "false"

from src.outreach.article_adapter import ArticleLink  # noqa: E402
from src.outreach.generation import GenerationSettings, OutreachResult, READY  # noqa: E402
from src.outreach.targets import ArticleState, ClinicTarget, EMAIL_FOUND  # noqa: E402
from src.outreach_ui import outreach_email_page  # noqa: E402

st.set_page_config(page_title="Clinic Lead 営業管理 LOCAL MOCK", layout="wide")
st.title("Clinic Lead System｜ローカル営業管理")
st.error("LOCAL MOCK ONLY — Gmail・Supabase・Google Sheets・GASには接続していません。保存結果はこの画面のsession fixtureです。")
if st.button("Mock状態を初期化（現在のブラウザーsessionのみ）"):
    for key in ("outreach_local_mock_draft_records", "outreach_shared_api_results"):
        st.session_state.pop(key, None)
    st.rerun()

class MockClinicStore:
    is_supabase_runtime = False
    def query(self, *args, **kwargs):
        return []
    def prefectures(self):
        return ["東京都"]

def formal_email_page(page, after_clinic_id=None):
    return {"rows": [(901, "clinic@example.invalid")] if page == 1 else [],
            "total": 1, "page": page, "page_size": 500, "pages": 1}

settings = GenerationSettings(
    staff="Mock IS担当", dates=(date(2026, 10, 21),), generation_batch_id="phase-f-fixture-batch",
)
target = ClinicTarget(
    clinic_id=901, clinic_uuid="fixture-clinic-uuid-1", clinic_name="あおば歯科（MOCK）",
    prefecture="東京都", municipality="千代田区", departments=("歯科",), treatments=(), hp_rank="A",
    director_name="Mock 院長", email="clinic@example.invalid", email_status=EMAIL_FOUND,
    director_name_confirmed=True, clinic_address="東京都千代田区1-1",
    article=ArticleState("LISTED", listed_url="https://example.invalid/mock-article",
                         clinic_profile_url="https://example.invalid/mock-clinic"),
)
result = OutreachResult(
    target=target, status=READY, subject="【確認用】ご紹介記事のご案内",
    body="Mock正式本文です。送信は行わず、画面確認用の合成文面です。",
    listed_url="https://example.invalid/mock-article",
)
st.session_state.setdefault("outreach_results", (settings, [result]))
st.session_state.setdefault("outreach_campaign_id", "fixture-campaign")
st.session_state.setdefault("outreach_gmail_states", {901: "CLEAR"})
outreach_email_page(MockClinicStore(), demo=True, formal_email_page=formal_email_page, read_only=False)

