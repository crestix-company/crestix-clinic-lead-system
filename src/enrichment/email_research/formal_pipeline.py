"""Formal fast Email Research flow built from the proven discovery/verifier engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic

from src.enrichment.email_research.email_role_classifier import classify_email_role
from src.enrichment.email_research.pipeline import (
    PipelineResult, SOURCE_OFFICIAL_HP, SOURCE_SEARCH_QUERY, STATUS_VERIFIED_EMAIL,
    _apply_candidates, run_pipeline,
)
from src.enrichment.email_research.search_candidate_adapter import (
    SearchEmailCandidate, extract_candidate_emails_from_results,
)
from src.enrichment.search_provider import SearchError
from src.master.email_sales_eligibility import (
    AUTO_FALSE, AUTO_TRUE, REVIEW, RULE_VERSION, classify_email_candidate,
    confidence_for_candidate, is_government_host, is_public_municipal_clinic, source_owner_type,
)


@dataclass
class FormalResearchResult:
    clinic_id: int
    research_status: str
    candidates: list[dict] = field(default_factory=list)
    rejection_evidence: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def _context(row):
    context = row.email_context
    return dict(
        section_heading=getattr(context, "section_heading", ""),
        nearby_text=getattr(context, "nearby_text", "") or row.nearby_text,
        anchor_text=getattr(context, "anchor_text", ""),
        parent_text=getattr(context, "parent_text", ""),
        page_url=row.source_url,
        page_title=getattr(context, "page_title", ""),
    )


def _formal_candidate(row, clinic, *, discovery_source=None):
    verified = row.status == STATUS_VERIFIED_EMAIL
    role_result = classify_email_role(row.email, **_context(row))
    source = discovery_source or row.source_type
    clinic_name = str(clinic.get("clinic_name") or "")
    clinic_is_public_municipal = is_public_municipal_clinic(clinic_name)
    owner_type = source_owner_type(discovery_source=source, source_url=row.source_url, clinic_name=clinic_name)
    email_host = row.email.rsplit("@", 1)[-1].casefold() if "@" in row.email else ""
    trusted_directory_verified = verified and owner_type == "TRUSTED_PUBLIC_DIRECTORY"
    # Source Owner != Email Owner (clinic_id=99522 incident): a government/municipal
    # mailbox domain is blocked regardless of discovery_source, unless the target
    # Clinic is itself municipally/publicly operated. The page being OFFICIAL_HP
    # (i.e. public.clinics.hp_url) is never, on its own, ownership evidence.
    explicit_third_party_email = is_government_host(email_host) and not clinic_is_public_municipal
    # A page whose publisher itself is a government/municipal directory (not the
    # dedicated MHLW TRUSTED_PUBLIC_DIRECTORY) is inconclusive ownership evidence
    # on its own -- defer to REVIEW rather than auto-granting CLINIC_OWNED.
    page_is_unverified_government = owner_type == "GOVERNMENT" and not clinic_is_public_municipal
    if explicit_third_party_email:
        ownership = "THIRD_PARTY_OWNED"
    elif trusted_directory_verified:
        ownership = "CLINIC_OWNED"
    elif page_is_unverified_government:
        ownership = "OWNERSHIP_UNKNOWN"
    elif row.third_party_suspected:
        ownership = "THIRD_PARTY_OWNED"
    else:
        ownership = "CLINIC_OWNED" if verified else "OWNERSHIP_UNKNOWN"
    identity = "TRUE" if verified else "REVIEW"
    name_match = True if verified else None
    prefecture_match = True if verified else None
    city_match = True if verified else None
    confidence = confidence_for_candidate(
        clinic_name_match=name_match, prefecture_match=prefecture_match, city_match=city_match,
        discovery_source=source, role=role_result.role, ownership=ownership,
    )
    candidate = {
        "clinic_id": int(clinic["clinic_id"]), "clinic_name": clinic_name, "email": row.email,
        "discovery_source": source, "source_url": row.source_url,
        "search_query": row.source_query, "role": role_result.role,
        "source_owner_type": owner_type, "email_ownership": ownership,
        "ownership": ownership, "verification_status": row.status,
        "auto_identity_decision": identity, "confidence": confidence,
        "clinic_name_match": name_match, "prefecture_match": prefecture_match,
        "city_match": city_match, "verification_reason": row.evidence_text or row.rationale,
        "rejection_reason": "", "sales_usable": "REVIEW",
        "hard_block_codes": [], "final_sales_eligible": False,
        "rule_version": RULE_VERSION, "researched_at": row.checked_at,
        "auto_snapshot": {
            "pipeline_version": row.parser_version, "rationale": row.rationale,
            "role_reason": role_result.role_reason, "role_confidence": role_result.role_confidence,
            "engine_confidence": row.confidence, "official_domain": row.official_domain,
            "verified_on_official": row.verified_on_official,
            "source_owner_type": owner_type,
            "email_ownership_evidence": "verified clinic-specific public-directory detail" if trusted_directory_verified else "",
        },
    }
    classification = classify_email_candidate(candidate)
    candidate.update({
        "auto_decision": classification.auto_decision,
        "auto_identity_decision": classification.auto_identity_decision,
        "confidence": classification.confidence,
        "sales_usable": classification.sales_usable,
        "hard_block_codes": list(classification.hard_block_codes),
        "final_sales_eligible": classification.final_sales_eligible,
        "rejection_reason": classification.rejection_reason,
        "auto_snapshot": {**candidate["auto_snapshot"],
                          "classification_rationale": list(classification.rationale)},
    })
    return candidate


def _append_unique(output, rows, clinic, *, source=None):
    by_email = {item["email"].strip().casefold(): item for item in output}
    for row in rows:
        item = _formal_candidate(row, clinic, discovery_source=source)
        key = item["email"].strip().casefold()
        previous = by_email.get(key)
        if previous is None or (item["auto_decision"] == AUTO_TRUE and previous["auto_decision"] != AUTO_TRUE):
            by_email[key] = item
    output[:] = list(by_email.values())


def _append_rejected(output, rows, clinic, *, source=None):
    by_email = {item["email"].strip().casefold(): item for item in output}
    for row in rows:
        key = row.email.strip().casefold()
        if key in by_email:
            continue
        by_email[key] = {
            "clinic_id": int(clinic["clinic_id"]), "email": row.email,
            "discovery_source": source or row.source_type, "source_url": row.source_url,
            "search_query": row.source_query, "role": "UNKNOWN",
            "source_owner_type": source_owner_type(discovery_source=source or row.source_type, source_url=row.source_url),
            "email_ownership": "THIRD_PARTY_OWNED", "ownership": "THIRD_PARTY_OWNED",
            "verification_status": "REJECTED_THIRD_PARTY",
            "auto_identity_decision": "FALSE", "confidence": "LOW",
            "clinic_name_match": None, "prefecture_match": None, "city_match": None,
            "verification_reason": "", "rejection_reason": row.rationale,
            "sales_usable": "NO", "hard_block_codes": ["THIRD_PARTY_OWNED"],
            "auto_decision": AUTO_FALSE,
            "final_sales_eligible": False, "rule_version": RULE_VERSION,
            "researched_at": row.checked_at,
            "auto_snapshot": {"rationale": row.rationale, "rejected": True},
        }
    output[:] = list(by_email.values())


def research_clinic(clinic: dict, *, fetcher, search_provider=None, trusted_candidates=()) -> FormalResearchResult:
    """Official HP -> NAVII -> exactly two conditional Search API calls."""
    started = monotonic()
    cid = int(clinic["clinic_id"])
    common = dict(
        clinic_id=str(cid), clinic_name=str(clinic.get("clinic_name") or ""),
        prefecture=str(clinic.get("prefecture") or ""), city=str(clinic.get("city") or ""),
        address=str(clinic.get("address") or ""), phone=str(clinic.get("phone") or ""),
        fetcher=fetcher, run_ai_overview=False, fast_acquisition_mode=True,
    )
    candidates = []
    transport = getattr(fetcher, "transport", None)
    transport_metrics = getattr(transport, "metrics", {})
    fetch_metrics = getattr(fetcher, "metrics", {})
    official_fetch_before = int(transport_metrics.get("web_fetch_attempts", 0) or 0)
    official_retry_before = int(fetch_metrics.get("rate_limit_retry_count", 0) or 0)
    official_started = monotonic()
    official = run_pipeline(
        **common, hp_url=str(clinic.get("hp_url") or ""),
        official_crawl_eligible=bool(clinic.get("hp_url")), search_provider=None,
        max_search_queries=2,
    )
    stage_elapsed_official_hp = monotonic() - official_started
    official_hp_fetch_attempts = int(transport_metrics.get("web_fetch_attempts", 0) or 0) - official_fetch_before
    official_fetch_retries = int(fetch_metrics.get("rate_limit_retry_count", 0) or 0) - official_retry_before
    _append_unique(candidates, official.email_rows, clinic)
    _append_rejected(candidates, official.rejected_rows, clinic)
    official_found = len(official.email_rows) + len(official.rejected_rows)
    metrics = {"search_query_count": 0, "query_1_candidate_count": 0, "query_2_candidate_count": 0,
               "candidate_found_count": official_found,
               "http_attempts": 0, "retry_count": 0,
               "timeout_count": 0, "web_fetch_attempts": 0, "web_fetch_errors": 0,
               "web_fetch_timeout": 0, "stage_elapsed_official_hp": stage_elapsed_official_hp,
               "stage_elapsed_query1": 0.0, "stage_elapsed_query2": 0.0,
               "stage_elapsed_verification": 0.0,
               "official_hp_fetch_attempts": official_hp_fetch_attempts,
               "verification_fetch_attempts": 0,
               "search_query1_attempts": 0, "search_query2_attempts": 0,
               "search_timeout": 0,
               "retry_by_stage": {"OFFICIAL_HP": official_fetch_retries, "NAVII": 0, "QUERY_1": 0, "QUERY_2": 0},
               "source_counts": {"OFFICIAL_HP": official_found, "NAVII": 0,
                                 "SEARCH_QUERY_1": 0, "SEARCH_QUERY_2": 0}}

    def finish(status):
        provider_metrics = getattr(search_provider, "metrics", {}) if search_provider is not None else {}
        metrics.update({
            "http_attempts": provider_metrics.get("attempted", metrics["search_query_count"]),
            "retry_count": int(provider_metrics.get("retry_count", 0) or 0)
                           + int(fetch_metrics.get("rate_limit_retry_count", 0) or 0),
            "timeout_count": int(provider_metrics.get("timeout", 0) or 0)
                             + int(transport_metrics.get("web_fetch_timeout", 0) or 0),
            "web_fetch_attempts": transport_metrics.get("web_fetch_attempts", 0),
            "web_fetch_errors": transport_metrics.get("web_fetch_errors", 0),
            "web_fetch_timeout": transport_metrics.get("web_fetch_timeout", 0),
            "search_timeout": provider_metrics.get("timeout", 0),
            "elapsed_seconds": monotonic() - started,
        })
        assert metrics["search_query_count"] <= 2
        rejected = [item for item in candidates if item.get("auto_decision") == AUTO_FALSE]
        retained = [item for item in candidates if item.get("auto_decision") in {AUTO_TRUE, REVIEW}]
        metrics.update({
            "candidate_found_count": metrics.get("candidate_found_count", 0),
            "valid_candidate_count": len(retained),
            "auto_false_count": len(rejected),
            "auto_true_count": sum(item.get("auto_decision") == AUTO_TRUE for item in retained),
            "review_count": sum(item.get("auto_decision") == REVIEW for item in retained),
            "review_clinic_count": int(any(item.get("auto_decision") == REVIEW for item in retained)),
            "auto_true_clinic_count": int(any(item.get("auto_decision") == AUTO_TRUE for item in retained)),
        })
        actual_status = ("FOUND" if any(item.get("auto_decision") == AUTO_TRUE for item in retained)
                         else "REVIEW" if any(item.get("auto_decision") == REVIEW for item in retained)
                         else "NO_EMAIL_FOUND" if status != "ERROR" else "ERROR")
        rejection_evidence = [{
            "clinic_id": int(cid), "normalized_email": item["email"].strip().casefold(),
            "source_url": item.get("source_url", ""), "source_type": item.get("discovery_source", ""),
            "auto_decision": AUTO_FALSE, "rejection_reason": item.get("rejection_reason", ""),
            "hard_block_codes": item.get("hard_block_codes", []),
            "auto_snapshot": {**item, "auto_snapshot": item.get("auto_snapshot", {})},
        } for item in rejected]
        return FormalResearchResult(cid, actual_status, retained, rejection_evidence, metrics)

    if any(item["final_sales_eligible"] for item in candidates):
        return finish("FOUND")

    if trusted_candidates:
        verification_fetch_before = int(transport_metrics.get("web_fetch_attempts", 0) or 0)
        verification_retry_before = int(fetch_metrics.get("rate_limit_retry_count", 0) or 0)
        verification_started = monotonic()
        navii = PipelineResult(str(cid), common["clinic_name"])
        navii_rows = [SearchEmailCandidate(
            email=str(item["email"]), source_url=str(item.get("source_url") or ""),
            source_title=str(item.get("source_title") or ""), snippet_text=str(item.get("snippet_text") or ""),
            matched_query="", field_matched="NAVII", third_party_suspected=False,
        ) for item in trusted_candidates]
        metrics["candidate_found_count"] += len(navii_rows)
        _apply_candidates(result=navii, candidates=navii_rows, clinic_id=str(cid),
                          clinic_name=common["clinic_name"], source_type="NAVII", source_query_fallback="",
                          known_hp_url=str(clinic.get("hp_url") or ""), fetcher=fetcher,
                          checked_at=official.email_rows[0].checked_at if official.email_rows else datetime.now(timezone.utc).isoformat(),
                          prefecture=common["prefecture"], city=common["city"], address=common["address"],
                          phone=common["phone"], strict_is_identity=True)
        _append_unique(candidates, navii.email_rows, clinic, source="NAVII")
        _append_rejected(candidates, navii.rejected_rows, clinic, source="NAVII")
        metrics["stage_elapsed_verification"] += monotonic() - verification_started
        metrics["verification_fetch_attempts"] += int(transport_metrics.get("web_fetch_attempts", 0) or 0) - verification_fetch_before
        metrics["retry_by_stage"]["NAVII"] += int(fetch_metrics.get("rate_limit_retry_count", 0) or 0) - verification_retry_before
        metrics["source_counts"]["NAVII"] = len(navii.email_rows) + len(navii.rejected_rows)
        if any(item["final_sales_eligible"] for item in candidates):
            return finish("FOUND")

    if search_provider is not None:
        queries = [
            f"{common['clinic_name']} メールアドレス",
            " ".join(value for value in (common["clinic_name"], "メールアドレス",
                                           common["prefecture"], common["city"]) if value),
        ]
        for query_index, query in enumerate(queries, start=1):  # invariant: statically at most two
            metrics["search_query_count"] += 1
            provider_metrics = getattr(search_provider, "metrics", {})
            attempts_before = int(provider_metrics.get("attempted", 0) or 0)
            retries_before = int(provider_metrics.get("retry_count", 0) or 0)
            timeouts_before = int(provider_metrics.get("timeout", 0) or 0)
            query_started = monotonic()
            try:
                results = search_provider.search(query)
            except SearchError:
                query_elapsed = monotonic() - query_started
                metrics[f"stage_elapsed_query{query_index}"] += query_elapsed
                metrics[f"search_query{query_index}_attempts"] += int(provider_metrics.get("attempted", 0) or 0) - attempts_before
                metrics["retry_by_stage"][f"QUERY_{query_index}"] += int(provider_metrics.get("retry_count", 0) or 0) - retries_before
                metrics["search_timeout"] += int(provider_metrics.get("timeout", 0) or 0) - timeouts_before
                continue
            metrics[f"stage_elapsed_query{query_index}"] += monotonic() - query_started
            metrics[f"search_query{query_index}_attempts"] += int(provider_metrics.get("attempted", 0) or 0) - attempts_before
            metrics["retry_by_stage"][f"QUERY_{query_index}"] += int(provider_metrics.get("retry_count", 0) or 0) - retries_before
            metrics["search_timeout"] += int(provider_metrics.get("timeout", 0) or 0) - timeouts_before
            found = extract_candidate_emails_from_results(results, query)
            metrics["candidate_found_count"] += len(found)
            metrics[f"query_{query_index}_candidate_count"] += len(found)
            verification_fetch_before = int(transport_metrics.get("web_fetch_attempts", 0) or 0)
            verification_retry_before = int(fetch_metrics.get("rate_limit_retry_count", 0) or 0)
            verification_started = monotonic()
            search_result = PipelineResult(str(cid), common["clinic_name"])
            _apply_candidates(result=search_result, candidates=found, clinic_id=str(cid),
                              clinic_name=common["clinic_name"], source_type=SOURCE_SEARCH_QUERY,
                              source_query_fallback=query, known_hp_url=str(clinic.get("hp_url") or ""),
                              fetcher=fetcher, checked_at=datetime.now(timezone.utc).isoformat(), prefecture=common["prefecture"],
                              city=common["city"], address=common["address"], phone=common["phone"],
                              strict_is_identity=True)
            _append_unique(candidates, search_result.email_rows, clinic)
            _append_rejected(candidates, search_result.rejected_rows, clinic)
            metrics["stage_elapsed_verification"] += monotonic() - verification_started
            metrics["verification_fetch_attempts"] += int(transport_metrics.get("web_fetch_attempts", 0) or 0) - verification_fetch_before
            metrics["retry_by_stage"][f"QUERY_{query_index}"] += int(fetch_metrics.get("rate_limit_retry_count", 0) or 0) - verification_retry_before
            metrics["source_counts"][f"SEARCH_QUERY_{query_index}"] += (
                len(search_result.email_rows) + len(search_result.rejected_rows)
            )
            if any(item["final_sales_eligible"] for item in candidates):
                break
    if any(item["final_sales_eligible"] for item in candidates):
        status = "FOUND"
    elif any(item["auto_identity_decision"] == "REVIEW" for item in candidates):
        status = "REVIEW"
    else:
        status = "NO_EMAIL_FOUND"
    return finish(status)
