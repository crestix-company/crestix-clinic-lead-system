import json

import pytest

from src.outreach.shared_draft_runtime_factory import (
    RuntimeConfigurationError, build_runtime_dependencies,
)


def test_missing_secrets_fail_before_database_or_gmail_factories():
    calls = []
    def pool_factory(*args, **kwargs):
        calls.append("pool")
    def gmail_factory(*args, **kwargs):
        calls.append("gmail")
    with pytest.raises(RuntimeConfigurationError):
        build_runtime_dependencies({}, pool_factory=pool_factory,
                                   gmail_service_factory=gmail_factory)
    assert calls == []


def test_runtime_feature_flag_missing_or_false_fails_before_database_or_gmail():
    calls = []
    env = {
        "SHARED_DRAFT_DATABASE_URL": "postgresql://test",
        "SHARED_DRAFT_API_TOKENS_JSON": json.dumps({"clinic": {"token": "synthetic", "roles": ["draft:create"]}}),
        "GMAIL_DWD_SERVICE_ACCOUNT_JSON": "synthetic-secret",
    }
    for value in (None, "false", "1", "yes"):
        candidate = dict(env)
        if value is not None:
            candidate["SHARED_DRAFT_API_RUNTIME_ENABLED"] = value
        with pytest.raises(RuntimeConfigurationError):
            build_runtime_dependencies(candidate, pool_factory=lambda *a, **k: calls.append("pool"),
                                       gmail_service_factory=lambda *a, **k: calls.append("gmail"))
    assert calls == []


def test_gmail_credential_missing_does_not_initialize_gmail_or_pool():
    calls = []
    env = {"SHARED_DRAFT_DATABASE_URL": "postgresql://test",
           "SHARED_DRAFT_API_TOKENS_JSON": json.dumps({"clinic": {"token": "synthetic", "roles": ["draft:create"]}})}
    with pytest.raises(RuntimeConfigurationError):
        build_runtime_dependencies(env, pool_factory=lambda *a, **k: calls.append("pool"),
                                   gmail_service_factory=lambda *a, **k: calls.append("gmail"))
    assert calls == []


def test_runtime_dependencies_use_injected_pool_and_gmail_mock():
    class Pool:
        def __init__(self):
            self.closed = False
        def close(self):
            self.closed = True
    pool, gmail_calls = Pool(), []
    env = {
        "SHARED_DRAFT_API_RUNTIME_ENABLED": "true",
        "SHARED_DRAFT_DATABASE_URL": "postgresql://synthetic-local-only",
        "SHARED_DRAFT_API_TOKENS_JSON": json.dumps({
            "clinic": {"token": "synthetic-token", "roles": ["draft:create"],
                       "clinic_ids": [101], "campaign_ids": ["campaign-a"]}
        }),
        "GMAIL_DWD_SERVICE_ACCOUNT_JSON": "synthetic-service-account-secret",
        "SHARED_DRAFT_GMAIL_MAILBOX": "drafts@example.invalid",
    }
    dependencies = build_runtime_dependencies(
        env, pool_factory=lambda _dsn: pool,
        gmail_service_factory=lambda secret, **kw: gmail_calls.append((secret, kw)) or object(),
        audit=lambda event: None,
    )
    principal = dependencies.token_verifier.verify("synthetic-token")
    assert principal.caller_id == "clinic"
    assert principal.clinic_ids == frozenset({101})
    assert principal.campaign_ids == frozenset({"campaign-a"})
    assert gmail_calls and gmail_calls[0][0] == "synthetic-service-account-secret"
    assert dependencies.store.pool is pool


def test_invalid_secret_json_fails_before_opening_database():
    called = []
    env = {"SHARED_DRAFT_DATABASE_URL": "unused",
           "SHARED_DRAFT_API_RUNTIME_ENABLED": "true",
           "SHARED_DRAFT_API_TOKENS_JSON": "not-json",
           "GMAIL_DWD_SERVICE_ACCOUNT_JSON": "not-used"}
    with pytest.raises(RuntimeConfigurationError):
        build_runtime_dependencies(env, pool_factory=lambda *a: called.append("pool"),
                                   gmail_service_factory=lambda *a, **k: called.append("gmail"))
    assert called == []

