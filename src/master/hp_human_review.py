"""Pure helpers for HP Human Review labels and training snapshots."""
from __future__ import annotations

from typing import Any

RULE_VERSION = "hp-identity-v1-name-and-(phone-or-address)"

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
        # identity() currently exposes manager match through the reasons/score, not a dedicated key.
        "manager_match": "院長名一致" in reasons,
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
    return snapshot
