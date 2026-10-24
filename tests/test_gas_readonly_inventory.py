"""Synthetic fixtures for the local GAS read-only inventory contract."""
from src.outreach.gas_readonly_inventory import (
    DuplicateKind, GmailObservation, MailState, build_inventory, preview_reconciliation,
    transition_state,
)


def test_inventory_preserves_id_strings_and_is_deterministic():
    tabs = {"案件管理": [{
        "案件ID": "0000123", "追跡トークン": "fixture-token-a",
        "下書きID": "draft-001", "下書きThreadID": "thread-01",
        "clinic_id": "00042", "Clinic UUID": "fixture-clinic-uuid",
    }]}
    first = build_inventory(tabs)
    second = build_inventory(tabs)
    assert first == second
    assert first[0].rows[0].values["案件ID"] == "0000123"
    assert first[0].rows[0].values["clinic_id"] == "00042"
    assert first[0].row_count == 1


def test_gmail_state_is_unknown_without_live_evidence_and_keeps_ids_distinct():
    inventory = build_inventory({"メール管理": [{
        "案件ID": "case-1", "下書きID": "draft-1",
        "下書きThreadID": "thread-1", "ThreadID": "thread-1",
        "状態": "送信済み", "送信日時": "2026-10-20 09:00:00",
    }]})
    record, = preview_reconciliation(inventory)
    assert record.case_id == "case-1"
    assert record.draft_id == "draft-1"
    assert record.draft_thread_id == "thread-1"
    assert record.sent_thread_id == "thread-1"
    assert record.message_id == ""
    assert record.sheet_status == "送信済み"
    assert record.gmail_status == "UNKNOWN"
    assert record.disposition == "REVIEW"


def test_duplicate_explicit_ids_are_reviewed_without_merging_rows():
    inventory = build_inventory({"メール管理": [
        {"案件ID": "case-1", "下書きID": "draft-1", "状態": "下書き作成"},
        {"案件ID": "case-1", "下書きID": "draft-1", "状態": "下書き作成"},
    ]})
    records = preview_reconciliation(inventory)
    assert len(records) == 2
    assert all(any("複数原票行" in reason for reason in row.reasons) for row in records)


def test_scheduled_status_is_not_inferred_from_absent_gas_fields():
    inventory = build_inventory({"メール管理": [{"案件ID": "case-2"}]})
    record, = preview_reconciliation(inventory)
    assert record.gmail_status == "UNKNOWN"
    assert any("送信予約済み" in reason for reason in record.reasons)


def test_same_source_row_reimport_is_reported_but_rows_are_not_merged():
    snapshot_a = build_inventory({"メール管理": [{"案件ID": "case-3", "下書きID": "draft-3"}]},
                                 source_id="fixture-sheet", revision="rev-a")
    snapshot_b = build_inventory({"メール管理": [{"案件ID": "case-3", "下書きID": "draft-3"}]},
                                 source_id="fixture-sheet", revision="rev-b")
    records = preview_reconciliation(snapshot_a + snapshot_b)
    assert len(records) == 2
    assert all(DuplicateKind.SAME_SOURCE_ROW_REIMPORT.value in row.duplicate_kinds for row in records)


def test_cross_format_message_id_match_does_not_use_thread_alone():
    inventory = build_inventory({
        "メール管理": [{"案件ID": "case-4", "Message ID": "msg-1", "ThreadID": "thread-1"}],
        "Gmail export": [{"Message ID": "msg-1", "Gmail Thread ID": "thread-1"}],
    })
    records = preview_reconciliation(inventory)
    assert all(DuplicateKind.SAME_EMAIL_CROSS_FORMAT.value in row.duplicate_kinds for row in records)

    thread_only = build_inventory({
        "row-a": [{"案件ID": "case-5", "ThreadID": "thread-2"}],
        "row-b": [{"案件ID": "case-6", "ThreadID": "thread-2"}],
    })
    thread_records = preview_reconciliation(thread_only)
    assert all(DuplicateKind.SAME_EMAIL_CROSS_FORMAT.value not in row.duplicate_kinds
               for row in thread_records)
    assert all(any("Thread ID共有のみ" in reason for reason in row.reasons) for row in thread_records)


def test_same_thread_distinct_message_ids_are_separate_reply_history():
    inventory = build_inventory({"Gmail export": [
        {"Clinic UUID": "uuid-a", "campaign_id": "campaign-a", "recipient_email": "clinic@example.test",
         "Message ID": "sent-1", "ThreadID": "thread-3"},
        {"Clinic UUID": "uuid-a", "campaign_id": "campaign-a", "recipient_email": "clinic@example.test",
         "Message ID": "reply-1", "ThreadID": "thread-3"},
    ]})
    records = preview_reconciliation(inventory)
    assert all(DuplicateKind.SAME_THREAD_DIFFERENT_MESSAGE.value in row.duplicate_kinds for row in records)
    assert all(row.message_id in {"sent-1", "reply-1"} for row in records)
    assert len({row.message_id for row in records}) == 2


def test_same_case_reference_across_tabs_is_not_automatically_same_email():
    inventory = build_inventory({
        "案件管理": [{"案件ID": "case-8", "Clinic UUID": "uuid-c"}],
        "メール管理": [{"案件ID": "case-8", "下書きID": "draft-8"}],
    })
    records = preview_reconciliation(inventory)
    assert all(DuplicateKind.SAME_GAS_CASE_REFERENCE.value in row.duplicate_kinds for row in records)
    assert all(DuplicateKind.SAME_EMAIL_CROSS_FORMAT.value not in row.duplicate_kinds for row in records)


