from src.outreach.phase_g_sheets_snapshot import (
    SheetSnapshotTab, import_action_counts, preview_import_actions, read_csv_snapshot, reconcile_mail_state,
)


def test_import_preview_preserves_source_tab_and_row_and_holds_unverified_uuid():
    tab = SheetSnapshotTab(
        "案件管理", ("clinic_id", "Clinic UUID", "案件ID"),
        (("1", "uuid-1", "case-1"), ("2", "", "case-2")), (9, 10),
    )
    actions = preview_import_actions((tab,), known_uuid_by_clinic_id={"1": "uuid-1"})
    assert [x["action"] for x in actions] == ["INSERT", "HOLD"]
    assert [(x["source_tab"], x["source_row"]) for x in actions] == [("案件管理", 9), ("案件管理", 10)]
    assert actions[0]["raw_values"] == ("1", "uuid-1", "case-1")
    assert import_action_counts(actions) == {"INSERT": 1, "UPDATE": 0, "NOOP": 0, "HOLD": 1}


def test_import_preview_holds_duplicate_history_rows_without_dropping_either():
    tab = SheetSnapshotTab(
        "メール管理", ("clinic_id", "Clinic UUID", "案件ID"),
        (("1", "uuid-1", "case-1"), ("1", "uuid-1", "case-2")), (2, 3),
    )
    actions = preview_import_actions((tab,), known_uuid_by_clinic_id={"1": "uuid-1"})
    assert [x["action"] for x in actions] == ["HOLD", "HOLD"]
    assert [x["gas_case_id"] for x in actions] == ["case-1", "case-2"]


def test_gmail_state_uses_exact_draft_and_marks_missing_draft_unknown():
    gmail = {"drafts": [{"draft_id": "draft-1"}], "searches": {"sent": [], "scheduled": [], "replied": [], "trash": []}}
    exact = reconcile_mail_state({"下書きID": "draft-1", "送信有無": "未送信"}, gmail)
    missing = reconcile_mail_state({"下書きID": "draft-gone", "送信有無": "送信済み"}, gmail)
    assert (exact["gmail_state"], exact["sheet_state"], exact["match"]) == ("DRAFT", "DRAFT", "一致")
    assert (missing["gmail_state"], missing["sheet_state"], missing["match"]) == ("UNKNOWN", "SENT", "REVIEW")


def test_gmail_state_requires_identifier_match_and_detects_replied_thread():
    gmail = {
        "drafts": [],
        "searches": {
            "sent": [{"id": "sent-id", "thread_id": "thread-1"}],
            "replied": [{"id": "reply-id", "thread_id": "thread-1"}],
            "scheduled": [], "trash": [],
        },
    }
    exact_reply = reconcile_mail_state({"送信ThreadID": "thread-1", "送信有無": "送信済み", "返信有無": "あり"}, gmail)
    wrong_thread = reconcile_mail_state({"送信ThreadID": "thread-2", "送信有無": "送信済み"}, gmail)
    assert (exact_reply["gmail_state"], exact_reply["sheet_state"], exact_reply["match"]) == ("REPLIED", "REPLIED", "一致")
    assert (wrong_thread["gmail_state"], wrong_thread["match"]) == ("UNKNOWN", "REVIEW")


def test_csv_reader_preserves_source_lines_and_quoted_multiline_cells():
    tab = read_csv_snapshot("案件管理.csv", '案件ID,本文\nCASE-1,"1行目\n2行目"\nCASE-2,本文2\n'.encode())
    assert tab.name == "案件管理"
    assert tab.headers == ("案件ID", "本文")
    assert tab.source_rows == (2, 4)
    assert tab.rows == (("CASE-1", "1行目\n2行目"), ("CASE-2", "本文2"))


def test_csv_reader_keeps_blank_source_rows():
    tab = read_csv_snapshot("all.csv", b"clinic_id,name\n1,Clinic\n\n2,Clinic2\n")
    assert tab.source_rows == (2, 3, 4)
    assert tab.rows[1] == (None, None)


def test_gmail_scheduled_and_trashed_statuses_need_exact_identifiers():
    gmail = {"drafts": [], "searches": {
        "scheduled": [{"id": "scheduled-id", "thread_id": "thread-scheduled"}],
        "trash": [{"id": "trash-id", "thread_id": "thread-trash"}],
    }}
    scheduled = reconcile_mail_state({"Scheduled Message ID": "scheduled-id", "状態": "送信予約中"}, gmail)
    trash = reconcile_mail_state({"ThreadID": "thread-trash", "状態": "下書き作成"}, gmail)
    absent = reconcile_mail_state({"下書きID": "missing", "状態": "下書き作成"}, gmail)
    assert scheduled["gmail_state"] == "SCHEDULED"
    assert trash["gmail_state"] == "TRASH"
    assert absent["gmail_state"] == "UNKNOWN"
