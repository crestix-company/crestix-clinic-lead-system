from dataclasses import replace

from src.outreach.gas_readonly_inventory import MailState, Reconciliation
from src.outreach.gas_sheet_sync_preview import build_sheet_sync_preview

UUID = "123e4567-e89b-42d3-a456-426614174000"


def row(key="source-1", **kwargs):
    values = dict(tab="メール管理", row_number=2, case_id="GAS-1", clinic_uuid=UUID,
        clinic_id="101", operation_key="operation-1", campaign_id="campaign-1",
        recipient_email="clinic@example.jp", tracking_token="", draft_id="draft-1",
        draft_thread_id="thread-1", message_id="", sent_thread_id="thread-1",
        sheet_status="下書き作成", gmail_status=MailState.DRAFT.value, disposition="RECONCILED",
        reasons=(), source_key=key, duplicate_kinds=(), related_rows=(), holds_new_draft=True,
        gmail_evidence="fixture")
    values.update(kwargs)
    return Reconciliation(**values)


def preview(records, **kwargs):
    kwargs.setdefault("confirmed_clinic_uuids", {r.source_key: r.clinic_uuid for r in records})
    kwargs.setdefault("gmail_verified_source_keys", {r.source_key for r in records})
    kwargs.setdefault("existing_projection", {})
    return build_sheet_sync_preview(records, **kwargs)


def test_insert_update_noop_and_identity_conflict_hold():
    base = row()
    inserted, = preview([base])
    assert inserted.action == "INSERT"
    current = {"source-1": {"clinic_uuid": UUID}}
    updated, = preview([base], existing_projection=current)
    assert updated.action == "UPDATE"
    current["source-1"] = {"clinic_uuid": UUID, "gas_case_id": "GAS-1",
        "operation_key": "operation-1", "gmail_draft_id": "draft-1", "gmail_message_id": "",
        "gmail_thread_id": "thread-1", "gmail_status": "DRAFT", "source_tab": "メール管理",
        "source_row_number": "2"}
    noop, = preview([base], existing_projection=current)
    assert noop.action == "NOOP"
    conflict, = preview([base], existing_projection={"source-1": {"clinic_uuid": "other-uuid"}})
    assert conflict.action == "HOLD"


def test_unconfirmed_uuid_unverified_gmail_and_identity_conflict_hold():
    base = row()
    assert build_sheet_sync_preview([base], confirmed_clinic_uuids={},
        gmail_verified_source_keys={base.source_key}, existing_projection={})[0].action == "HOLD"
    assert build_sheet_sync_preview([base], confirmed_clinic_uuids={base.source_key: UUID},
        gmail_verified_source_keys=set(), existing_projection={})[0].action == "HOLD"
    bad = replace(base, duplicate_kinds=("IDENTIFIER_CONFLICT",))
    assert preview([bad])[0].action == "HOLD"


def test_distinct_gas_cases_remain_independent_projection_rows():
    first = row("source-a", case_id="GAS-A", draft_id="draft-a", operation_key="op-a")
    second = row("source-b", case_id="GAS-B", draft_id="draft-b", operation_key="op-b")
    result = preview([first, second])
    assert [item.action for item in result] == ["INSERT", "INSERT"]
    assert result[0].source_key != result[1].source_key


def test_sheets_projection_changed_after_preview_is_held():
    base = row()
    result, = preview([base], existing_projection={"source-1": {"clinic_uuid": UUID}},
                      expected_projection_hashes={"source-1": "snapshot-a"},
                      current_projection_hashes={"source-1": "snapshot-b"})
    assert result.action == "HOLD"
    assert "再preview" in result.reason
