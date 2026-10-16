"""Explicitly gated Gmail draft creation. Default behavior is local DRY_RUN only."""
from __future__ import annotations

import argparse
import csv
import hashlib
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.outreach.gmail_drafts import (  # noqa: E402
    BATCH_CONFIRMATION,
    CANARY_CONFIRMATION,
    CANARY_ACCOUNT,
    DEFAULT_DRAFT_LEDGER,
    MAX_BATCH_SIZE,
    ApprovedDraftRequest,
    DraftSafetyError,
    create_approved_draft,
)
from src.outreach.gmail_oauth import (  # noqa: E402
    GmailOAuthError,
    connect_existing,
    expected_gmail_account,
)

ARTICLE_MASTER = Path(".codex-input/article_selection_v02/原本_変更禁止_Crestix_クリニックナビ記事一覧.csv")
ARTICLE_MASTER_SHA256 = "9608e4ac5ecac98af18c29f7375a71d1232b3b3c90c7b9c337d5bbe9df4b07fa"
REQUIRED_COLUMNS = {
    "clinic_uuid", "email_candidate_id", "clinic_name", "director_name",
    "director_name_verified", "to_email", "recipient_email_verified",
    "subject", "v2_body", "human_review_status", "template_version",
    "article_status", "article_evidence_url", "article_evidence_method",
    "article_checked_at", "actual_article_url", "recommended_article_url",
    "clinic_detail_url", "clinic_detail_verified", "article_listing_verified",
    "article_absence_verified", "schedule_iso", "phone_call_date",
    "email_send_request_confirmed",
}


def _bool(value: str, field: str) -> bool:
    normalized = value.strip().casefold()
    if normalized not in {"true", "false"}:
        raise DraftSafetyError(f"{field} must be true or false")
    return normalized == "true"


def _load_article_master(path: Path) -> frozenset[str]:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != ARTICLE_MASTER_SHA256:
        raise DraftSafetyError("Immutable article master hash mismatch")
    with path.open(encoding="utf-8-sig", newline="") as source:
        urls = frozenset((row.get("URL") or "").strip() for row in csv.DictReader(source)
                         if (row.get("URL") or "").strip())
    if not urls:
        raise DraftSafetyError("Immutable article master contains no URLs")
    return urls


