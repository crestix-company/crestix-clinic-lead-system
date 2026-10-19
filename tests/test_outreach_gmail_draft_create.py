from __future__ import annotations

import base64
import json
import stat
from datetime import date, datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest

from scripts import outreach_gmail_create_drafts as draft_cli
from src.outreach import email_template_v2
from src.outreach.email_template_v2 import ARTICLE_PRESENT, SUBJECT, TEMPLATE_VERSION, PreviewInput, render_preview
from src.outreach.gmail_drafts import (
    BATCH_CONFIRMATION,
    CANARY_CONFIRMATION,
    CANARY_ACCOUNT,
    ApprovedDraftRequest,
    DraftSafetyError,
    ExistingDraft,
    build_draft_key,
    create_approved_draft,
)
from src.outreach.gmail_oauth import GmailConnection, REQUIRED_SCOPES

AS_OF = date(2026, 10, 16)
ARTICLE = "https://clinic.mynavi.jp/article/chofushi_orthodontics/"
DETAIL = "https://clinic.mynavi.jp/clinic/ihff0m1f1d12/"
SCHEDULE = ("2026-10-19 10:00", "2026-10-20 14:00")


class _FixedDateTime(datetime):
    """Freezes only `email_template_v2.datetime.now()` so SCHEDULE above stays "future"
    relative to AS_OF regardless of the real wall clock; no production behavior changes."""

    @classmethod
    def now(cls, tz=None):
        fixed = cls(2026, 10, 16, 9, 0)
        return fixed.astimezone(tz) if tz else fixed


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    monkeypatch.setattr(email_template_v2, "datetime", _FixedDateTime)


class _Response:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _DraftApi:
    def __init__(self, *, existing=(), save_then_timeout=False, timeout_without_save=False,
                 add_unrelated_before_timeout=False):
        self.existing = list(existing)
        self.create_calls = []
        self.list_calls = 0
        self.get_calls = []
        self.save_then_timeout = save_then_timeout
        self.timeout_without_save = timeout_without_save
        self.add_unrelated_before_timeout = add_unrelated_before_timeout

    def list(self, **kwargs):
        self.list_calls += 1
        return _Response({"drafts": [{"id": item[0]} for item in self.existing]})

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        draft_id = kwargs["id"]
        item = next(item for item in self.existing if item[0] == draft_id)
        return _Response({"message": {"payload": {"headers": item[1]}}})

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        if self.add_unrelated_before_timeout:
            self.existing.append(("manual-draft", [
                {"name": "To", "value": CANARY_ACCOUNT},
                {"name": "Subject", "value": SUBJECT},
                {"name": "X-Clinic-Lead-Draft-Key", "value": "unrelated-key"},
            ]))
        if not self.timeout_without_save:
            raw = kwargs["body"]["message"]["raw"]
            message = BytesParser(policy=policy.default).parsebytes(
                base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
            )
            self.existing.append(("draft-001", [
                {"name": "To", "value": message["To"]},
                {"name": "Subject", "value": str(message["Subject"])},
                {"name": "X-Clinic-Lead-Draft-Key", "value": message["X-Clinic-Lead-Draft-Key"]},
            ]))
        if self.save_then_timeout or self.timeout_without_save:
            raise TimeoutError("mocked timeout")
        return _Response({"id": "draft-001", "message": {"id": "message-001"}})


class _Service:
    def __init__(self, api):
        self.api = api

    def users(self):
        return self

    def drafts(self):
        return self.api


def _request(*, human_status="承認", recipient=CANARY_ACCOUNT, body=None, subject=SUBJECT):
    preview = render_preview(PreviewInput(
        clinic_name="ふじた矯正歯科クリニック",
        director_name="確認済み院長",
        director_name_verified=True,
        article_status=ARTICLE_PRESENT,
        actual_article_url=ARTICLE,
        article_listing_verified=True,
        clinic_detail_url=DETAIL,
        clinic_detail_verified=True,
        schedule=SCHEDULE,
        sender_name="高原",
        phone_call_date=AS_OF,
        email_send_request_confirmed=True,
        recipient_email_verified=True,
        gmail_account_verified=True,
        human_approved=True,
        duplicate_check_complete=True,
        article_master_urls=frozenset({ARTICLE}),
        as_of=AS_OF,
    ))
    return ApprovedDraftRequest(
        clinic_uuid="clinic-uuid-1",
        email_candidate_id="candidate-1",
        clinic_name="ふじた矯正歯科クリニック",
        director_name="確認済み院長",
        recipient=recipient,
        subject=subject,
        v2_body=body if body is not None else preview.body,
        human_review_status=human_status,
        template_version=TEMPLATE_VERSION,
        article_status=ARTICLE_PRESENT,
        article_evidence_url=ARTICLE,
        article_evidence_method="医院詳細ページと紹介記事の照合",
        article_checked_at="2026-10-16T12:00:00+09:00",
        actual_article_url=ARTICLE,
        clinic_detail_url=DETAIL,
        clinic_detail_verified=True,
        article_listing_verified=True,
        director_name_verified=True,
        recipient_email_verified=True,
        schedule=SCHEDULE,
        phone_call_date=AS_OF,
        email_send_request_confirmed=True,
        article_master_urls=frozenset({ARTICLE}),
    )


