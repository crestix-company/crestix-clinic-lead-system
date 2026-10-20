from src.outreach.local_import_actions import plan_local_import_actions
from src.outreach.local_mock_draft_transport import LocalMockDraftTransport
from src.outreach.google_sheets_readonly_adapter import GoogleSheetsReadOnlyAdapter, TARGET_TABS


def test_mock_draft_is_idempotent_and_holds_different_operation_in_same_campaign():
    ledger = {}
    transport = LocalMockDraftTransport(ledger)
    payload = {"operationKey": "op-1", "clinicUuid": "uuid-1", "campaignId": "campaign-1"}
    kwargs = {"headers": {"Authorization": "Bearer local-mock-only"}, "timeout": 5}
    first = transport("mock://local/v1/drafts", json=payload, **kwargs).json()
    retry = transport("mock://local/v1/drafts", json=payload, **kwargs).json()
    duplicate = transport("mock://local/v1/drafts", json={**payload, "operationKey": "op-2"}, **kwargs).json()
    assert first["status"] == "DRAFT"
    assert retry == first
    assert duplicate["status"] == "HOLD"


def test_local_import_actions_hold_duplicate_identity_and_preserve_each_source_row():
    headers = ("clinic_id", "clinic_uuid", "案件ID", "campaign_id", "gmail_state")
    rows = (
        ("101", "uuid-1", "case-1", "campaign", "DRAFT"),
        ("102", "uuid-2", "case-2", "campaign", "SENT"),
        ("103", "uuid-3", "case-3", "campaign", "UNKNOWN"),
        ("104", "uuid-4", "case-4", "campaign", "DRAFT"),
        ("104", "uuid-4", "case-4", "campaign", "DRAFT"),
    )
    actions = plan_local_import_actions(
        headers, rows, source_tab="案件管理",
        known_clinics={f"uuid-{i}": {"clinic_id": str(100 + i)} for i in range(1, 5)},
        local_ledger={
            "uuid-1": {"case_id": "case-1", "campaign_id": "campaign", "gmail_state": "DRAFT"},
            "uuid-2": {"case_id": "old", "campaign_id": "campaign", "gmail_state": "DRAFT"},
        },
    )
    assert [row["action"] for row in actions] == ["NOOP", "UPDATE", "INSERT", "HOLD", "HOLD"]
    assert [row["source_row"] for row in actions] == [2, 3, 4, 5, 6]
    assert all(row["raw_values"] for row in actions)


def test_sheets_readonly_adapter_fetches_only_the_three_allowlisted_tabs_and_preserves_rows():
    calls = []
    def get_values(spreadsheet_id, a1_range):
        calls.append((spreadsheet_id, a1_range))
        return [["案件ID", "Clinic UUID"], ["GAS-1", "uuid-1"]]

    tabs = GoogleSheetsReadOnlyAdapter(get_values).read_outreach_tabs("fixture-spreadsheet")
    assert tuple(tab.tab_name for tab in tabs) == TARGET_TABS
    assert all(tab.rows[0]["案件ID"] == "GAS-1" and tab.source_rows == (2,) for tab in tabs)
    assert calls == [("fixture-spreadsheet", f"'{name}'!A:ZZ") for name in TARGET_TABS]