def test_shared_email_id_with_conflicting_clinic_identity_is_held():
    inventory = build_inventory({"exports": [
        {"案件ID": "case-9", "Clinic UUID": "uuid-left", "Message ID": "shared-message"},
        {"案件ID": "case-9b", "Clinic UUID": "uuid-right", "Message ID": "shared-message"},
    ]})
    records = preview_reconciliation(inventory)
    assert all(DuplicateKind.IDENTIFIER_CONFLICT.value in row.duplicate_kinds for row in records)
    assert all(row.holds_new_draft for row in records)


def test_same_campaign_candidate_and_different_campaign_history_are_distinct():
    inventory = build_inventory({"history": [
        {"Clinic UUID": "uuid-b", "campaign_id": "spring", "recipient_email": "a@example.test"},
        {"Clinic UUID": "uuid-b", "campaign_id": "spring", "recipient_email": "a@example.test"},
        {"Clinic UUID": "uuid-b", "campaign_id": "autumn", "recipient_email": "a@example.test"},
    ]})
    records = preview_reconciliation(inventory)
    assert all(DuplicateKind.SAME_CLINIC_CAMPAIGN_CANDIDATE.value in row.duplicate_kinds
               for row in records[:2])
    assert all(DuplicateKind.DIFFERENT_CAMPAIGN_HISTORY.value in row.duplicate_kinds
               for row in records)


def test_verified_scheduled_observation_conflicts_with_sheet_draft_and_holds_new_draft():
    inventory = build_inventory({"メール管理": [{
        "案件ID": "case-7", "下書きID": "draft-7", "状態": "下書き作成",
    }]}, source_id="fixture-sheet")
    initial, = preview_reconciliation(inventory)
    observed, = preview_reconciliation(inventory, gmail_observations={
        initial.source_key: GmailObservation(MailState.SCHEDULED, True, "synthetic scheduled query",
                                             draft_id="draft-7", message_id="msg-7", thread_id="thread-7")
    })
    assert observed.gmail_status == MailState.SCHEDULED.value
    assert observed.disposition == "REVIEW"
    assert observed.holds_new_draft
    assert DuplicateKind.DRAFT_SCHEDULED_CONFLICT.value in observed.duplicate_kinds


def test_verified_existing_draft_never_authorizes_another_draft():
    inventory = build_inventory({"メール管理": [{
        "案件ID": "case-10", "下書きID": "draft-10", "状態": "下書き作成",
    }]}, source_id="fixture-sheet")
    initial, = preview_reconciliation(inventory)
    observed, = preview_reconciliation(inventory, gmail_observations={
        initial.source_key: GmailObservation(MailState.DRAFT, True, "synthetic drafts.get", draft_id="draft-10")
    })
    assert observed.gmail_status == MailState.DRAFT.value
    assert observed.holds_new_draft


def test_caller_observation_key_works_without_source_id():
    inventory = build_inventory({"メール管理": [{"案件ID": "case-12"}]})
    initial, = preview_reconciliation(inventory)
    observed, = preview_reconciliation(inventory, gmail_observations={
        initial.source_key: GmailObservation(MailState.SENT, True, "synthetic message lookup",
                                             message_id="msg-12", thread_id="thread-12")
    })
    assert observed.gmail_status == MailState.SENT.value


def test_draft_and_scheduled_observations_in_same_operation_are_both_held():
    inventory = build_inventory({"メール管理": [
        {"案件ID": "case-11", "Clinic UUID": "uuid-d", "campaign_id": "campaign-x",
         "recipient_email": "x@example.test", "下書きID": "draft-11"},
        {"案件ID": "case-11b", "Clinic UUID": "uuid-d", "campaign_id": "campaign-x",
         "recipient_email": "x@example.test", "下書きID": "draft-11b"},
    ]}, source_id="fixture-sheet")
    initial = preview_reconciliation(inventory)
    observations = {
        initial[0].source_key: GmailObservation(MailState.DRAFT, True, "synthetic draft", draft_id="draft-11"),
        initial[1].source_key: GmailObservation(MailState.SCHEDULED, True, "synthetic schedule",
                                                message_id="msg-11", thread_id="thread-11"),
    }
    records = preview_reconciliation(inventory, gmail_observations=observations)
    assert all(DuplicateKind.DRAFT_SCHEDULED_CONFLICT.value in row.duplicate_kinds for row in records)
    assert all(row.holds_new_draft for row in records)


def test_state_transition_requires_verified_evidence_and_blocks_regression():
    unverified = transition_state(MailState.UNKNOWN, MailState.SENT, verified=False)
    assert not unverified.accepted
    assert unverified.current == MailState.PENDING_REVIEW

    sent = transition_state(MailState.DRAFT, MailState.SENT, verified=True, evidence="fixture message id")
    assert sent.accepted and sent.current == MailState.SENT

    regression = transition_state(MailState.SENT, MailState.DRAFT, verified=True, evidence="fixture draft")
    assert not regression.accepted
    assert regression.current == MailState.PENDING_REVIEW
