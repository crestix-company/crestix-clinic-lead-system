from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from src.outreach import gmail_oauth


class _Request:
    def execute(self):
        return self.value

    def __init__(self, value):
        self.value = value


class _Service:
    def __init__(self, account: str):
        self.account = account

    def users(self):
        return self

    def getProfile(self, *, userId):
        assert userId == "me"
        return _Request({"emailAddress": self.account})


class _Credentials:
    valid = True
    expired = False
    refresh_token = "refresh-value"
    scopes = gmail_oauth.REQUIRED_SCOPES
    granted_scopes = None

    def __init__(self, token_json='{"refresh_token":"secret"}'):
        self.token_json = token_json

    def has_scopes(self, scopes):
        return tuple(scopes) == gmail_oauth.REQUIRED_SCOPES

    def to_json(self):
        return self.token_json


class _CredentialLoader:
    credential = _Credentials()

    @classmethod
    def from_authorized_user_info(cls, info, scopes):
        assert tuple(scopes) == gmail_oauth.REQUIRED_SCOPES
        return cls.credential


def _private_file(path: Path, value: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value or '{"scopes":["https://www.googleapis.com/auth/gmail.compose",'
                    '"https://www.googleapis.com/auth/gmail.readonly"]}', encoding="utf-8")
    path.chmod(0o600)
    return path


