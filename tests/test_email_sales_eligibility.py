import pytest
import json
from pathlib import Path

from src.master.email_human_review import true_positive_allowed
from src.master.email_sales_eligibility import (
    AUTO_FALSE, AUTO_TRUE, REVIEW, classify_email_candidate, confidence_for_candidate,
    evaluate_sales_eligibility, may_auto_primary, source_owner_type,
)


def candidate(**changes):
    value = dict(auto_identity_decision="TRUE", role="GENERAL", ownership="CLINIC_OWNED",
                 clinic_name_match=True, prefecture_match=True, city_match=True,
                 discovery_source="OFFICIAL_HP", confidence="HIGH")
    value.update(changes)
    return value


@pytest.mark.parametrize("role", ["RESERVATION", "RECRUIT", "RECRUITING", "MEDICAL_COOPERATION"])
def test_hard_block_roles_never_primary(role):
    assert not may_auto_primary(candidate(role=role))


def test_third_party_never_primary():
    assert not may_auto_primary(candidate(ownership="THIRD_PARTY_OWNED"))


@pytest.mark.parametrize("role", ["GENERAL", "OFFICE"])
def test_valid_roles_may_be_primary(role):
    assert may_auto_primary(candidate(role=role, ownership="SAME_MEDICAL_CORPORATION"))


@pytest.mark.parametrize("decision", ["REVIEW", "FALSE"])
def test_non_true_ai_never_auto_primary(decision):
    assert not may_auto_primary(candidate(auto_identity_decision=decision))


@pytest.mark.parametrize("role", ["RESERVATION", "RECRUIT"])
def test_human_true_does_not_override_hard_block(role):
    result = evaluate_sales_eligibility(**candidate(role=role), human_decision="TRUE_POSITIVE")
    assert result.final_sales_eligible is False
    assert result.sales_usable == "NO"
    assert true_positive_allowed({"role": role, "hard_block_codes": [role]}) is False


def test_human_label_is_identity_only_and_false_never_promotes():
    positive = evaluate_sales_eligibility(**candidate(auto_identity_decision="REVIEW"), human_decision="TRUE_POSITIVE")
    negative = evaluate_sales_eligibility(**candidate(), human_decision="FALSE_POSITIVE")
    assert positive.final_sales_eligible is True
    assert negative.final_sales_eligible is False


def test_confidence_thresholds_are_fixture_backed_and_independent_from_decision():
    cases = json.loads((Path(__file__).parent / "fixtures/email_confidence_cases.json").read_text())
    for case in cases:
        expected = case.pop("expected")
        case.pop("name")
        assert confidence_for_candidate(**case) == expected


def classify(**changes):
    row = dict(email="info@clinic.jp", verification_status="VERIFIED_EMAIL", role="GENERAL",
               ownership="CLINIC_OWNED", discovery_source="OFFICIAL_HP", source_url="https://clinic.jp/",
               clinic_name_match=True, prefecture_match=True, city_match=True)
    row.update(changes)
    return classify_email_candidate(row)


@pytest.mark.parametrize("role", ["RESERVATION", "RECRUIT", "RECRUITING", "MEDICAL_COOPERATION"])
def test_hard_block_roles_are_auto_false_even_when_identity_is_true(role):
    result = classify(role=role)
    assert result.auto_decision == AUTO_FALSE
    assert result.auto_identity_decision == "TRUE"
    assert result.final_sales_eligible is False


@pytest.mark.parametrize("role", ["GENERAL", "OFFICE"])
def test_valid_official_candidate_is_auto_true(role):
    assert classify(role=role).auto_decision == AUTO_TRUE


@pytest.mark.parametrize("email", ["info@city.example.lg.jp", "contact@agency.gov"])
def test_government_owned_email_is_auto_false(email):
    assert classify(email=email).auto_decision == AUTO_FALSE


def test_navii_discovery_host_is_not_mistaken_for_government_owned_email():
    result = classify(email="contact@clinic.jp", discovery_source="NAVII",
                      source_url="https://www.iryou.teikyouseido.mhlw.go.jp/clinic")
    assert result.auto_decision == AUTO_TRUE


