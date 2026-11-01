import csv
import io
import stat

import pytest

from src.master.comdesk_hold_review import HoldReviewStore


def _case(**overrides):
    row = {
        "source_batch_id": "b" * 64,
        "source_row_id": "17",
        "source_row_hash": "r" * 64,
        "source_file": "project_export.csv",
        "comdesk_uuid": "COMDESK-CLINIC-7",
        "comdesk_name": "中央クリニック",
        "comdesk_phone": "03-1234-5678",
        "comdesk_address": "東京都千代田区1-2-3 4階",
        "candidate_clinic_id": "987654",
        "clinic_name": "中央クリニック",
        "clinic_phone": "0312345678",
        "clinic_address": "東京都千代田区1丁目2番3号 4階",
        "hold_reason_code": "住所差異",
        "candidate_count": 1,
        "matched_fields": "電話番号",
        "mismatched_fields": "住所",
        "uuid_conflict": "なし",
    }
    return {**row, **overrides}


def _store(tmp_path):
    return HoldReviewStore(tmp_path / "private-review" / "review.sqlite3")


def test_seed_is_idempotent_and_scopes_by_batch_row_candidate(tmp_path):
    store = _store(tmp_path)
    first = _case()
    second = _case(source_row_id="18")
    third = _case(source_batch_id="c" * 64)
    assert store.seed([first, second, third]) == 3
    assert store.seed([first, second, third]) == 0
    assert store.summary()["total_input_rows"] == 3
    assert store.summary()["total_candidate_rows"] == 3


def test_same_clinic_requires_note_and_writes_revision_history(tmp_path):
    store = _store(tmp_path)
    store.seed([_case()])
    row = store.rows()[0]
    with pytest.raises(ValueError, match="evidence note"):
        store.decide(row["review_id"], expected_revision=1, decision="SAME_CLINIC", reviewer_id="reviewer")
    saved = store.decide(row["review_id"], expected_revision=1, decision="SAME_CLINIC",
                         reviewer_id="reviewer", note="一次資料の住所と電話を確認")
    assert saved["revision"] == 2
    assert store.history(row["review_id"])[0]["review_note"] == "一次資料の住所と電話を確認"
    with pytest.raises(ValueError, match="stale revision"):
        store.decide(row["review_id"], expected_revision=1, decision="HOLD", reviewer_id="reviewer")


def test_candidate_basis_change_invalidates_old_decision_and_reopens_review(tmp_path):
    store = _store(tmp_path)
    first = _case(clinic_name="中央クリニック", clinic_address="東京都千代田区1-2-3")
    store.seed([first])
    row = store.rows()[0]
    store.decide(row["review_id"], expected_revision=1, decision="SAME_CLINIC",
                 reviewer_id="reviewer", note="公式HPの所在地を確認")
    changed = _case(clinic_name="中央クリニック分院", clinic_address="東京都千代田区1-2-3", review_id=row["review_id"])
    store.seed([changed])
    refreshed = store.rows()[0]
    assert refreshed["revision"] == 3
    assert refreshed["review_status"] == "UNREVIEWED"
    assert refreshed["reviewer_decision"] == ""
    assert store.history(row["review_id"])[-1]["reviewer_id"] == "SYSTEM_REVALIDATION"


def test_prior_review_is_suggestion_only_and_requires_exact_basis(tmp_path):
    store = _store(tmp_path)
    store.seed([_case()]); row=store.rows()[0]
    store.decide(row["review_id"], expected_revision=1, decision="DIFFERENT_CLINIC", reviewer_id="reviewer")
    matches=store.prior_reviews(source_row_hash=row["source_row_hash"], candidate_clinic_id=row["candidate_clinic_id"], review_basis_hash=row["review_basis_hash"])
    assert len(matches)==1
    assert store.prior_reviews(source_row_hash=row["source_row_hash"], candidate_clinic_id=row["candidate_clinic_id"], review_basis_hash="changed") == []


def test_multiple_candidates_cannot_both_be_confirmed_as_same_clinic(tmp_path):
    store = _store(tmp_path)
    store.seed([_case(candidate_clinic_id="101", candidate_count=2),
                _case(candidate_clinic_id="202", candidate_count=2)])
    rows=store.rows()
    store.decide(rows[0]["review_id"], expected_revision=1, decision="SAME_CLINIC",
                 reviewer_id="reviewer", note="確認根拠")
    with pytest.raises(ValueError, match="another Clinic candidate"):
        store.decide(rows[1]["review_id"], expected_revision=1, decision="SAME_CLINIC",
                     reviewer_id="reviewer", note="別候補も同一とした根拠")


