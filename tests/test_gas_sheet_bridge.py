from src.outreach.gas_sheet_bridge import (
    ClinicIdentity, Decision, LegacyCase, plan_registration, registration_key, resolve_clinic,
)


UUID1 = "123e4567-e89b-42d3-a456-426614174000"
UUID2 = "123e4567-e89b-42d3-a456-426614174001"


def test_matches_verified_name_and_phone():
    case = LegacyCase("GAS-1", "あいだ内科", director_name="相田 健次", phone="03-1111-2222")
    clinic = ClinicIdentity(UUID1, "あいだ内科", director_name="相田健次", phone="0311112222")
    assert resolve_clinic(case, [clinic]).clinic_uuid == UUID1


def test_no_auto_link_from_name_and_email_alone():
    case = LegacyCase("GAS-1", "同名医院", email="info@example.jp")
    clinic = ClinicIdentity(UUID1, "同名医院", email="info@example.jp")
    assert resolve_clinic(case, [clinic]).decision == Decision.REVIEW


def test_no_auto_link_conflicting_director():
    case = LegacyCase("GAS-1", "医院", director_name="院長A", phone="03-1111-2222")
    clinic = ClinicIdentity(UUID1, "医院", director_name="院長B", phone="0311112222")
    assert resolve_clinic(case, [clinic]).decision == Decision.REVIEW


def test_ambiguous_results_require_review():
    case = LegacyCase("GAS-1", "医院", phone="0311112222")
    clinics = [ClinicIdentity(UUID1, "医院", phone="0311112222"),
               ClinicIdentity(UUID2, "医院", phone="0311112222")]
    assert resolve_clinic(case, clinics).decision == Decision.REVIEW


def test_history_unknown_fails_closed():
    plan = plan_registration(clinic_uuid=UUID1, campaign_id="cmp",
        recipient_email="info@example.jp", template_kind="LISTED",
        existing_keys=[], history_verified=False)
    assert plan.decision == Decision.BLOCKED


def test_duplicate_operation_requires_review():
    key = registration_key(clinic_uuid=UUID1, campaign_id="cmp",
        recipient_email="INFO@example.jp", template_kind="LISTED")
    plan = plan_registration(clinic_uuid=UUID1, campaign_id="cmp",
        recipient_email="info@example.jp", template_kind="LISTED",
        existing_keys=[key], history_verified=True)
    assert plan.decision == Decision.REVIEW
    assert plan.operation_key == key


def test_distinct_campaigns_have_distinct_keys():
    a = registration_key(clinic_uuid=UUID1, campaign_id="cmp-a",
        recipient_email="info@example.jp", template_kind="NOT_LISTED")
    b = registration_key(clinic_uuid=UUID1, campaign_id="cmp-b",
        recipient_email="info@example.jp", template_kind="NOT_LISTED")
    assert a != b


def test_invalid_uuid_blocks_registration():
    plan = plan_registration(clinic_uuid="not-a-uuid", campaign_id="cmp",
        recipient_email="info@example.jp", template_kind="LISTED",
        existing_keys=[], history_verified=True)
    assert plan.decision == Decision.BLOCKED
