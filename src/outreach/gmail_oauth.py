"""Gmail OAuth boundary for outreach draft preparation.

This module deliberately exposes no Gmail send or draft-create operation.  It only
establishes an account-verified service that other read-only preparation code can use.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

EXPECTED_GMAIL_ACCOUNT = "kazuma.takahara@crestix-inc.com"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
REQUIRED_SCOPES = (GMAIL_COMPOSE_SCOPE,)
CLIENT_FILE_ENV = "OUTREACH_GMAIL_OAUTH_CLIENT_JSON"
TOKEN_FILE_ENV = "OUTREACH_GMAIL_OAUTH_TOKEN_JSON"
DEFAULT_TOKEN_FILE = Path(".secrets/outreach_gmail/token.json")


class GmailOAuthError(RuntimeError):
    """Safe OAuth failure whose message never contains credential material."""


class GmailAccountMismatch(GmailOAuthError):
    """The authenticated mailbox is not the explicitly allowed mailbox."""


@dataclass(frozen=True)
class GmailConnection:
    service: Any
    email_address: str
    scopes: tuple[str, ...]


def oauth_paths(*, root: Path | None = None) -> tuple[Path, Path]:
    base = (root or Path.cwd()).resolve()
    client_value = os.environ.get(CLIENT_FILE_ENV, "").strip()
    if not client_value:
        raise GmailOAuthError(f"{CLIENT_FILE_ENV} is not configured")
    client_path = Path(client_value).expanduser()
    token_value = os.environ.get(TOKEN_FILE_ENV, "").strip()
    token_path = Path(token_value).expanduser() if token_value else base / DEFAULT_TOKEN_FILE
    return client_path.resolve(), token_path.resolve()


def _reject_unsafe_permissions(path: Path, *, label: str) -> None:
    if not path.is_file():
        raise GmailOAuthError(f"{label} file is not available")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise GmailOAuthError(f"{label} file permissions must be 0600")


def _secure_token_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)


def _write_token_atomic(path: Path, token_json: str) -> None:
    """Atomically rotate the local token without ever making it group/world readable."""
    _secure_token_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".token-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(token_json)
        os.replace(temporary, path)
        path.chmod(0o600)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


def _google_dependencies() -> tuple[Any, Any, Any, Any]:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - environment-specific message
        raise GmailOAuthError("Google OAuth dependencies are not installed") from exc
    return Request, Credentials, InstalledAppFlow, build


def _verified_connection(
    credentials: Any,
    *,
    build_service: Callable[..., Any],
) -> GmailConnection:
    granted = set(getattr(credentials, "granted_scopes", None) or getattr(credentials, "scopes", None) or ())
    if granted != set(REQUIRED_SCOPES) or not credentials.has_scopes(REQUIRED_SCOPES):
        raise GmailOAuthError("OAuth token scopes do not exactly match the required Gmail scope")
    try:
        service = build_service("gmail", "v1", credentials=credentials, cache_discovery=False)
        profile = service.users().getProfile(userId="me").execute()
    except Exception as exc:
        raise GmailOAuthError("Gmail profile verification failed") from exc
    actual = str(profile.get("emailAddress", "")).strip().casefold()
    if actual != EXPECTED_GMAIL_ACCOUNT.casefold():
        raise GmailAccountMismatch("Authenticated Gmail account is not the required account")
    return GmailConnection(service=service, email_address=actual, scopes=REQUIRED_SCOPES)


def connect_existing(*, root: Path | None = None) -> GmailConnection:
    """Load an existing token, refresh in memory, and verify the Gmail profile.

    A refreshed token is persisted only after the Gmail profile matches the allowlisted
    account.  This method performs no Gmail writes.
    """
    client_path, token_path = oauth_paths(root=root)
    _reject_unsafe_permissions(client_path, label="OAuth client")
    _reject_unsafe_permissions(token_path, label="OAuth token")
    Request, Credentials, _InstalledAppFlow, build = _google_dependencies()
    try:
        token_info = json.loads(token_path.read_text(encoding="utf-8"))
        saved_scopes = token_info.get("scopes", ())
        if isinstance(saved_scopes, str):
            saved_scopes = saved_scopes.split()
        if set(saved_scopes) != set(REQUIRED_SCOPES):
            raise GmailOAuthError("Saved OAuth scopes are not the required minimal scope")
        credentials = Credentials.from_authorized_user_info(token_info, REQUIRED_SCOPES)
    except GmailOAuthError:
        raise
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise GmailOAuthError("OAuth token file is invalid") from exc
    refreshed = False
    if not credentials.valid:
        if not credentials.expired or not credentials.refresh_token:
            raise GmailOAuthError("OAuth token requires interactive authorization")
        try:
            credentials.refresh(Request())
        except Exception as exc:
            raise GmailOAuthError("OAuth token refresh failed") from exc
        refreshed = True
    connection = _verified_connection(credentials, build_service=build)
    if refreshed:
        _write_token_atomic(token_path, credentials.to_json())
    return connection


def authorize_interactive(*, root: Path | None = None) -> GmailConnection:
    """Run installed-app OAuth and persist only an allowlisted account's token."""
    client_path, token_path = oauth_paths(root=root)
    _reject_unsafe_permissions(client_path, label="OAuth client")
    _Request, _Credentials, InstalledAppFlow, build = _google_dependencies()
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(client_path), REQUIRED_SCOPES)
        credentials = flow.run_local_server(
            host="127.0.0.1",
            port=0,
            open_browser=True,
            access_type="offline",
            prompt="consent",
            authorization_prompt_message="Google OAuth authorization is required in the browser.",
            success_message="OAuth response received. You may close this browser window.",
        )
    except Exception as exc:
        raise GmailOAuthError("Interactive OAuth authorization failed") from exc
    connection = _verified_connection(credentials, build_service=build)
    _write_token_atomic(token_path, credentials.to_json())
    return connection
