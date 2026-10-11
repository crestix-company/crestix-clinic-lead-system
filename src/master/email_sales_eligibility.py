"""Single source of truth for Email identity, confidence, and sales eligibility."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlsplit


RULE_VERSION = "email-sales-eligibility-v1"

DECISION_TRUE = "TRUE"
DECISION_REVIEW = "REVIEW"
DECISION_FALSE = "FALSE"
VALID_DECISIONS = frozenset({DECISION_TRUE, DECISION_REVIEW, DECISION_FALSE})

CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_LOW = "LOW"
VALID_CONFIDENCE = frozenset({CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW})

ROLE_GENERAL = "GENERAL"
ROLE_OFFICE = "OFFICE"
ROLE_RESERVATION = "RESERVATION"
ROLE_RECRUIT = "RECRUIT"
ROLE_RECRUITING = "RECRUITING"
ROLE_MEDICAL_COOPERATION = "MEDICAL_COOPERATION"
ROLE_PERSONAL = "PERSONAL"
ROLE_UNKNOWN = "UNKNOWN"
VALID_ROLES = frozenset({
    ROLE_GENERAL, ROLE_OFFICE, ROLE_RESERVATION, ROLE_RECRUIT, ROLE_RECRUITING,
    ROLE_MEDICAL_COOPERATION, ROLE_PERSONAL, ROLE_UNKNOWN,
})

OWNERSHIP_CLINIC = "CLINIC_OWNED"
OWNERSHIP_SAME_CORPORATION = "SAME_MEDICAL_CORPORATION"
OWNERSHIP_THIRD_PARTY = "THIRD_PARTY_OWNED"
OWNERSHIP_UNKNOWN = "OWNERSHIP_UNKNOWN"
VALID_OWNERSHIP = frozenset({
    OWNERSHIP_CLINIC, OWNERSHIP_SAME_CORPORATION,
    OWNERSHIP_THIRD_PARTY, OWNERSHIP_UNKNOWN,
})

HARD_BLOCK_ROLES = frozenset({
    ROLE_RESERVATION, ROLE_RECRUIT, ROLE_RECRUITING, ROLE_MEDICAL_COOPERATION,
})
PRIMARY_ROLES = frozenset({ROLE_GENERAL, ROLE_OFFICE})
PRIMARY_OWNERSHIP = frozenset({OWNERSHIP_CLINIC, OWNERSHIP_SAME_CORPORATION})
TRUSTED_SOURCES = frozenset({"OFFICIAL_HP", "NAVII"})
AUTO_TRUE = "AUTO_TRUE"
AUTO_FALSE = "AUTO_FALSE"
REVIEW = "REVIEW"
VALID_AUTO_DECISIONS = frozenset({AUTO_TRUE, AUTO_FALSE, REVIEW})
_THIRD_PARTY_HOSTS = frozenset({
    "indeed.com", "job-medley.com", "en-gage.net", "doda.jp", "townwork.net",
    "airwork.net", "doctorsfile.jp", "caloo.jp", "病院なび.jp", "byoinnavi.jp",
    "medicaldoc.jp", "epark.jp", "wixsite.com", "jimdo.com", "amebaownd.com",
})
TRUSTED_PUBLIC_DIRECTORY_HOSTS = frozenset({
    "iryou.teikyouseido.mhlw.go.jp",
})
SOURCE_OWNER_TYPES = frozenset({
    "OFFICIAL_CLINIC", "TRUSTED_PUBLIC_DIRECTORY", "THIRD_PARTY_PORTAL",
    "GOVERNMENT", "ASSOCIATION", "VENDOR", "UNKNOWN",
})
_ENTITY_REJECTION_MARKERS = (
    "自治体", "市役所", "県庁", "行政機関", "学会", "協会", "求人サイト",
    "ポータル運営", "portal operator", "vendor", "広告会社", "ec事業者",
    "別医院", "別法人",
)

# Clinic_id=99522 incident (Production Canary 100, 2026-10-11): あむデンタルクリニック's
# Primary resolved to kaigoserv@city.ashibetsu.hokkaido.jp (芦別市 介護高齢課), a municipal
# government contact, not the clinic's own. The pre-existing government-domain check only
# covered the dedicated ".lg.jp"/".go.jp" TLDs; this host uses the common Japanese municipal
# naming convention city.<name>.<prefecture>.jp, which has no .lg.jp/.go.jp suffix. These two
# signals are therefore combined: a public.clinics.hp_url being registered is not itself
# evidence of ownership (Source Owner != Email Owner) -- the page/mailbox host must be
# evaluated independently of discovery_source=="OFFICIAL_HP".
_GOVERNMENT_HOST_SUFFIXES = (".lg.jp", ".go.jp", ".gov", ".gov.uk")
_MUNICIPAL_HOST_PREFIX_RE = re.compile(r"^(?:city|town|vill|pref)\.[^.]+\.")
# A target Clinic that is itself municipally/publicly operated may legitimately share a
# government/municipal domain; only that specific clinic's own candidates are exempted.
_PUBLIC_MUNICIPAL_CLINIC_NAME_RE = re.compile(
    "市立|町立|村立|区立|県立|都立|府立|道立|国立|公立|国民健康保険"
)


def is_government_host(host: Any) -> bool:
    """True for government/municipal domains, including Japan's common
    city.<name>.<prefecture>.jp / town.*.jp / vill.*.jp / pref.*.jp pattern
    (which does not necessarily use the dedicated .lg.jp TLD)."""
    text = str(host or "").strip().lower()
    if not text:
        return False
    if text.endswith(_GOVERNMENT_HOST_SUFFIXES):
        return True
    return text.endswith(".jp") and bool(_MUNICIPAL_HOST_PREFIX_RE.match(text))


def is_public_municipal_clinic(clinic_name: Any) -> bool:
    """True when the target Clinic itself is self-identified as municipally/publicly
    operated (市立/町立/村立/県立/公立/国民健康保険 etc.) -- the one case where a
    government/municipal domain may legitimately be the Clinic's own."""
    return bool(_PUBLIC_MUNICIPAL_CLINIC_NAME_RE.search(str(clinic_name or "")))


