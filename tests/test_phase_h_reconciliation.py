from src.outreach.phase_g_sheets_snapshot import SheetSnapshotTab, read_csv_snapshot
from src.outreach.phase_h_reconciliation import (
    classify_review_rows, import_plan_counts, plan_supabase_import,
)
from src.outreach.history import canonical_gmail_message_status


def test_review_counts_are_per_tab_and_cross_tab_union_uses_case_id():
    cases = [
        {"案件ID": "C-1", "Clinic ID": "1", "Clinic UUID": "", "送信有無": "送信済み", "元行": 2},
        {"案件ID": "C-2", "Clinic ID": "1", "Clinic UUID": "u-1", "送信有無": "下書き", "元行": 3},
    ]
    mails = [
        {"案件ID": "C-1", "Clinic ID": "1", "Clinic UUID": "", "宛先": "a@example.com", "状態": "送信済み", "元行": 2},
        {"案件ID": "C-3", "Clinic ID": "2", "Clinic UUID": "", "宛先": "b@example.com", "状態": "未取得", "元行": 3},
    ]
    report = classify_review_rows(cases, mails, {"drafts": [], "searches": {}})
    assert report["case_review_count"] == 2
    assert report["mail_review_count"] == 2
    # C-1 is present in both tables but counted once in the cross-tab union.
    assert report["unique_review_cases"] == 3
    assert report["review_rows_without_case_id"] == 0
    assert report["case_reason_counts"]["UUID不足"] == 1


def test_case_id_mismatch_requires_a_unique_identity_counterpart_not_name_only():
    case = {"案件ID": "CASE-A", "Clinic UUID": "u-1", "クリニック名": "Clinic", "状態": "下書き作成"}
    mail = {"案件ID": "CASE-B", "Clinic UUID": "u-1", "クリニック名": "Clinic", "状態": "下書き作成"}
    unrelated = {"案件ID": "CASE-C", "Clinic UUID": "u-2", "クリニック名": "Clinic", "状態": "下書き作成"}
    report = classify_review_rows([case], [mail, unrelated], {"drafts": [], "searches": {}})
    assert "案件ID不一致" in report["case_rows"][0]["reasons"]
    assert "対応先なし" in report["mail_rows"][1]["reasons"]


def test_missing_gmail_draft_is_unknown_and_review_reason_is_preserved():
    row = {"案件ID": "C-1", "Clinic UUID": "u-1", "下書きID": "gone", "状態": "下書き作成", "元行": 8}
    report = classify_review_rows([row], [], {"drafts": [], "searches": {}})
    assert report["case_rows"][0]["gmail_state"] == "UNKNOWN"
    assert "Gmail Draft ID変化・消失" in report["case_rows"][0]["reasons"]


def test_supabase_import_preview_never_claims_database_actions_and_is_repeatable():
    tab = read_csv_snapshot("案件管理.csv", "clinic_id,Clinic UUID,案件ID\n1,u-1,C-1\n".encode())
    first = plan_supabase_import((tab,), known_uuid_by_clinic_id={"1": "u-1"})
    second = plan_supabase_import((tab,), known_uuid_by_clinic_id={"1": "u-1"})
    assert first == second
    assert first[0]["action"] == "UNMATCHED_CANDIDATE"
    assert first[0]["target_compared"] is False
    assert import_plan_counts(first) == {"UNMATCHED_CANDIDATE": 1, "UNMATCHED": 0, "HOLD": 0}


def test_import_preview_classifies_logical_history_tables_without_dropping_rows():
    tab = SheetSnapshotTab("トラッキングログ", ("clinic_id", "Clinic UUID"),
                          (("1", "u-1"), ("1", "u-1")), (9, 10))
    rows = plan_supabase_import((tab,), known_uuid_by_clinic_id={"1": "u-1"})
    assert len(rows) == 2
    assert all(row["logical_table"] == "engagement_estimate" for row in rows)
    assert [row["source_row"] for row in rows] == [9, 10]


def test_gmail_unknown_history_is_held_and_never_suggests_recreation():
    tab = SheetSnapshotTab("メール管理", ("clinic_id", "Clinic UUID", "案件ID", "下書きID", "状態"),
                          (("1", "u-1", "C-1", "gone", "下書き作成"),), (4,))
    rows = plan_supabase_import((tab,), known_uuid_by_clinic_id={"1": "u-1"},
                                gmail_snapshot={"drafts": [], "searches": {}})
    assert rows[0]["action"] == "HOLD"
    assert rows[0]["gmail_state"] == "UNKNOWN"
    assert "Gmail" in rows[0]["reason"]


def test_missing_supabase_target_is_hold_and_never_confirmed_insert():
    tab = read_csv_snapshot("案件管理.csv", "clinic_id,Clinic UUID,案件ID\n1,u-1,C-1\n".encode())
    rows = plan_supabase_import((tab,), known_uuid_by_clinic_id={"1": "u-1"},
                                existing_target_tables={"email_contact_events"})
    assert rows[0]["action"] == "HOLD"
    assert rows[0]["target_table"] == "sales_cases"
    assert "比較不可" in rows[0]["reason"]


def test_gmail_trash_and_cancellation_remain_distinct_in_storage_vocabulary():
    assert canonical_gmail_message_status("TRASHED") == "TRASH"
    assert canonical_gmail_message_status("TRASH") == "TRASH"
    assert canonical_gmail_message_status("CANCELLED") == "CANCELLED"
    assert canonical_gmail_message_status("TRASH") != canonical_gmail_message_status("CANCELLED")


def test_clinic_master_uuid_is_added_only_when_missing_and_existing_uuid_is_preserved():
    cases = [
        {"案件ID": "CASE-1", "Clinic UUID": "", "状態": "下書き作成", "元行": 2},
        {"案件ID": "CASE-2", "Clinic UUID": "confirmed-old", "状態": "下書き作成", "元行": 3},
    ]
    report = classify_review_rows(
        cases, [], {"drafts": [], "searches": {}},
        known_uuid_by_gas_case_id={"CASE-1": "new-match", "CASE-2": "must-not-replace"})
    by_case = {row["gas_case_id"]: row["clinic_uuid"] for row in report["case_rows"]}
    assert by_case["CASE-1"] == "new-match"
    assert by_case["CASE-2"] == "confirmed-old"