def test_source_owner_is_distinct_from_email_owner_for_mhlw_directory():
    result = classify(email="contact@clinic.jp", discovery_source="SEARCH_QUERY",
                      source_url="https://www.iryou.teikyouseido.mhlw.go.jp/znk-web/clinic",
                      source_owner_type="TRUSTED_PUBLIC_DIRECTORY", email_ownership="CLINIC_OWNED")
    assert source_owner_type(discovery_source="SEARCH_QUERY",
                             source_url="https://www.iryou.teikyouseido.mhlw.go.jp/znk-web/clinic") == "TRUSTED_PUBLIC_DIRECTORY"
    assert result.auto_decision == AUTO_TRUE


def test_trusted_directory_fixture_fn_candidates_are_not_auto_false():
    from src.enrichment.email_research.formal_pipeline import _formal_candidate
    from src.enrichment.email_research.pipeline import EmailResultRow, STATUS_VERIFIED_EMAIL

    cases = json.loads((Path(__file__).parent / "fixtures/email_trusted_directory_false_negative_cases.json").read_text())
    for case in cases:
        row = EmailResultRow(
            clinic_id=str(case["clinic_id"]), email=case["email"], status=STATUS_VERIFIED_EMAIL,
            source_type="SEARCH_QUERY", source_url=case["source_url"], source_query="clinic email",
            official_domain="", verified_on_official=False, confidence=1.0,
            evidence_text="verified on matched public medical directory detail",
            checked_at="2026-10-09T00:00:00+00:00", parser_version="fixture",
            third_party_suspected=True,
        )
        actual = _formal_candidate(row, case["clinic"], discovery_source="SEARCH_QUERY")
        assert actual["source_owner_type"] == "TRUSTED_PUBLIC_DIRECTORY"
        assert actual["email_ownership"] == "CLINIC_OWNED"
        assert actual["auto_decision"] != AUTO_FALSE
        assert actual["hard_block_codes"] == []
        assert actual["auto_decision"] == case["expected_auto_decision"]


def test_verified_trusted_directory_does_not_erase_role_hard_block():
    from src.enrichment.email_research.formal_pipeline import _formal_candidate
    from src.enrichment.email_research.pipeline import EmailResultRow, STATUS_VERIFIED_EMAIL

    row = EmailResultRow(
        clinic_id="1", email="recruit@clinic.jp", status=STATUS_VERIFIED_EMAIL,
        source_type="SEARCH_QUERY",
        source_url="https://iryou.teikyouseido.mhlw.go.jp/znk-web/juminkanja/S2430/initialize?kikanCd=x&kikanKbn=2&prefCd=13",
        source_query="clinic email", official_domain="", verified_on_official=False,
        confidence=1.0, evidence_text="対象医院 採用担当 recruit@clinic.jp",
        checked_at="2026-10-09T00:00:00+00:00", parser_version="fixture",
        third_party_suspected=True,
    )
    result = _formal_candidate(row, {"clinic_id": 1, "clinic_name": "サンプルクリニック"}, discovery_source="SEARCH_QUERY")
    assert result["auto_identity_decision"] == "TRUE"
    assert result["auto_decision"] == AUTO_FALSE
    assert result["final_sales_eligible"] is False


def test_third_party_portal_is_auto_false():
    assert classify(email="contact@clinic.jp", source_url="https://www.byoinnavi.jp/clinic").auto_decision == AUTO_FALSE


def test_location_mismatch_is_auto_false():
    result = classify(city_match=False)
    assert result.auto_decision == AUTO_FALSE
    assert "LOCATION_IDENTITY_MISMATCH" in result.hard_block_codes


def test_unknown_ownership_personal_and_bare_rejected_stay_review():
    assert classify(ownership="OWNERSHIP_UNKNOWN").auto_decision == REVIEW
    assert classify(role="PERSONAL").auto_decision == REVIEW
    assert classify(verification_status="REJECTED").auto_decision == REVIEW


def test_rejected_with_competing_trusted_identity_stays_review():
    result = classify(verification_status="REJECTED", ownership="THIRD_PARTY_OWNED", discovery_source="NAVII")
    assert result.auto_decision == REVIEW
