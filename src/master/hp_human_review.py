"""Pure helpers for HP Human Review labels and training snapshots."""
from __future__ import annotations

from typing import Any

RULE_VERSION = "hp-identity-v2-name-or-(phone+address+manager)"

DECISION_LABELS = {
    "OFFICIAL": "公式HPで間違いない",
    "ORGANIZATION_PAGE": "法人サイト内の正式な医院ページ",
    "NOT_OFFICIAL": "この医院のHPではない",
    "ACCESS_RESTRICTED": "URLは正しいがアクセス制限",
    "UNCERTAIN": "判断できない",
}
VALID_DECISIONS = frozenset(DECISION_LABELS)
POSITIVE_DECISIONS = frozenset({"OFFICIAL", "ORGANIZATION_PAGE", "ACCESS_RESTRICTED"})

PRIORITY_LABELS = {"HIGH": "高確度候補", "MEDIUM": "中確度候補", "LOW": "低確度候補"}
BUCKET_LABELS = {
    "ACCESS_RESTRICTED": "アクセス制限・URL本人確認済み",
    "PHONE_ADDRESS_NO_NAME": "電話＋住所一致 / 医院名不一致",
    "PHONE_MANAGER_NO_NAME": "電話＋院長名一致 / 医院名不一致",
    "HIGH_SCORE_NAME_MISSING": "高スコア / 医院名不一致",
    "NAME_ONLY": "医院名のみ一致",
    "OTHER": "その他",
}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def candidate_features(candidate: dict | None) -> dict:
    candidate = candidate if isinstance(candidate, dict) else {}
    reasons = candidate.get("reasons") if isinstance(candidate.get("reasons"), list) else []
    return {
        "url": str(candidate.get("url") or ""),
        "score": int(candidate.get("score") or 0),
        "name_match": _as_bool(candidate.get("name_match")),
        "phone_match": _as_bool(candidate.get("phone_match")),
        "address_match": _as_bool(candidate.get("address_match")),
        "manager_match": _as_bool(candidate.get("manager_match")) or "院長名一致" in reasons,
        "reasons": [str(item) for item in reasons],
    }


def _best_candidate(candidates: list[dict]) -> dict:
    normalized = [candidate_features(item) for item in candidates if isinstance(item, dict)]
    if not normalized:
        return candidate_features(None)
    return max(normalized, key=lambda item: (item["score"], bool(item["url"])))


def review_priority(snapshot: dict) -> str:
    if snapshot.get("content_status") == "ACCESS_RESTRICTED":
        return "HIGH"
    score = int(snapshot.get("score") or 0)
    if score >= 80:
        return "HIGH"
    if score >= 40:
        return "MEDIUM"
    return "LOW"


def auto_accept_without_human(snapshot: dict) -> bool:
    """User-approved precision exception for old REVIEW rows.

    If clinic name is the only failed identity feature while phone, address and manager
    all match, Human Review is unnecessary. ACCESS_RESTRICTED remains a separate case.
    """
    if str(snapshot.get("content_status") or "") == "ACCESS_RESTRICTED":
        return False
    return bool(
        snapshot.get("best_candidate_url")
        and not bool(snapshot.get("name_match"))
        and bool(snapshot.get("phone_match"))
        and bool(snapshot.get("address_match"))
        and bool(snapshot.get("manager_match"))
    )


def review_bucket(snapshot: dict) -> str:
    if snapshot.get("content_status") == "ACCESS_RESTRICTED":
        return "ACCESS_RESTRICTED"
    name = bool(snapshot.get("name_match"))
    phone = bool(snapshot.get("phone_match"))
    address = bool(snapshot.get("address_match"))
    manager = bool(snapshot.get("manager_match"))
    score = int(snapshot.get("score") or 0)
    if phone and address and not name:
        return "PHONE_ADDRESS_NO_NAME"
    if phone and manager and not name:
        return "PHONE_MANAGER_NO_NAME"
    if score >= 80 and not name:
        return "HIGH_SCORE_NAME_MISSING"
    if name and not (phone or address):
        return "NAME_ONLY"
    return "OTHER"


def review_snapshot(result: dict | None) -> dict:
    """Freeze the exact auto-decision evidence used for later human-label analysis."""
    result = result if isinstance(result, dict) else {}
    raw_candidates = result.get("hp_candidates") if isinstance(result.get("hp_candidates"), list) else []
    candidates = [candidate_features(item) for item in raw_candidates if isinstance(item, dict)]

    # ACCESS_RESTRICTED has a verified Maps URL but intentionally no hp_candidates.
    hp_url = str(result.get("hp_url") or "")
    if not candidates and hp_url:
        candidates = [{
            **candidate_features(None),
            "url": hp_url,
            "score": int(result.get("hp_score") or 0),
            "reasons": [str(x) for x in (result.get("hp_match_reason") or [])],
        }]

    best = _best_candidate(candidates)
    snapshot = {
        "rule_version": RULE_VERSION,
        "auto_decision": str(result.get("research_status") or "REVIEW"),
        "hp_status": str(result.get("hp_status") or ""),
        "hp_checked_at": str(result.get("hp_checked_at") or ""),
        "content_status": str(result.get("hp_content_status") or ""),
        "content_note": str(result.get("hp_content_note") or ""),
        "score": best["score"],
        "name_match": best["name_match"],
        "phone_match": best["phone_match"],
        "address_match": best["address_match"],
        "manager_match": best["manager_match"],
        "reasons": best["reasons"],
        "best_candidate_url": best["url"],
        "candidate_urls": [item["url"] for item in candidates if item["url"]],
        "candidates": candidates,
        "crawl_errors": result.get("crawl_errors") if isinstance(result.get("crawl_errors"), list) else [],
        "hp_match_reason": result.get("hp_match_reason") if isinstance(result.get("hp_match_reason"), list) else [],
    }
    snapshot["priority"] = review_priority(snapshot)
    snapshot["bucket"] = review_bucket(snapshot)
    snapshot["auto_accept_without_human"] = auto_accept_without_human(snapshot)
    snapshot["auto_accept_reason"] = (
        "医院名のみ不一致・電話番号/住所/院長名一致"
        if snapshot["auto_accept_without_human"] else ""
    )
    return snapshot


