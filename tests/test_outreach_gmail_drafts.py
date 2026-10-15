from __future__ import annotations

from src.outreach.gmail_drafts import (
    DraftIdentity,
    ExistingDraft,
    build_draft_key,
    check_duplicate,
    list_existing_drafts,
)


class _Response:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _DraftApi:
    def __init__(self):
        self.list_calls = []
        self.get_calls = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        if kwargs.get("pageToken") == "page-2":
            return _Response({"drafts": [{"id": "d2"}]})
        return _Response({"drafts": [{"id": "d1"}], "nextPageToken": "page-2"})

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        values = {
            "d1": [
                {"name": "To", "value": "Clinic <Clinic@Example.com>"},
                {"name": "Subject", "value": "subject-1"},
                {"name": "X-Clinic-Lead-Draft-Key", "value": "key-1"},
            ],
            "d2": [{"name": "To", "value": "other@example.com"}],
        }
        return _Response({"message": {"payload": {"headers": values[kwargs["id"]]}}})


class _Service:
    def __init__(self):
        self.api = _DraftApi()

    def users(self):
        return self

    def drafts(self):
        return self.api


def _identity(recipient="clinic@example.com"):
    return DraftIdentity("UUID-1", "candidate-7", recipient, "RECOMMEND_RELATED_V2", "https://example.test/a")


def test_draft_key_is_deterministic_and_sensitive_to_identity_fields():
    assert build_draft_key(_identity("CLINIC@example.com")) == build_draft_key(_identity())
    assert build_draft_key(_identity("other@example.com")) != build_draft_key(_identity())


def test_list_existing_drafts_reads_metadata_only_and_paginates():
    service = _Service()
    drafts = list_existing_drafts(service)
    assert [item.draft_id for item in drafts] == ["d1", "d2"]
    assert drafts[0].recipients == ("clinic@example.com",)
    assert service.api.list_calls == [
        {"userId": "me", "maxResults": 100},
        {"userId": "me", "maxResults": 100, "pageToken": "page-2"},
    ]
    assert all(call["format"] == "metadata" for call in service.api.get_calls)
    assert all("X-Clinic-Lead-Draft-Key" in call["metadataHeaders"] for call in service.api.get_calls)


def test_duplicate_exact_key_blocks_creation():
    identity = _identity()
    existing = [ExistingDraft("d1", build_draft_key(identity), (), "unrelated")]
    assert check_duplicate(identity, subject="subject", existing=existing).reason == "DRAFT_KEY_MATCH"


def test_duplicate_recipient_and_subject_is_conservative_fallback():
    existing = [ExistingDraft("d2", "", ("clinic@example.com",), "subject")]
    result = check_duplicate(_identity(), subject="subject", existing=existing)
    assert result.duplicate is True
    assert result.reason == "RECIPIENT_SUBJECT_MATCH"


def test_no_duplicate_when_neither_key_nor_recipient_subject_match():
    existing = [ExistingDraft("d3", "other-key", ("other@example.com",), "subject")]
    assert check_duplicate(_identity(), subject="subject", existing=existing).duplicate is False
