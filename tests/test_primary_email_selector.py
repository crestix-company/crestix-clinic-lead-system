"""primary_email_selector.py のunit test。純粋関数のみ(HTTP/API接続は一切行わない)。"""
from src.enrichment.email_research.primary_email_selector import (
    EmailCandidateInput,
    select_primary_email,
)

CLINIC_ID, CLINIC_NAME = "1", "サンプルクリニック"


def _candidate(email, role, *, status="VERIFIED_EMAIL", source_type="OFFICIAL_HP",
                third_party_suspected=False, role_confidence=0.8, sales_usable="YES",
                source_url="https://clinic.example/"):
    return EmailCandidateInput(
        clinic_id=CLINIC_ID, clinic_name=CLINIC_NAME, email=email, status=status,
        source_type=source_type, source_url=source_url, verified_on_official=True,
        third_party_suspected=third_party_suspected, email_role=role,
        role_confidence=role_confidence, sales_usable=sales_usable,
    )


def test_case6_general_wins_over_recruit_and_reservation():
    candidates = [
        _candidate("info@clinic.jp", "GENERAL"),
        _candidate("recruit@clinic.jp", "RECRUIT", sales_usable="NO"),
        _candidate("yoyaku@clinic.jp", "RESERVATION", sales_usable="NO"),
    ]
    result = select_primary_email(candidates)
    assert result.primary_email == "info@clinic.jp"
    assert result.needs_review is False


def test_case7_multiple_general_picks_highest_role_confidence():
    candidates = [
        _candidate("mail@clinic.jp", "GENERAL", role_confidence=0.6),
        _candidate("info@clinic.jp", "GENERAL", role_confidence=0.92),
        _candidate("contact@clinic.jp", "GENERAL", role_confidence=0.8),
    ]
    result = select_primary_email(candidates)
    assert result.primary_email == "info@clinic.jp"


def test_case8_unknown_only_does_not_auto_promote_and_needs_review():
    candidates = [
        _candidate("zaidan@clinic.jp", "UNKNOWN", role_confidence=0.3, sales_usable="REVIEW"),
    ]
    result = select_primary_email(candidates)
    assert result.primary_email == ""
    assert result.needs_review is True


def test_case9_known_wrong_clinic_fp_never_becomes_primary():
    """既知wrong-clinic FP(status=REVIEW、VERIFIEDではない)はprimaryにならない。"""
    candidates = [
        _candidate(
            "mrkm0708@b.inet489.jp", "GENERAL", status="REVIEW", source_type="SEARCH_QUERY",
            role_confidence=0.6,
        ),
    ]
    result = select_primary_email(candidates)
    assert result.primary_email == ""
    assert result.needs_review is True


def test_case10_known_third_party_fp_never_becomes_primary():
    """既知third-party FP(third_party_suspected=True)はGENERAL役割でもprimaryにならない。"""
    candidates = [
        _candidate(
            "zaidan@jptoho.or.jp", "GENERAL", status="VERIFIED_EMAIL", source_type="SEARCH_QUERY",
            third_party_suspected=True, role_confidence=0.6,
        ),
    ]
    result = select_primary_email(candidates)
    assert result.primary_email == ""
    assert result.needs_review is True


def test_official_hp_outranks_verified_search_source_for_same_role():
    candidates = [
        _candidate("info@clinic.jp", "GENERAL", source_type="SEARCH_QUERY", role_confidence=0.92),
        _candidate("contact@clinic.jp", "GENERAL", source_type="OFFICIAL_HP", role_confidence=0.6),
    ]
    result = select_primary_email(candidates)
    assert result.primary_email == "contact@clinic.jp"  # tierが優先され、role_confidenceでは逆転しない


def test_general_outranks_office_within_same_source():
    candidates = [
        _candidate("office@clinic.jp", "OFFICE", source_type="OFFICIAL_HP"),
        _candidate("info@clinic.jp", "GENERAL", source_type="OFFICIAL_HP"),
    ]
    result = select_primary_email(candidates)
    assert result.primary_email == "info@clinic.jp"


def test_non_verified_search_candidate_role_general_is_not_eligible():
    """SEARCH_CANDIDATE(Three Gatesを通過していない)はGENERALでもprimaryにしない。"""
    candidates = [
        _candidate("info@clinic.jp", "GENERAL", status="SEARCH_CANDIDATE", source_type="SEARCH_QUERY"),
    ]
    result = select_primary_email(candidates)
    assert result.primary_email == ""
    assert result.needs_review is True


def test_personal_and_medical_cooperation_never_selected_even_if_only_options():
    candidates = [
        _candidate("doctor1@clinic.jp", "PERSONAL", role_confidence=0.9, sales_usable="REVIEW"),
        _candidate("renkei@clinic.jp", "MEDICAL_COOPERATION", role_confidence=0.9, sales_usable="NO"),
    ]
    result = select_primary_email(candidates)
    assert result.primary_email == ""
    assert result.needs_review is True


def test_original_candidates_are_not_mutated_or_dropped():
    candidates = [
        _candidate("info@clinic.jp", "GENERAL"),
        _candidate("recruit@clinic.jp", "RECRUIT", sales_usable="NO"),
    ]
    before = list(candidates)
    select_primary_email(candidates)
    assert candidates == before  # 元candidateは一切変更/削除されない
