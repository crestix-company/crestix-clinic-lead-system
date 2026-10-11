from src.master.email_human_review import HUMAN_DECISIONS


def test_human_review_is_append_only_and_training_rows_select_latest():
    migration = open("scripts/supabase_migration/email_human_review_schema.sql", encoding="utf-8").read().lower()
    repository = open("src/repository/email_human_review_repository.py", encoding="utf-8").read().lower()
    assert set(HUMAN_DECISIONS) == {"TRUE_POSITIVE", "FALSE_POSITIVE", "UNCERTAIN"}
    assert "grant select,insert" in migration
    assert "grant update" not in migration and "grant delete" not in migration
    assert "revoke update,delete,truncate,references,trigger on provenance.email_human_reviews" in migration
    assert "distinct on(h.candidate_id)" in repository
    assert "public.clinics" not in repository


def test_email_review_queue_and_session_state_are_independent_from_hp():
    source = open("src/email_human_review_ui.py", encoding="utf-8").read()
    assert "_open_email_human_review_dialog" in source
    assert "_open_hp_human_review_dialog" not in source
    assert "email_human_reviews" in open("src/repository/email_human_review_repository.py", encoding="utf-8").read()


def test_normal_email_review_queue_contains_review_only():
    repository = open("src/repository/email_human_review_repository.py", encoding="utf-8").read()
    assert "e.auto_decision='REVIEW'" in repository
    assert "AUTO_TRUE" not in repository and "AUTO_FALSE" not in repository


def test_email_review_button_labels_match_operator_language():
    from src.master.email_human_review import DECISION_LABELS
    assert DECISION_LABELS == {
        "TRUE_POSITIVE": "使用可（対象医院のEmail）",
        "FALSE_POSITIVE": "使用不可（対象医院のEmailではない）",
        "UNCERTAIN": "要確認",
    }


def test_review_ui_displays_candidate_and_saves_then_advances_offline():
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_string('''
import streamlit as st
import src.email_human_review_ui as ui
class FakeReviewRepo:
    def __init__(self):
        self.rows = [
            dict(clinic_id=1, clinic_uuid="uuid-1", clinic_name="Clinic One", prefecture="東京都",
                 address="東京都千代田区1", phone="03-1111-1111", hp_url="https://clinic-one.example/",
                 candidate_id=101, email="one@example.jp", auto_decision="REVIEW", confidence="MEDIUM",
                 verification_status="REVIEW", role="GENERAL", ownership="OWNERSHIP_UNKNOWN",
                 email_ownership="OWNERSHIP_UNKNOWN", source_owner_type="TRUSTED_PUBLIC_DIRECTORY",
                 clinic_name_match=True, prefecture_match=True, city_match=None, discovery_source="NAVII",
                 source_url="https://directory.example/1", search_query="", verification_reason="needs review",
                 rejection_reason="", hard_block_codes=[]),
            dict(clinic_id=2, clinic_uuid="uuid-2", clinic_name="Clinic Two", prefecture="大阪府",
                 address="大阪市中央区2", phone="06-2222-2222", hp_url="", candidate_id=102,
                 email="two@example.jp", auto_decision="REVIEW", confidence="HIGH",
                 verification_status="REVIEW", role="OFFICE", ownership="CLINIC_OWNED",
                 email_ownership="CLINIC_OWNED", source_owner_type="OFFICIAL_CLINIC",
                 clinic_name_match=True, prefecture_match=True, city_match=True, discovery_source="OFFICIAL_HP",
                 source_url="https://clinic-two.example/contact", search_query="", verification_reason="",
                 rejection_reason="", hard_block_codes=[])
        ]
        self.saved = []
    def available(self): return True
    def queue(self, **kwargs): return self.rows[:kwargs["limit"]]
    def save_review(self, **kwargs):
        self.saved.append(kwargs)
        self.rows = [r for r in self.rows if r["candidate_id"] != kwargs["candidate_id"]]
st.session_state.setdefault("fake_review_repo", FakeReviewRepo())
original_repo = ui._repo
ui._repo = lambda _store: st.session_state["fake_review_repo"]
try:
    ui.email_human_review_page(object())
finally:
    ui._repo = original_repo
''').run()
    assert any("Clinic One" in item.value for item in app.markdown)
    assert app.code[0].value == "one@example.jp"
    assert any(item.label == "確認済み公式HPを開く ↗" for item in app.get("link_button"))
    app.text_input[0].set_value("Test reviewer").run()
    next(button for button in app.button if button.label.startswith("使用不可")).click().run()
    repo = app.session_state["fake_review_repo"]
    assert repo.saved[0]["candidate_id"] == 101
    assert repo.saved[0]["human_decision"] == "FALSE_POSITIVE"
    assert any("Clinic Two" in item.value for item in app.markdown)
    assert app.code[0].value == "two@example.jp"
