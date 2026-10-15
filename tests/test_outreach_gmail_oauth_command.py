from __future__ import annotations

import csv
from pathlib import Path

from scripts import outreach_gmail_oauth as command
from src.outreach.gmail_drafts import ExistingDraft, build_draft_key, DraftIdentity


def _write_csv(path: Path) -> None:
    fields = [
        "clinic_uuid", "email_candidate_id", "to_email", "template",
        "recommended_url", "listed_url", "subject",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        writer.writerow(dict(zip(fields, [
            "uuid", "candidate", "clinic@example.com", "V2",
            "https://example.test/article", "", "subject",
        ])))
    path.chmod(0o600)


def test_duplicate_command_helper_reports_counts_without_recipient(monkeypatch, tmp_path):
    candidate_file = tmp_path / "canary.csv"
    _write_csv(candidate_file)
    identity = DraftIdentity(
        "uuid", "candidate", "clinic@example.com", "V2", "https://example.test/article"
    )
    monkeypatch.setattr(command, "list_existing_drafts", lambda service: [
        ExistingDraft("draft-id", build_draft_key(identity), (), "subject")
    ])
    total, duplicates, reasons = command._duplicate_counts(candidate_file, object())
    assert (total, duplicates) == (1, 1)
    assert reasons == {"DRAFT_KEY_MATCH": 1}


def test_duplicate_command_helper_rejects_non_private_csv(tmp_path):
    candidate_file = tmp_path / "canary.csv"
    _write_csv(candidate_file)
    candidate_file.chmod(0o644)
    try:
        command._duplicate_counts(candidate_file, object())
    except command.GmailOAuthError as exc:
        assert "0600" in str(exc)
    else:
        raise AssertionError("insecure Canary CSV was accepted")
