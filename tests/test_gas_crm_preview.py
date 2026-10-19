"""Offline tests only: no live GAS, Gmail, CRM or DB access."""
import csv
from io import StringIO
import pytest
from src.outreach.gas_sheet_bridge import registration_key
from src.outreach.gas_crm_preview import (
    CANDIDATE_COLUMNS, PreviewInputError, preview_cases, read_csv_snapshot, to_safe_csv,
)

CLINIC = "123e4567-e89b-42d3-a456-426614174000"
ORG = "123e4567-e89b-42d3-a456-426614174001"
COMPANY = "123e4567-e89b-42d3-a456-426614174002"
CLINIC2 = "123e4567-e89b-42d3-a456-426614174003"

def candidate(**kw):
    row = dict(clinic_uuid=CLINIC, clinic_name="架空A医院", campaign_id="campaign-x",
               recipient_email="a@example.com", template_kind="LISTED", gmail_draft_id="draft-one")
    return dict(row, **kw)

def mapped(**kw):
    row = dict(clinic_uuid=CLINIC, organization_id=ORG, crm_company_id=COMPANY)
    return dict(row, **kw)

def test_no_verified_history_never_ready():
    x = preview_cases([candidate()], [], [mapped()])
    assert x[0].gas_status == "HOLD" and x[0].crm_status == "MAPPED"

def test_valid_preview_does_not_write():
    x = preview_cases([candidate()], [], [mapped()], history_verified=True)
    assert x[0].gas_status == "PREVIEW_OK"
    assert x[0].operation_key == registration_key(
        clinic_uuid=CLINIC, campaign_id="campaign-x",
        recipient_email="a@example.com", template_kind="LISTED"
    )

def test_existing_key_needs_review():
    key = registration_key(clinic_uuid=CLINIC, campaign_id="campaign-x",
                           recipient_email="a@example.com", template_kind="LISTED")
    assert preview_cases([candidate()], [{"operation_key":key}], [mapped()], history_verified=True)[0].gas_status == "REVIEW"

def test_existing_draft_needs_review():
    assert preview_cases([candidate()], [{"gmail_draft_id":"draft-one"}], [mapped()], history_verified=True)[0].gas_status == "REVIEW"

def test_missing_draft_or_bad_uuid_holds():
    x = preview_cases([candidate(clinic_uuid="not-a-uuid", gmail_draft_id="")], [], [], history_verified=True)
    assert x[0].gas_status == "HOLD" and x[0].crm_status == "HOLD"

def test_duplicate_candidate_needs_review():
    x = preview_cases([candidate(),candidate()],[],[mapped()],history_verified=True)
    assert [r.gas_status for r in x] == ["PREVIEW_OK", "REVIEW"]

def test_conflicting_crm_crosslink():
    x = preview_cases([candidate()], [], [mapped(),mapped(clinic_uuid=CLINIC2)], history_verified=True)
    assert x[0].crm_status == "REVIEW"

def test_conflicting_crm_multiple_company():
    x = preview_cases([candidate()], [], [mapped(),mapped(crm_company_id=CLINIC2)], history_verified=True)
    assert x[0].crm_status == "REVIEW"

def test_csv_validation_and_formula_safety():
    line = ",".join(CANDIDATE_COLUMNS) + "\n" + ",".join(
        [CLINIC, "=1+1", "campaign-x", "a@example.com", "LISTED", "draft-one"])
    rows = read_csv_snapshot(line.encode(), required_columns=CANDIDATE_COLUMNS)
    results = preview_cases(rows, [], [mapped()], history_verified=True)
    csv_rows = list(csv.reader(StringIO(to_safe_csv(results).decode("utf-8-sig"))))
    assert csv_rows[1][1] == "'=1+1"
    with pytest.raises(PreviewInputError):
        read_csv_snapshot(b"a,b\n1,2\n", required_columns=CANDIDATE_COLUMNS)
    with pytest.raises(PreviewInputError):
        read_csv_snapshot(b"x" * 3_000_001, required_columns=CANDIDATE_COLUMNS)
