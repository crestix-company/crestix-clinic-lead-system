from collections import Counter

from src.outreach.local_gas_validation import _FIXTURES, _run_once


def test_ten_row_local_gas_mock_holds_real_rows_and_retries_without_duplicates():
    real_rows = [
        {
            "case_key": f"snapshot:REAL-{i:03d}", "source_kind": "LOCAL_SNAPSHOT",
            "gas_case_id": f"REAL-{i:03d}", "clinic_id": str(1000 + i),
            "clinic_uuid": f"d1234567-e89b-42d3-a456-426614174{i:03d}",
            "campaign_id": "", "clinic_name": "", "recipient_email": "",
            "source_hold_reason": "ローカル原票にキャンペーンIDがありません",
        }
        for i in range(1, 7)
    ]
    fixtures = []
    for item in _FIXTURES:
        fixture = dict(item)
        fixture.update({"case_key": "fixture:" + item["gas_case_id"], "source_kind": "FIXTURE",
                        "source_hold_reason": ""})
        fixtures.append(fixture)
    cases = real_rows + fixtures

    first = _run_once(cases, [])
    second = _run_once(cases, first)

    assert len(first) == len(second) == 10
    assert Counter(row["result_status"] for row in first) == {"MATCHED": 2, "HOLD": 8}
    assert Counter(row["result_status"] for row in second) == {"MATCHED": 2, "HOLD": 8}
    assert all(row["result_status"] == "HOLD" for row in second[:6])
    assert second[6]["gas_result_status"] == "ALREADY_REGISTERED"
    assert second[7]["reason"].startswith("同一医院・Campaign ID")
    assert second[8]["gas_result_status"] == "ALREADY_REGISTERED"
    assert second[9]["reason"] == "fixtureにキャンペーンIDがありません"
    for old, new in zip(first, second):
        assert (old["case_key"], old["result_status"], old["mock_draft_id"], old["mock_registered_case_id"]) == (
            new["case_key"], new["result_status"], new["mock_draft_id"], new["mock_registered_case_id"]
        )
    assert all(row["draft_preview"]["mode"] == "LOCAL MOCK ONLY — NOT SENT" for row in second if row["draft_preview"])
    assert len({row["mock_draft_id"] for row in second if row["mock_draft_id"]}) == 2