def _configure(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    client = _private_file(tmp_path / "private" / "client.json")
    token = _private_file(tmp_path / "private" / "token.json")
    monkeypatch.setenv(gmail_oauth.CLIENT_FILE_ENV, str(client))
    monkeypatch.setenv(gmail_oauth.TOKEN_FILE_ENV, str(token))
    return client, token


def test_scopes_are_limited_to_draft_creation_and_mailbox_history_reading():
    assert gmail_oauth.REQUIRED_SCOPES == (
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.readonly",
    )


def test_default_expected_account_is_info_and_is_configurable(monkeypatch):
    monkeypatch.delenv(gmail_oauth.EXPECTED_GMAIL_ACCOUNT_ENV, raising=False)
    assert gmail_oauth.expected_gmail_account() == "info@crestix-inc.com"
    monkeypatch.setenv(gmail_oauth.EXPECTED_GMAIL_ACCOUNT_ENV, "sales@example.com")
    assert gmail_oauth.expected_gmail_account() == "sales@example.com"


def test_connect_existing_accepts_only_expected_account(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(
        gmail_oauth,
        "_google_dependencies",
        lambda: (object, _CredentialLoader, object, lambda *a, **k: _Service(gmail_oauth.EXPECTED_GMAIL_ACCOUNT)),
    )
    connection = gmail_oauth.connect_existing(root=tmp_path)
    assert connection.email_address == gmail_oauth.EXPECTED_GMAIL_ACCOUNT


def test_connect_existing_rejects_old_takahara_account(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.delenv(gmail_oauth.EXPECTED_GMAIL_ACCOUNT_ENV, raising=False)
    monkeypatch.setattr(
        gmail_oauth,
        "_google_dependencies",
        lambda: (object, _CredentialLoader, object, lambda *a, **k: _Service("kazuma.takahara@crestix-inc.com")),
    )
    with pytest.raises(gmail_oauth.GmailAccountMismatch):
        gmail_oauth.connect_existing(root=tmp_path)


def test_interactive_account_mismatch_refuses_and_does_not_save(monkeypatch, tmp_path):
    client = _private_file(tmp_path / "private" / "client.json")
    token = tmp_path / "private" / "new-token.json"
    monkeypatch.setenv(gmail_oauth.CLIENT_FILE_ENV, str(client))
    monkeypatch.setenv(gmail_oauth.TOKEN_FILE_ENV, str(token))

    class Flow:
        def run_local_server(self, **kwargs):
            return _Credentials()

    class InstalledFlow:
        @staticmethod
        def from_client_secrets_file(path, scopes):
            return Flow()

    monkeypatch.setattr(
        gmail_oauth,
        "_google_dependencies",
        lambda: (object, object, InstalledFlow, lambda *a, **k: _Service("wrong@example.com")),
    )
    with pytest.raises(gmail_oauth.GmailAccountMismatch):
        gmail_oauth.authorize_interactive(root=tmp_path)
    assert not token.exists()


def test_existing_wrong_account_token_blocks_authorization_without_replacement(monkeypatch, tmp_path):
    _client, token = _configure(monkeypatch, tmp_path)
    original = token.read_bytes()
    monkeypatch.delenv(gmail_oauth.EXPECTED_GMAIL_ACCOUNT_ENV, raising=False)

    class ForbiddenInstalledFlow:
        @staticmethod
        def from_client_secrets_file(*args, **kwargs):
            pytest.fail("interactive flow must not start when existing token belongs to another account")

    monkeypatch.setattr(
        gmail_oauth,
        "_google_dependencies",
        lambda: (
            object,
            _CredentialLoader,
            ForbiddenInstalledFlow,
            lambda *a, **k: _Service("kazuma.takahara@crestix-inc.com"),
        ),
    )
    with pytest.raises(gmail_oauth.GmailAccountMismatch):
        gmail_oauth.authorize_interactive(root=tmp_path)
    assert token.read_bytes() == original


def test_interactive_saves_private_token_after_account_verification(monkeypatch, tmp_path):
    client = _private_file(tmp_path / "input" / "client.json")
    token = tmp_path / "secrets" / "token.json"
    monkeypatch.setenv(gmail_oauth.CLIENT_FILE_ENV, str(client))
    monkeypatch.setenv(gmail_oauth.TOKEN_FILE_ENV, str(token))

    class Flow:
        def run_local_server(self, **kwargs):
            assert kwargs["host"] == "127.0.0.1"
            return _Credentials()

    class InstalledFlow:
        @staticmethod
        def from_client_secrets_file(path, scopes):
            return Flow()

    monkeypatch.setattr(
        gmail_oauth,
        "_google_dependencies",
        lambda: (
            object,
            object,
            InstalledFlow,
            lambda *a, **k: _Service(gmail_oauth.EXPECTED_GMAIL_ACCOUNT),
        ),
    )
    gmail_oauth.authorize_interactive(root=tmp_path)
    assert stat.S_IMODE(token.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(token.stat().st_mode) == 0o600


def test_insecure_token_permissions_are_rejected(monkeypatch, tmp_path):
    _client, token = _configure(monkeypatch, tmp_path)
    token.chmod(0o644)
    with pytest.raises(gmail_oauth.GmailOAuthError, match="0600"):
        gmail_oauth.connect_existing(root=tmp_path)


def test_saved_broader_gmail_scope_is_rejected(monkeypatch, tmp_path):
    client = _private_file(tmp_path / "private" / "client.json")
    token = _private_file(
        tmp_path / "private" / "token.json",
        '{"scopes":["https://www.googleapis.com/auth/gmail.compose",'
        '"https://www.googleapis.com/auth/gmail.readonly",'
        '"https://www.googleapis.com/auth/gmail.modify"]}',
    )
    monkeypatch.setenv(gmail_oauth.CLIENT_FILE_ENV, str(client))
    monkeypatch.setenv(gmail_oauth.TOKEN_FILE_ENV, str(token))
    monkeypatch.setattr(
        gmail_oauth,
        "_google_dependencies",
        lambda: (object, _CredentialLoader, object, lambda *a, **k: _Service("unused")),
    )
    with pytest.raises(gmail_oauth.GmailOAuthError, match="minimal scope"):
        gmail_oauth.connect_existing(root=tmp_path)


def test_default_token_location_is_git_ignored(monkeypatch, tmp_path):
    client = _private_file(tmp_path / "client.json")
    monkeypatch.setenv(gmail_oauth.CLIENT_FILE_ENV, str(client))
    monkeypatch.delenv(gmail_oauth.TOKEN_FILE_ENV, raising=False)
    _client_path, token_path = gmail_oauth.oauth_paths(root=tmp_path)
    assert token_path == (tmp_path / ".secrets/outreach_gmail/token.json").resolve()