@dataclass(frozen=True)
class EligibilityResult:
    role: str
    ownership: str
    identity_decision: str
    confidence: str
    sales_usable: str
    hard_block_codes: tuple[str, ...]
    final_sales_eligible: bool
    rule_version: str = RULE_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "ownership": self.ownership,
            "auto_identity_decision": self.identity_decision,
            "confidence": self.confidence,
            "sales_usable": self.sales_usable,
            "hard_block_codes": list(self.hard_block_codes),
            "final_sales_eligible": self.final_sales_eligible,
            "rule_version": self.rule_version,
        }


def normalize_role(value: Any) -> str:
    raw = str(value or "").strip().upper().replace("-", "_")
    aliases = {
        "": ROLE_UNKNOWN,
        "OTHER": ROLE_UNKNOWN,
        "RECRUITMENT": ROLE_RECRUITING,
        "JOB": ROLE_RECRUIT,
        "APPOINTMENT": ROLE_RESERVATION,
    }
    normalized = aliases.get(raw, raw)
    return normalized if normalized in VALID_ROLES else ROLE_UNKNOWN


def normalize_ownership(value: Any, *, same_medical_corporation: bool = False) -> str:
    if same_medical_corporation:
        return OWNERSHIP_SAME_CORPORATION
    raw = str(value or "").strip().upper().replace("-", "_")
    aliases = {
        "": OWNERSHIP_UNKNOWN,
        "MATCH": OWNERSHIP_CLINIC,
        "MISMATCH": OWNERSHIP_THIRD_PARTY,
        "REVIEW": OWNERSHIP_UNKNOWN,
        "UNKNOWN": OWNERSHIP_UNKNOWN,
    }
    normalized = aliases.get(raw, raw)
    return normalized if normalized in VALID_OWNERSHIP else OWNERSHIP_UNKNOWN