def _connection(api, account="info@crestix-inc.com"):
    return GmailConnection(_Service(api), account, REQUIRED_SCOPES)


def _execute(request, api, ledger, **kwargs):
    return create_approved_draft(
        request, mode="canary", connection=_connection(api), execute=True,
        confirmation=CANARY_CONFIRMATION, batch_size=1, ledger_path=ledger,
        as_of=AS_OF, **kwargs,
    )


def test_dry_run_is_default_and_performs_zero_gmail_writes(tmp_path):
    api = _DraftApi()
    result = create_approved_draft(
        _request(), mode="canary", connection=_connection(api), ledger_path=tmp_path / "ledger.jsonl",
        as_of=AS_OF,
    )
    assert result.status == "DRY_RUN_CONTENT_READY"
    assert api.create_calls == []
    assert api.list_calls == 0
    assert not (tmp_path / "ledger.jsonl").exists()


def test_execute_creates_utf8_mime_draft_with_exact_to_subject_body_and_key(tmp_path):
    api = _DraftApi()
    result = _execute(_request(), api, tmp_path / "ledger.jsonl")
    assert result.status == "DRAFT_CREATED"
    assert result.draft_id == "draft-001"
    assert len(api.create_calls) == 1
    raw = api.create_calls[0]["body"]["message"]["raw"]
    message = BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    )
    assert message["To"] == CANARY_ACCOUNT
    assert str(message["Subject"]) == SUBJECT
    assert message.get_content().replace("\r\n", "\n") == _request().v2_body + "\n"
    assert message["X-Clinic-Lead-Draft-Key"] == result.draft_key
    assert message["Cc"] is None and message["Bcc"] is None
    assert stat.S_IMODE((tmp_path / "ledger.jsonl").stat().st_mode) == 0o600
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    entries = [json.loads(line) for line in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert [entry["status"] for entry in entries] == ["PENDING", "CREATED"]
    assert entries[-1]["draft_id"] == "draft-001"


def test_human_approval_and_exact_v2_body_are_required_before_api_access(tmp_path):
    api = _DraftApi()
    unapproved = _execute(_request(human_status="未確認"), api, tmp_path / "ledger.jsonl")
    tampered = _execute(_request(body="legacy Phase 2 body"), api, tmp_path / "ledger.jsonl")
    assert unapproved.status == "NEEDS_REVIEW"
    assert tampered.status == "NEEDS_REVIEW"
    assert api.create_calls == []
    assert api.list_calls == 0


def test_oauth_account_mismatch_is_rejected_before_gmail_api_calls(tmp_path):
    api = _DraftApi()
    with pytest.raises(DraftSafetyError, match="許可アカウント"):
        create_approved_draft(
            _request(), mode="canary", connection=_connection(api, "kazuma.takahara@crestix-inc.com"),
            execute=True, confirmation=CANARY_CONFIRMATION, batch_size=1,
            ledger_path=tmp_path / "ledger.jsonl", as_of=AS_OF,
        )
    assert api.list_calls == 0 and api.create_calls == []


@pytest.mark.parametrize("existing,expected_reason", [
    ("key", "DRAFT_KEY_MATCH"),
    ("recipient_subject", "RECIPIENT_SUBJECT_MATCH"),
])
def test_existing_gmail_duplicate_prevents_create(tmp_path, existing, expected_reason):
    request = _request()
    key = build_draft_key(request.identity)
    headers = [
        {"name": "To", "value": CANARY_ACCOUNT},
        {"name": "Subject", "value": SUBJECT},
        {"name": "X-Clinic-Lead-Draft-Key", "value": key if existing == "key" else "other"},
    ]
    api = _DraftApi(existing=[("existing-id", headers)])
    result = _execute(request, api, tmp_path / "ledger.jsonl")
    assert result.status == "SKIPPED_DUPLICATE"
    assert result.reasons == (expected_reason,)
    assert api.create_calls == []


def test_canary_only_accepts_internal_recipient(tmp_path):
    with pytest.raises(DraftSafetyError, match="Canary recipient"):
        create_approved_draft(
            _request(recipient="clinic@example.com"), mode="canary", execute=False,
            ledger_path=tmp_path / "ledger.jsonl", as_of=AS_OF,
        )


def test_timeout_requeries_and_recovers_created_draft_without_retry(tmp_path):
    request = _request()
    api = _DraftApi(save_then_timeout=True)
    ledger = tmp_path / "ledger.jsonl"
    result = _execute(request, api, ledger)
    assert result.status == "CREATED_RECOVERED"
    assert result.draft_id == "draft-001"
    assert len(api.create_calls) == 1
    retry = _execute(request, api, ledger)
    assert retry.status == "SKIPPED_DUPLICATE"
    assert len(api.create_calls) == 1


def test_timeout_without_requery_match_is_unknown_and_never_retried(tmp_path):
    request = _request()
    api = _DraftApi(timeout_without_save=True)
    ledger = tmp_path / "ledger.jsonl"
    first = _execute(request, api, ledger)
    assert first.status == "OUTCOME_UNKNOWN"
    assert first.reasons == ("NO_AUTOMATIC_RETRY_AFTER_UNCERTAIN_CREATE",)
    retry = _execute(request, api, ledger)
    assert retry.status == "SKIPPED_DUPLICATE"
    assert len(api.create_calls) == 1


def test_timeout_does_not_misattribute_concurrent_same_subject_draft(tmp_path):
    api = _DraftApi(timeout_without_save=True, add_unrelated_before_timeout=True)
    result = _execute(_request(), api, tmp_path / "ledger.jsonl")
    assert result.status == "OUTCOME_UNKNOWN"
    assert result.draft_id == ""
    assert result.reasons == ("NO_AUTOMATIC_RETRY_AFTER_UNCERTAIN_CREATE",)
    assert len(api.create_calls) == 1


def test_batch_requires_separate_confirmation_phrase_and_exact_count(tmp_path):
    request = _request(recipient="clinic@example.com")
    api = _DraftApi()
    with pytest.raises(DraftSafetyError, match="Batch confirmation"):
        create_approved_draft(
            request, mode="batch", connection=_connection(api), execute=True,
            confirmation=BATCH_CONFIRMATION, confirmed_batch_count=2, batch_size=1,
            ledger_path=tmp_path / "ledger.jsonl", as_of=AS_OF,
        )
    assert api.create_calls == []


def test_send_api_is_not_called_or_required(tmp_path):
    api = _DraftApi()
    result = _execute(_request(), api, tmp_path / "ledger.jsonl")
    assert result.status == "DRAFT_CREATED"
    assert not hasattr(api, "send")
    assert not hasattr(_Service(api), "messages")


def test_cli_dry_run_reports_zero_writes_without_oauth_or_ledger(tmp_path, monkeypatch, capsys):
    # Keep the fixture's call date deterministic as the CLI intentionally uses today's
    # date when no --as-of override is provided.
    from src.outreach import gmail_drafts
    class FixedDate(date):
        @classmethod
        def today(cls):
            return AS_OF
    monkeypatch.setattr(gmail_drafts, "date", FixedDate)
    request = _request()
    monkeypatch.setattr(draft_cli, "_load_article_master", lambda _path: frozenset({ARTICLE}))
    monkeypatch.setattr(draft_cli, "load_approved_candidates", lambda *_args, **_kwargs: [request])
    monkeypatch.setattr(draft_cli, "connect_existing", lambda **_kwargs: pytest.fail("OAuth must not run"))
    ledger = tmp_path / "ledger.jsonl"

    code = draft_cli.main([
        "canary", "--candidates", str(tmp_path / "candidate.csv"), "--ledger", str(ledger),
    ])

    assert code == 0
    output = capsys.readouterr().out
    assert "DRY_RUN_CONTENT_READY:1" in output
    assert "draft_writes=0 sends=0" in output
    assert "NOT_CHECKED_DRY_RUN" in output
    assert not ledger.exists()


def test_cli_live_write_requires_exact_confirmation_before_oauth(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(draft_cli, "_load_article_master", lambda _path: frozenset({ARTICLE}))
    monkeypatch.setattr(draft_cli, "load_approved_candidates", lambda *_args, **_kwargs: [_request()])
    monkeypatch.setattr(draft_cli, "connect_existing", lambda **_kwargs: pytest.fail("OAuth must not run"))

    code = draft_cli.main([
        "canary", "--candidates", str(tmp_path / "candidate.csv"), "--execute",
    ])

    assert code == 2
    assert "status=REFUSED" in capsys.readouterr().err