def reanalyze_auto_verified_hp(store, *, clinic_id, selected_url):
    """Re-run HP content analysis for a legacy REVIEW now covered by an auto-verify rule.

    No Human Review row is created. The current research result and HP ledger are refreshed
    exactly like a normal successful HP job, with an explicit identity source for audit/export.
    """
    from src.enrichment.researcher import research_human_verified_hp
    from src.master.jobs import _hp_ledger_payload
    from src.repository.write_backend import write_repositories_for

    repositories = write_repositories_for(store)
    if repositories.hp is None:
        raise RuntimeError("HP research ledgerを利用できません。")

    cid = int(clinic_id)
    url = str(selected_url or "").strip()
    if not url:
        raise ValueError("自動再解析する候補URLがありません。")

    record = store.get(cid)
    saved = repositories.research.get_saved_research(cid)
    record["marketing_signals"] = saved.get("marketing_signals", [])

    result, pages = research_human_verified_hp(record, url)
    result.update(
        hp_identity_source="AUTO_NAME_ONLY_MISMATCH",
        hp_match_reason=["医院名のみ不一致・電話番号/住所/院長名一致で自動本人確認"],
        hp_content_note="新しい本人確認ルールによりHuman Review不要として自動再解析しました。",
    )
    repositories.research.save_research(cid, result, pages)
    repositories.hp.upsert_result(
        _hp_ledger_payload(cid, result, "SUCCESS", "Auto identity rule reanalysis")
    )
    categories = result.get("treatment_categories") or []
    if not isinstance(categories, list):
        categories = []
    return {
        "status": "DONE",
        "clinic_id": cid,
        "selected_url": url,
        "treatment_categories": categories,
        "treatment_count": len(categories),
        "fetch_status": "OK",
    }


def reanalyze_human_verified_hp(store, *, human_review_id, clinic_id, selected_url):
    """Re-run normal HP content analysis after human identity verification.

    The human label is already committed before this function is called. On success,
    the canonical research result + HP ledger are refreshed. On fetch/analysis failure,
    the human-verified URL remains preserved by manual override while the HP ledger is
    marked failed; the previous REVIEW evidence row is intentionally kept for audit/retry.
    """
    from src.enrichment.researcher import (
        empty_hp_result,
        research_human_verified_hp,
    )
    from src.enrichment.safe_web import WebError
    from src.master.jobs import _hp_ledger_payload
    from src.master.store import now
    from src.repository.write_backend import write_repositories_for

    repositories = write_repositories_for(store)
    if repositories.hp is None or repositories.hp_human_review is None:
        raise RuntimeError("HP Human ReviewのSupabase保存先を利用できません。")

    cid = int(clinic_id)
    url = str(selected_url or "").strip()
    started_at = now()

    record = store.get(cid)
    saved = repositories.research.get_saved_research(cid)
    record["marketing_signals"] = saved.get("marketing_signals", [])

    try:
        result, pages = research_human_verified_hp(record, url)
        repositories.research.save_research(cid, result, pages)
        repositories.hp.upsert_result(
            _hp_ledger_payload(cid, result, "SUCCESS", "Human Review verified")
        )
        categories = result.get("treatment_categories") or []
        if not isinstance(categories, list):
            categories = []
        repositories.hp_human_review.record_reanalysis(
            human_review_id=human_review_id,
            clinic_id=cid,
            selected_url=url,
            status="DONE",
            treatment_categories=categories,
            started_at=started_at,
            finished_at=now(),
        )
        return {
            "status": "DONE",
            "treatment_categories": categories,
            "treatment_count": len(categories),
            "fetch_status": "OK",
        }
    except Exception as exc:
        # SafeFetcher/WebError messages contain useful HTTP/robots context, while arbitrary
        # unexpected exceptions may contain implementation details and must not be persisted.
        note = str(exc) if isinstance(exc, WebError) else (
            "Human Review後のHP内容解析でエラーが発生しました。再試行してください。"
        )
        safe_result = empty_hp_result("ERROR", record)
        safe_result.update(
            hp_status="VERIFIED",
            hp_verified=True,
            hp_url=url,
            final_url=url,
            hp_checked_at=now(),
            hp_identity_source="HUMAN_REVIEW",
            human_verified_source_url=url,
            research_status="ERROR",
            research_error=note,
        )
        # Do not overwrite research.research_results on failure: the original REVIEW
        # evidence remains inspectable and retryable. Only the canonical batch ledger
        # becomes FETCH_FAILED/ERROR for top-level metrics.
        repositories.hp.upsert_result(
            _hp_ledger_payload(cid, safe_result, "ERROR", note)
        )
        repositories.hp_human_review.record_reanalysis(
            human_review_id=human_review_id,
            clinic_id=cid,
            selected_url=url,
            status="FAILED",
            treatment_categories=[],
            error_detail=note,
            started_at=started_at,
            finished_at=now(),
        )
        return {
            "status": "FAILED",
            "treatment_categories": [],
            "treatment_count": 0,
            "fetch_status": "ERROR",
            "error_detail": note,
        }