def normalize_decision(value: Any) -> str:
    raw = str(value or "").strip().upper()
    aliases = {
        "VERIFIED": DECISION_TRUE,
        "VERIFIED_EMAIL": DECISION_TRUE,
        "SEARCH_CANDIDATE": DECISION_REVIEW,
        "UNCERTAIN": DECISION_REVIEW,
        "REJECTED": DECISION_FALSE,
        "REJECTED_THIRD_PARTY": DECISION_FALSE,
    }
    decision = aliases.get(raw, raw)
    return decision if decision in VALID_DECISIONS else DECISION_REVIEW


def human_effective_decision(auto_decision: Any, human_decision: Any = "") -> str:
    human = str(human_decision or "").strip().upper()
    if human == "TRUE_POSITIVE":
        return DECISION_TRUE
    if human == "FALSE_POSITIVE":
        return DECISION_FALSE
    if human == "UNCERTAIN":
        return DECISION_REVIEW
    return normalize_decision(auto_decision)


def confidence_for_candidate(
    *,
    clinic_name_match: bool | None,
    prefecture_match: bool | None,
    city_match: bool | None,
    discovery_source: str,
    role: Any,
    ownership: Any,
) -> str:
    """Evidence strength only; deliberately independent from TRUE/REVIEW/FALSE."""
    normalized_role = normalize_role(role)
    normalized_ownership = normalize_ownership(ownership)
    identity_strong = all(value is True for value in (
        clinic_name_match, prefecture_match, city_match,
    ))
    source_strong = str(discovery_source or "").strip().upper() in TRUSTED_SOURCES
    role_clear = normalized_role != ROLE_UNKNOWN
    ownership_clear = normalized_ownership != OWNERSHIP_UNKNOWN
    if identity_strong and source_strong and role_clear and ownership_clear:
        return CONFIDENCE_HIGH
    positive_signals = sum((
        clinic_name_match is True,
        prefecture_match is True,
        city_match is True,
        source_strong,
        role_clear,
        ownership_clear,
    ))
    if positive_signals >= 4 and clinic_name_match is not False:
        return CONFIDENCE_MEDIUM
    return CONFIDENCE_LOW


def hard_block_codes(
    *,
    role: Any,
    ownership: Any,
    clinic_name_match: bool | None,
    prefecture_match: bool | None,
    city_match: bool | None,
    extra_codes: Iterable[str] = (),
) -> tuple[str, ...]:
    normalized_role = normalize_role(role)
    normalized_ownership = normalize_ownership(ownership)
    codes = {str(code).strip().upper() for code in extra_codes if str(code).strip()}
    if normalized_role in HARD_BLOCK_ROLES:
        codes.add(normalized_role)
    if normalized_ownership == OWNERSHIP_THIRD_PARTY:
        codes.add(OWNERSHIP_THIRD_PARTY)
    if clinic_name_match is False:
        codes.add("CLINIC_IDENTITY_MISMATCH")
    if prefecture_match is False or city_match is False:
        codes.add("LOCATION_IDENTITY_MISMATCH")
    return tuple(sorted(codes))