def test_csv_roundtrip_validates_identity_revisions_and_records_history(tmp_path):
    store = _store(tmp_path)
    store.seed([_case(comdesk_name="=HYPERLINK(\"x\")")])
    exported = store.export_csv()
    assert exported.startswith(b"\xef\xbb\xbf")
    assert b"'=HYPERLINK" in exported
    rows = list(csv.DictReader(io.StringIO(exported.decode("utf-8-sig"), newline="")))
    rows[0]["reviewer_decision"] = "同一医院と確認"
    rows[0]["reviewer_id"] = "reviewer"
    rows[0]["review_note"] = "元データと公式住所を確認"
    data = io.StringIO(newline="")
    writer = csv.DictWriter(data, fieldnames=rows[0].keys(), quoting=csv.QUOTE_ALL)
    writer.writeheader(); writer.writerows(rows)
    preview = store.preview_csv(data.getvalue().encode("utf-8-sig"))
    assert preview["valid"] is True
    assert store.apply_preview(preview) == 1
    assert store.rows()[0]["revision"] == 2
    assert store.history(store.rows()[0]["review_id"])[0]["reviewer_id"] == "reviewer"
    repeated = store.preview_csv(store.export_csv())
    assert repeated["valid"] is True
    assert repeated["accepted"] == []
    assert len(repeated["already_applied"]) == 1
    assert store.apply_preview(repeated) == 0
    assert len(store.history(store.rows()[0]["review_id"])) == 1


@pytest.mark.parametrize("tamper", ["source_row_hash", "candidate_clinic_id", "revision", "source_batch_id"])
def test_csv_tampering_is_rejected(tmp_path, tamper):
    store = _store(tmp_path); store.seed([_case()])
    rows = list(csv.DictReader(io.StringIO(store.export_csv().decode("utf-8-sig"), newline="")))
    rows[0][tamper] = "attacker-change"
    rows[0]["reviewer_decision"] = "別医院と確認"
    rows[0]["reviewer_id"] = "reviewer"
    data = io.StringIO(newline="")
    writer = csv.DictWriter(data, fieldnames=rows[0].keys()); writer.writeheader(); writer.writerows(rows)
    preview = store.preview_csv(data.getvalue().encode())
    assert preview["valid"] is False
    assert preview["accepted"] == []


def test_csv_duplicate_review_ids_and_same_decision_note_validation(tmp_path):
    store = _store(tmp_path); store.seed([_case()])
    rows = list(csv.DictReader(io.StringIO(store.export_csv().decode("utf-8-sig"), newline="")))
    rows[0]["reviewer_decision"] = "同一医院と確認"
    rows[0]["reviewer_id"] = "reviewer"
    data = io.StringIO(newline="")
    writer = csv.DictWriter(data, fieldnames=rows[0].keys()); writer.writeheader(); writer.writerows(rows * 2)
    preview = store.preview_csv(data.getvalue().encode())
    assert preview["valid"] is False
    assert any("review_id重複" in e for e in preview["errors"])
    assert any("根拠メモ" in e for e in preview["errors"])


def test_csv_apply_is_all_or_nothing_when_revision_turns_stale(tmp_path):
    store = _store(tmp_path)
    store.seed([_case(), _case(source_row_id="18")])
    rows = list(csv.DictReader(io.StringIO(store.export_csv().decode("utf-8-sig"), newline="")))
    for row in rows:
        row["reviewer_decision"] = "別医院と確認"
        row["reviewer_id"] = "reviewer"
    data = io.StringIO(newline="")
    writer = csv.DictWriter(data, fieldnames=rows[0].keys()); writer.writeheader(); writer.writerows(rows)
    preview = store.preview_csv(data.getvalue().encode())
    store.decide(store.rows()[0]["review_id"], expected_revision=1, decision="HOLD", reviewer_id="other")
    with pytest.raises(ValueError, match="stale revision"):
        store.apply_preview(preview)
    assert sum(r["revision"] > 1 for r in store.rows()) == 1


def test_store_file_permissions_are_restricted(tmp_path):
    store = _store(tmp_path)
    store.seed([_case()])
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_streamlit_review_page_smoke(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("CLINIC_COMDESK_REVIEW_DB", str(tmp_path / "ui-private" / "review.sqlite3"))
    monkeypatch.setenv("CLINIC_COMDESK_REVIEW_REPORT_DIR", str(tmp_path / "no-report"))
    monkeypatch.setenv("CLINIC_COMDESK_REVIEW_INPUT_DIR", str(tmp_path / "no-csv"))
    def app_page():
        import streamlit as st
        from src.comdesk_hold_review_ui import render_comdesk_hold_review_page
        render_comdesk_hold_review_page()
    app = AppTest.from_function(app_page).run()
    assert not app.exception
    assert any("Comdesk HOLD確認" in e.value for e in app.title)
    assert any("レビュー対象はまだありません" in e.value for e in app.info)


def test_app_navigation_routes_to_isolated_review_before_master_store(tmp_path, monkeypatch):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("CLINIC_COMDESK_REVIEW_ENABLED", "true")
    monkeypatch.setenv("CLINIC_COMDESK_REVIEW_DB", str(tmp_path / "app-private" / "review.sqlite3"))
    monkeypatch.setenv("CLINIC_DATA_BACKEND", "supabase")
    monkeypatch.delenv("SUPABASE_RUNTIME_DB_URL", raising=False)
    app = AppTest.from_file(str(Path(__file__).parents[1] / "app_v2.py"))
    app.session_state["navigation"] = "Comdesk HOLD確認"
    app.run()
    assert not app.exception
    assert any("Comdesk HOLD確認" in e.value for e in app.title)