def load_approved_candidates(path: Path, *, article_master_urls: frozenset[str]) -> list[ApprovedDraftRequest]:
    if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
        raise DraftSafetyError("Candidate CSV must exist as a regular private 0600 file")
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None or not REQUIRED_COLUMNS.issubset(set(reader.fieldnames)):
            raise DraftSafetyError("Candidate CSV is missing required v2 approval fields")
        requests: list[ApprovedDraftRequest] = []
        for row in reader:
            phone_date = (row.get("phone_call_date") or "").strip()
            requests.append(ApprovedDraftRequest(
                clinic_uuid=(row.get("clinic_uuid") or "").strip(),
                email_candidate_id=(row.get("email_candidate_id") or "").strip(),
                clinic_name=(row.get("clinic_name") or "").strip(),
                director_name=(row.get("director_name") or "").strip(),
                recipient=(row.get("to_email") or "").strip(),
                subject=row.get("subject") or "",
                v2_body=row.get("v2_body") or "",
                human_review_status=(row.get("human_review_status") or "").strip(),
                template_version=(row.get("template_version") or "").strip(),
                article_status=(row.get("article_status") or "").strip(),
                article_evidence_url=(row.get("article_evidence_url") or "").strip(),
                article_evidence_method=(row.get("article_evidence_method") or "").strip(),
                article_checked_at=(row.get("article_checked_at") or "").strip(),
                actual_article_url=(row.get("actual_article_url") or "").strip(),
                recommended_article_url=(row.get("recommended_article_url") or "").strip(),
                clinic_detail_url=(row.get("clinic_detail_url") or "").strip(),
                clinic_detail_verified=_bool(row.get("clinic_detail_verified") or "", "clinic_detail_verified"),
                article_listing_verified=_bool(row.get("article_listing_verified") or "", "article_listing_verified"),
                article_absence_verified=_bool(row.get("article_absence_verified") or "", "article_absence_verified"),
                director_name_verified=_bool(row.get("director_name_verified") or "", "director_name_verified"),
                recipient_email_verified=_bool(row.get("recipient_email_verified") or "", "recipient_email_verified"),
                schedule=tuple(item.strip() for item in (row.get("schedule_iso") or "").split(";") if item.strip()),
                phone_call_date=date.fromisoformat(phone_date) if phone_date else None,
                email_send_request_confirmed=_bool(
                    row.get("email_send_request_confirmed") or "", "email_send_request_confirmed"
                ),
                article_master_urls=article_master_urls,
            ))
    return requests


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("canary", "batch"))
    parser.add_argument("--candidates", type=Path, required=True,
                        help="0600 UTF-8 CSV exported from human-approved v2 bodies only")
    parser.add_argument("--article-master", type=Path, default=ROOT / ARTICLE_MASTER)
    parser.add_argument("--ledger", type=Path, default=ROOT / DEFAULT_DRAFT_LEDGER)
    parser.add_argument("--execute", action="store_true", help="Request a real Gmail draft write")
    parser.add_argument("--confirmation", default="", help="Exact mode-specific confirmation phrase")
    parser.add_argument("--confirm-count", type=int, help="Required candidate count for batch execution")
    args = parser.parse_args(argv)

    try:
        master_urls = _load_article_master(args.article_master)
        requests = load_approved_candidates(args.candidates, article_master_urls=master_urls)
        if not requests:
            raise DraftSafetyError("Candidate CSV contains no rows")
        if args.mode == "canary":
            if len(requests) != 1:
                raise DraftSafetyError("Canary mode requires exactly one candidate")
            if requests[0].recipient.strip().casefold() != CANARY_ACCOUNT:
                raise DraftSafetyError("Canary recipient must be info@crestix-inc.com")
        else:
            if len(requests) > MAX_BATCH_SIZE:
                raise DraftSafetyError(f"Batch mode is limited to {MAX_BATCH_SIZE} candidates per run")
            if any(row.recipient.strip().casefold() == CANARY_ACCOUNT for row in requests):
                raise DraftSafetyError("The internal Canary recipient is not accepted in batch mode")

        if args.execute:
            required_confirmation = CANARY_CONFIRMATION if args.mode == "canary" else BATCH_CONFIRMATION
            if args.confirmation != required_confirmation:
                raise DraftSafetyError("Execution requires the exact mode-specific confirmation phrase")
            if args.mode == "batch" and args.confirm_count != len(requests):
                raise DraftSafetyError("Batch --confirm-count must exactly match the candidate count")

        connection = connect_existing(root=ROOT) if args.execute else None
        results = [create_approved_draft(
            request,
            mode=args.mode,
            connection=connection,
            execute=args.execute,
            confirmation=args.confirmation,
            confirmed_batch_count=args.confirm_count,
            batch_size=len(requests),
            ledger_path=args.ledger,
        ) for request in requests]
        counts: dict[str, int] = {}
        for result in results:
            counts[result.status] = counts.get(result.status, 0) + 1
        count_text = ",".join(f"{status}:{count}" for status, count in sorted(counts.items()))
        live_account = connection.email_address if connection else "NOT_CHECKED_DRY_RUN"
        print(f"mode={args.mode} account={live_account} candidates={len(requests)} results={count_text} "
              f"draft_writes={counts.get('DRAFT_CREATED', 0) + counts.get('CREATED_RECOVERED', 0)} sends=0")
        return 0 if all(result.status in {"DRY_RUN_CONTENT_READY", "DRY_RUN_NEEDS_REVIEW",
                                         "DRAFT_CREATED", "CREATED_RECOVERED", "SKIPPED_DUPLICATE"}
                        for result in results) else 2
    except (DraftSafetyError, GmailOAuthError, OSError, ValueError) as exc:
        # Error messages are deliberately sanitized; never print candidate rows or credential material.
        print(f"status=REFUSED reason={exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