def evaluate_sales_eligibility(
    *,
    auto_identity_decision: Any,
    role: Any,
    ownership: Any,
    clinic_name_match: bool | None,
    prefecture_match: bool | None,
    city_match: bool | None,
    discovery_source: str = "",
    confidence: Any = "",
    human_decision: Any = "",
    extra_hard_block_codes: Iterable[str] = (),
) -> EligibilityResult:
    normalized_role = normalize_role(role)
    normalized_ownership = normalize_ownership(ownership)
    identity_decision = human_effective_decision(auto_identity_decision, human_decision)
    normalized_confidence = str(confidence or "").strip().upper()
    if normalized_confidence not in VALID_CONFIDENCE:
        normalized_confidence = confidence_for_candidate(
            clinic_name_match=clinic_name_match,
            prefecture_match=prefecture_match,
            city_match=city_match,
            discovery_source=discovery_source,
            role=normalized_role,
            ownership=normalized_ownership,
        )
    blocks = hard_block_codes(
        role=normalized_role,
        ownership=normalized_ownership,
        clinic_name_match=clinic_name_match,
        prefecture_match=prefecture_match,
        city_match=city_match,
        extra_codes=extra_hard_block_codes,
    )
    eligible = bool(
        identity_decision == DECISION_TRUE
        and normalized_role in PRIMARY_ROLES
        and normalized_ownership in PRIMARY_OWNERSHIP
        and clinic_name_match is True
        and prefecture_match is True
        and city_match is True
        and not blocks
    )
    if eligible:
        sales_usable = "YES"
    elif blocks or identity_decision == DECISION_FALSE:
        sales_usable = "NO"
    else:
        sales_usable = "REVIEW"
    return EligibilityResult(
        role=normalized_role,
        ownership=normalized_ownership,
        identity_decision=identity_decision,
        confidence=normalized_confidence,
        sales_usable=sales_usable,
        hard_block_codes=blocks,
        final_sales_eligible=eligible,
    )


def may_auto_primary(candidate: dict, *, minimum_confidence: str | None = None) -> bool:
    """Production threshold is configurable; eligibility rules can never be bypassed."""
    if candidate.get("auto_decision", AUTO_TRUE) != AUTO_TRUE:
        return False
    result = evaluate_sales_eligibility(
        auto_identity_decision=candidate.get("auto_identity_decision"),
        role=candidate.get("role"),
        ownership=candidate.get("ownership"),
        clinic_name_match=candidate.get("clinic_name_match"),
        prefecture_match=candidate.get("prefecture_match"),
        city_match=candidate.get("city_match"),
        discovery_source=candidate.get("discovery_source", ""),
        confidence=candidate.get("confidence", ""),
        human_decision=candidate.get("human_decision", ""),
        extra_hard_block_codes=candidate.get("hard_block_codes") or (),
    )
    if not result.final_sales_eligible:
        return False
    if minimum_confidence:
        order = {CONFIDENCE_LOW: 0, CONFIDENCE_MEDIUM: 1, CONFIDENCE_HIGH: 2}
        return order[result.confidence] >= order[str(minimum_confidence).upper()]
    return True


@dataclass(frozen=True)
class CandidateClassification:
    auto_decision: str
    auto_identity_decision: str
    confidence: str
    sales_usable: str
    hard_block_codes: tuple[str, ...]
    final_sales_eligible: bool
    rejection_reason: str = ""
    rationale: tuple[str, ...] = ()


def _host(value: Any) -> str:
    text = str(value or "").strip().lower()
    if "@" in text and "://" not in text:
        text = text.rsplit("@", 1)[-1]
    if "://" in text:
        text = urlsplit(text).hostname or ""
    return text.removeprefix("www.").rstrip(".")


def source_owner_type(*, discovery_source: Any = "", source_url: Any = "", clinic_name: Any = "") -> str:
    """Classify the publisher of the page, never the owner of its email address.

    Host-based checks (trusted directory, third-party portal, government/municipal)
    run before the discovery_source=="OFFICIAL_HP" shortcut: a Clinic's registered
    hp_url being used to discover a page is not itself evidence that the page is the
    Clinic's own (Source Owner != Email Owner; see is_government_host docstring).
    """
    source = str(discovery_source or "").strip().upper()
    host = _host(source_url)
    path = urlsplit(str(source_url or "")).path if source_url else ""
    legacy_public_detail = host.endswith(".lg.jp") and "/pb_md_info/" in path
    if host in TRUSTED_PUBLIC_DIRECTORY_HOSTS or host.endswith(".iryou.teikyouseido.mhlw.go.jp") or legacy_public_detail:
        return "TRUSTED_PUBLIC_DIRECTORY"
    if any(host == domain or host.endswith("." + domain) for domain in _THIRD_PARTY_HOSTS):
        return "THIRD_PARTY_PORTAL"
    if is_government_host(host) and not is_public_municipal_clinic(clinic_name):
        return "GOVERNMENT"
    if source == "OFFICIAL_HP":
        return "OFFICIAL_CLINIC"
    if host.endswith(".or.jp") or "association" in host or "medical-association" in host:
        return "ASSOCIATION"
    if any(marker in host for marker in ("dentnet", "doctorqube", "epark", "dirbi")):
        return "VENDOR"
    return "UNKNOWN"


