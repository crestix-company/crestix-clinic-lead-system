"""Prepare/verify Gmail OAuth without creating or sending mail."""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.outreach.gmail_oauth import (  # noqa: E402
    GmailOAuthError,
    authorize_interactive,
    connect_existing,
    expected_gmail_account,
)
from src.outreach.gmail_drafts import (  # noqa: E402
    DraftIdentity,
    check_duplicate,
    list_existing_drafts,
)


def _duplicate_counts(path: Path, service) -> tuple[int, int, dict[str, int]]:
    if not path.is_file() or path.stat().st_mode & 0o077:
        raise GmailOAuthError("Canary CSV must exist with permissions 0600")
    with path.open(encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))
    existing = list_existing_drafts(service)
    reason_counts: dict[str, int] = {}
    duplicates = 0
    for row in rows:
        identity = DraftIdentity(
            clinic_uuid=row.get("clinic_uuid", ""),
            email_candidate_id=row.get("email_candidate_id", ""),
            recipient=row.get("to_email", row.get("primary_email", "")),
            template_version=row.get("template", row.get("template_version", "")),
            recommended_url=row.get("recommended_url", row.get("article_example_url", "")),
            listed_url=row.get("listed_url", ""),
        )
        result = check_duplicate(identity, subject=row.get("subject", ""), existing=existing)
        if result.duplicate:
            duplicates += 1
            reason_counts[result.reason] = reason_counts.get(result.reason, 0) + 1
    return len(rows), duplicates, reason_counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Authorize or verify the allowlisted outreach Gmail account. No draft writes."
    )
    parser.add_argument("action", choices=("status", "authorize", "check-duplicates"))
    parser.add_argument("--candidates", type=Path)
    args = parser.parse_args(argv)
    try:
        connection = authorize_interactive(root=ROOT) if args.action == "authorize" else connect_existing(root=ROOT)
        if args.action == "check-duplicates":
            if args.candidates is None:
                parser.error("check-duplicates requires --candidates")
            total, duplicates, reasons = _duplicate_counts(args.candidates, connection.service)
            reason_text = ",".join(f"{key}:{value}" for key, value in sorted(reasons.items())) or "none"
            print(
                f"status=CHECKED account={connection.email_address} candidates={total} "
                f"duplicates={duplicates} reasons={reason_text} draft_writes=0 sends=0"
            )
            return 0
    except GmailOAuthError as exc:
        expected = expected_gmail_account()
        print(f"status=REFUSED expected_account={expected} reason={exc}", file=sys.stderr)
        return 2
    print(
        f"status=CONNECTED account={connection.email_address} "
        f"scope_count={len(connection.scopes)} draft_writes=0 sends=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
