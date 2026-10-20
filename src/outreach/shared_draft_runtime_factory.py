"""Production-shaped dependency composition for the isolated Draft API service.

All provider access is opt-in through explicit environment/Secret Manager values.
Importing this module does not connect to Supabase or Gmail.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from email.message import EmailMessage
import json
import logging
import os
import sys
from threading import Lock
from typing import Any, Callable

from src.outreach.draft_creation_api import MailboxSnapshot, SharedDraftCreationAPI
from src.outreach.gmail_drafts import list_existing_drafts, list_existing_messages
from src.outreach.gmail_oauth import DEFAULT_EXPECTED_GMAIL_ACCOUNT
from src.outreach.shared_draft_http_api import (
    Principal, create_app,
)
from src.repository.shared_draft_lease_repository import SharedDraftLeaseRepository

logger = logging.getLogger("shared_draft_api")


class RuntimeConfigurationError(RuntimeError):
    """Safe configuration error; never includes secret values."""


@dataclass(frozen=True)
class RuntimeDependencies:
    store: Any
    inspect_mailbox: Callable
    create_gmail_draft: Callable
    identity_resolver: Any
    token_verifier: Any
    rate_limiter: Any
    audit: Callable


class PooledLeaseStore:
    """Uses a separate pooled DB connection for each fenced repository call."""
    def __init__(self, pool):
        self.pool = pool

    def _call(self, method: str, **kwargs):
        with self.pool.connection() as connection:
            return getattr(SharedDraftLeaseRepository(connection), method)(**kwargs)

    def acquire(self, **kwargs):
        return self._call("acquire", **kwargs)

    def complete(self, **kwargs):
        return self._call("complete", **kwargs)

    def reconcile_existing(self, **kwargs):
        return self._call("reconcile_existing", **kwargs)

    def mark_unknown(self, **kwargs):
        return self._call("mark_unknown", **kwargs)


class PooledIdentityResolver:
    def __init__(self, pool):
        self.pool = pool

    def confirmed_uuid(self, clinic_id: int) -> str | None:
        with self.pool.connection() as connection:
            row = connection.execute("SELECT uuid FROM public.clinics WHERE id=%s", (int(clinic_id),)).fetchone()
        return str(row[0]).strip() if row and row[0] else None


class JsonSecretTokenVerifier:
    """Bearer tokens provided by a Secret Manager mounted/injected JSON secret."""
    def __init__(self, payload: str):
        from src.outreach.shared_draft_http_api import constant_time_token_verifier
        try:
            raw = json.loads(payload)
            tokens = {}
            scopes = {}
            for caller_id, entry in raw.items():
                if not caller_id or not isinstance(entry, dict) or not entry.get("token"):
                    raise ValueError("invalid caller")
                tokens[str(caller_id)] = (
                    str(entry["token"]),
                    frozenset(str(role) for role in entry.get("roles", [])),
                )
                scopes[str(caller_id)] = (
                    frozenset(map(int, entry["clinic_ids"])) if entry.get("clinic_ids") is not None else None,
                    frozenset(map(str, entry["campaign_ids"])) if entry.get("campaign_ids") is not None else None,
                )
            if not tokens:
                raise ValueError("no callers")
        except Exception:
            raise RuntimeConfigurationError("SHARED_DRAFT_API_TOKENS_JSON is invalid") from None
        self._delegate = constant_time_token_verifier(tokens)
        self._scopes = scopes

    def verify(self, token: str) -> Principal | None:
        principal = self._delegate.verify(token)
        if principal is None:
            return None
        clinic_ids, campaign_ids = self._scopes[principal.caller_id]
        return Principal(principal.caller_id, principal.roles, clinic_ids, campaign_ids)


class GoogleGmailDraftGateway:
    """Thread-safe Gmail read/reconcile/create adapter. It never sends mail."""
    def __init__(self, service, *, mailbox: str, timeout_seconds: int = 20):
        self.service = service
        self.mailbox = mailbox
        self.timeout_seconds = timeout_seconds
        self._lock = Lock()  # Service object safety only; DB lease is cross-instance lock.

    def inspect(self, request) -> MailboxSnapshot:
        with self._lock:
            drafts = list_existing_drafts(self.service)
            scheduled = list_existing_messages(self.service, query="in:scheduled")
            sent = list_existing_messages(self.service, query=f"in:sent to:{request.recipient}")
            trashed = list_existing_messages(self.service, query="in:trash")
            replies = list_existing_messages(self.service, query=f"in:anywhere from:{request.recipient}")
        return MailboxSnapshot(tuple(drafts), tuple(scheduled), tuple(sent), tuple(trashed), tuple(replies), True)

    def create(self, request) -> tuple[str, str, str]:
        message = EmailMessage()
        message["To"] = request.recipient
        message["From"] = self.mailbox
        message["Subject"] = request.subject
        for header, value in request.gmail_headers.items():
            if value:
                message[header] = value
        message.set_content(request.body, subtype="plain", charset="utf-8")
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
        with self._lock:
            response = self.service.users().drafts().create(
                userId="me", body={"message": {"raw": raw}}
            ).execute()
        draft_id = str(response.get("id") or "")
        gmail_message = response.get("message") or {}
        message_id = str(gmail_message.get("id") or "")
        thread_id = str(gmail_message.get("threadId") or "")
        if not draft_id:
            raise TimeoutError("Gmail did not confirm Draft ID")
        return draft_id, message_id, thread_id


def _service_account_gmail(service_account_json: str, *, mailbox: str, timeout_seconds: int):
    """Build Gmail API with DWD OAuth; caller must supply Secret Manager JSON."""
    try:
        from google.oauth2 import service_account
        from google_auth_httplib2 import AuthorizedHttp
        import httplib2
        from googleapiclient.discovery import build
        from src.outreach.gmail_oauth import REQUIRED_SCOPES
        info = json.loads(service_account_json)
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=list(REQUIRED_SCOPES)
        ).with_subject(mailbox)
        http = AuthorizedHttp(credentials, http=httplib2.Http(timeout=timeout_seconds))
        service = build("gmail", "v1", http=http, cache_discovery=False)
        # Verify OAuth principal/mailbox only after every required secret is present.
        actual = service.users().getProfile(userId="me").execute().get("emailAddress", "")
        if str(actual).strip().casefold() != mailbox.casefold():
            raise RuntimeConfigurationError("Gmail OAuth mailbox does not match configured mailbox")
        return service
    except RuntimeConfigurationError:
        raise
    except Exception:
        raise RuntimeConfigurationError("Gmail OAuth initialization failed") from None


def build_runtime_dependencies(environ=None, *, pool_factory=None,
                               gmail_service_factory=None, audit=None) -> RuntimeDependencies:
    """Create providers only after all credentials/configuration are validated."""
    env = os.environ if environ is None else environ
    runtime_enabled = str(env.get("SHARED_DRAFT_API_RUNTIME_ENABLED", "false")).strip().casefold()
    if runtime_enabled != "true":
        raise RuntimeConfigurationError("Shared Draft API runtime feature flag is not enabled")
    dsn = str(env.get("SHARED_DRAFT_DATABASE_URL", "")).strip()
    token_json = str(env.get("SHARED_DRAFT_API_TOKENS_JSON", "")).strip()
    gmail_secret = str(env.get("GMAIL_DWD_SERVICE_ACCOUNT_JSON", "")).strip()
    mailbox = str(env.get("SHARED_DRAFT_GMAIL_MAILBOX", DEFAULT_EXPECTED_GMAIL_ACCOUNT)).strip()
    if not dsn or not token_json or not gmail_secret or not mailbox:
        raise RuntimeConfigurationError(
            "Database, API token, Gmail OAuth secret, and mailbox configuration are required"
        )
    if "@" not in mailbox or any(ch.isspace() for ch in mailbox):
        raise RuntimeConfigurationError("Configured Gmail mailbox is invalid")
    token_verifier = JsonSecretTokenVerifier(token_json)
    preflight_pool = pool_factory is None
    if pool_factory is None:
        try:
            from psycopg_pool import ConnectionPool
        except ImportError:
            raise RuntimeConfigurationError("psycopg pool dependency is unavailable") from None
        def configure_runtime_role(connection):
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE clinic_outreach_runtime")
                cursor.execute("SELECT current_role")
                role = cursor.fetchone()[0]
            if str(role).casefold() != "clinic_outreach_runtime":
                raise RuntimeConfigurationError("Restricted Supabase runtime role assumption failed")

        pool_factory = lambda url: ConnectionPool(conninfo=url, min_size=0, max_size=8,
                                                  timeout=5, configure=configure_runtime_role, open=True)
    if gmail_service_factory is None:
        gmail_service_factory = _service_account_gmail
    pool = pool_factory(dsn)
    try:
        if preflight_pool:
            # Validate the restricted role, schema/function access, and identity
            # lookup before the Gmail factory can perform its profile read.
            with pool.connection() as connection:
                row = connection.execute(
                    "SELECT current_role,"
                    "has_function_privilege(current_user,'outreach.acquire_shared_draft_lease(text,text,bigint,text,text,uuid,integer)','EXECUTE'),"
                    "has_function_privilege(current_user,'outreach.consume_shared_draft_api_rate(text,timestamptz,integer)','EXECUTE')"
                ).fetchone()
                if not row or str(row[0]).casefold() != "clinic_outreach_runtime" or not row[1] or not row[2]:
                    raise RuntimeConfigurationError("Restricted Supabase role preflight failed")
                connection.execute("SELECT id,uuid FROM public.clinics LIMIT 0")
        timeout_seconds = int(env.get("SHARED_DRAFT_GMAIL_TIMEOUT_SECONDS", "20"))
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("invalid Gmail timeout")
        service = gmail_service_factory(gmail_secret, mailbox=mailbox,
                                        timeout_seconds=timeout_seconds)
    except Exception as exc:
        close = getattr(pool, "close", None)
        if close:
            close()
        # No provider details/secrets escape to the response or logs.
        if isinstance(exc, RuntimeConfigurationError):
            raise
        raise RuntimeConfigurationError("Supabase preflight or Gmail provider initialization failed") from None
    gateway = GoogleGmailDraftGateway(service, mailbox=mailbox,
                                      timeout_seconds=timeout_seconds)
    rate_window = int(env.get("SHARED_DRAFT_RATE_WINDOW_SECONDS", "60"))
    rate_limit = int(env.get("SHARED_DRAFT_RATE_LIMIT", "30"))
    sink = audit or _cloud_logging_audit
    return RuntimeDependencies(
        store=PooledLeaseStore(pool), inspect_mailbox=gateway.inspect,
        create_gmail_draft=gateway.create, identity_resolver=PooledIdentityResolver(pool),
        token_verifier=token_verifier,
        rate_limiter=PooledRateLimiter(pool),
        audit=sink,
    )


class PooledRateLimiter:
    """Acquire one pooled connection per shared atomic rate-window operation."""
    def __init__(self, pool):
        self.pool = pool

    def allow(self, key: str, *, limit: int, window_seconds: int) -> bool:
        from datetime import datetime, timezone
        if not key or not 1 <= int(limit) <= 1000 or not 1 <= int(window_seconds) <= 3600:
            return False
        now = int(datetime.now(timezone.utc).timestamp())
        start = datetime.fromtimestamp(now - now % int(window_seconds), timezone.utc)
        try:
            with self.pool.connection() as connection:
                with connection.transaction():
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT outreach.consume_shared_draft_api_rate(%s,%s,%s)",
                                       (key, start, int(limit)))
                        row = cursor.fetchone()
            return bool(row and row[0])
        except Exception:
            raise RuntimeError("shared rate limiter unavailable") from None


def _cloud_logging_audit(event):
    # Cloud Run captures structured JSON written to stdout/stderr as Cloud Logging.
    sys.stdout.write(json.dumps(dict(event), sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def create_runtime_app(environ=None, *, dependencies_factory=build_runtime_dependencies):
    env = os.environ if environ is None else environ
    dependencies = dependencies_factory(env)
    core = SharedDraftCreationAPI(store=dependencies.store,
                                  inspect_mailbox=dependencies.inspect_mailbox,
                                  create_gmail_draft=dependencies.create_gmail_draft,
                                  lease_seconds=int(env.get("SHARED_DRAFT_LEASE_SECONDS", "90")))
    return create_app(core=core, token_verifier=dependencies.token_verifier,
                      identity_resolver=dependencies.identity_resolver,
                      rate_limiter=dependencies.rate_limiter, audit=dependencies.audit,
                      rate_limit=int(env.get("SHARED_DRAFT_RATE_LIMIT", "30")),
                      rate_window_seconds=int(env.get("SHARED_DRAFT_RATE_WINDOW_SECONDS", "60")))