def _explicit_third_party(candidate: dict) -> tuple[str, ...]:
    codes = set()
    ownership = normalize_ownership(candidate.get("email_ownership") or candidate.get("ownership"))
    role = normalize_role(candidate.get("role"))
    codes.update(str(value).strip().upper() for value in candidate.get("hard_block_codes") or () if str(value).strip())
    if ownership == OWNERSHIP_THIRD_PARTY:
        codes.add("THIRD_PARTY_OWNED")
    if role in HARD_BLOCK_ROLES:
        codes.add(role)
    # Distinguish the email owner from the page that discovered it. NAVII and
    # other public registries may use government-domain URLs while listing a
    # clinic-owned address; the discovery host alone is not ownership evidence.
    # The mailbox's own domain is the strongest signal and is exempted only when
    # the target Clinic is itself municipally/publicly operated.
    email_host = _host(candidate.get("email") or candidate.get("email_domain"))
    if is_government_host(email_host) and not is_public_municipal_clinic(candidate.get("clinic_name")):
        codes.add(OWNERSHIP_THIRD_PARTY)
    for field in ("email", "email_domain", "source_url", "source_type"):
        host = _host(candidate.get(field))
        source_is_trusted_directory = source_owner_type(
            discovery_source=candidate.get("discovery_source") or candidate.get("source_type"),
            source_url=candidate.get("source_url"),
        ) == "TRUSTED_PUBLIC_DIRECTORY"
        if ((field in {"email", "email_domain"} or (field == "source_url" and not source_is_trusted_directory)) and any(
            host == domain or host.endswith("." + domain) for domain in _THIRD_PARTY_HOSTS
        )):
            codes.add("THIRD_PARTY_SOURCE")
    # Structured ownership and role signals above are preferred. Text evidence
    # is limited to affirmative evidence describing the page/entity; model
    # rejection rationales often repeat generic "third party" warnings and
    # must not turn themselves into hard blocks.
    for field in ("source_type", "page_title", "nearby_text", "evidence_text"):
        text = str(candidate.get(field) or "").casefold()
        if any(marker.casefold() in text for marker in _ENTITY_REJECTION_MARKERS):
            codes.add("THIRD_PARTY_EVIDENCE")
    if _bool_evidence(candidate.get("clinic_name_match")) is False:
        codes.add("CLINIC_IDENTITY_MISMATCH")
    if _bool_evidence(candidate.get("prefecture_match")) is False or _bool_evidence(candidate.get("city_match")) is False:
        codes.add("LOCATION_IDENTITY_MISMATCH")
    return tuple(sorted(codes))


def _bool_evidence(value):
    if value is True or str(value).strip().casefold() in {"true", "yes", "1"}:
        return True
    if value is False or str(value).strip().casefold() in {"false", "no", "0"}:
        return False
    return None


def classify_email_candidate(candidate: dict) -> CandidateClassification:
    """Classify disposition separately from email identity and Sales eligibility.

    A bare REJECTED status is not enough for AUTO_FALSE. Hard-block evidence is
    required; an identity conflict without a clear explanation stays REVIEW.
    """
    role = normalize_role(candidate.get("role"))
    ownership = normalize_ownership(candidate.get("email_ownership") or candidate.get("ownership"))
    status = str(candidate.get("verification_status") or candidate.get("status") or "").upper()
    explicit = _explicit_third_party(candidate)
    identity_mismatch = any(code in explicit for code in (
        "CLINIC_IDENTITY_MISMATCH", "LOCATION_IDENTITY_MISMATCH",
    ))
    hard_false = bool(explicit)
    verified = status in {"VERIFIED_EMAIL", "VERIFIED", "PASSED"}
    identity_fields = tuple(_bool_evidence(candidate.get(key)) for key in (
        "clinic_name_match", "prefecture_match", "city_match"))
    identity_strong = all(value is True for value in identity_fields)
    identity_unknown = any(value is None for value in identity_fields)
    identity_decision = "TRUE" if verified and identity_strong else "REVIEW"
    if identity_mismatch:
        identity_decision = "FALSE"
    source = str(candidate.get("discovery_source") or candidate.get("source_type") or "").upper()
    owner_type = str(candidate.get("source_owner_type") or source_owner_type(
        discovery_source=source, source_url=candidate.get("source_url")
    )).upper()
    rejected = status in {"REJECTED", "REJECTED_THIRD_PARTY"}
    third_party_only = bool(explicit) and set(explicit).issubset({"THIRD_PARTY_OWNED", "THIRD_PARTY_EVIDENCE"})
    competing_trusted_identity = bool(
        rejected and identity_strong and (source in TRUSTED_SOURCES or owner_type == "TRUSTED_PUBLIC_DIRECTORY")
        and role not in HARD_BLOCK_ROLES and third_party_only
    )
    if verified and identity_strong and role in PRIMARY_ROLES and ownership in PRIMARY_OWNERSHIP and not explicit:
        disposition = AUTO_TRUE
        identity_decision = "TRUE"
    elif hard_false and not competing_trusted_identity:
        disposition = AUTO_FALSE
    else:
        # REJECTED without a specific block or with mixed identity evidence is reviewable.
        disposition = REVIEW

    confidence = confidence_for_candidate(
        clinic_name_match=True if identity_strong else candidate.get("clinic_name_match"),
        prefecture_match=True if identity_strong else candidate.get("prefecture_match"),
        city_match=True if identity_strong else candidate.get("city_match"),
        discovery_source=source, role=role, ownership=ownership,
    )
    eligibility = evaluate_sales_eligibility(
        auto_identity_decision=identity_decision, role=role, ownership=ownership,
        clinic_name_match=True if identity_strong else candidate.get("clinic_name_match"),
        prefecture_match=True if identity_strong else candidate.get("prefecture_match"),
        city_match=True if identity_strong else candidate.get("city_match"),
        discovery_source=source, confidence=confidence,
        extra_hard_block_codes=explicit,
    )
    if disposition != AUTO_TRUE:
        # AUTO_FALSE means no automatic Sales use; preserve identity TRUE for hard-blocked
        # but verified role cases, while never making them eligible or primary.
        final_eligible = False
        sales_usable = "NO" if disposition == AUTO_FALSE else "REVIEW"
    else:
        final_eligible = eligibility.final_sales_eligible
        sales_usable = eligibility.sales_usable
        if not final_eligible:
            disposition = REVIEW
    reasons = list(explicit)
    if competing_trusted_identity:
        reasons.append("TRUSTED_IDENTITY_CONFLICTS_WITH_REJECTED_OWNERSHIP_SIGNAL")
    if status in {"REJECTED", "REJECTED_THIRD_PARTY"} and not explicit:
        reasons.append("REJECTED_WITHOUT_HARD_BLOCK_EVIDENCE")
    if identity_unknown:
        reasons.append("IDENTITY_EVIDENCE_INCOMPLETE")
    return CandidateClassification(
        disposition, identity_decision, confidence, sales_usable,
        tuple(sorted(set(eligibility.hard_block_codes).union(explicit))), final_eligible,
        ";".join(reasons), tuple(reasons),
    )
