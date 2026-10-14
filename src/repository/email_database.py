"""Fail-closed connection boundary for shared Email Research state.

Clinic/HP runtime credentials are never an Email DB fallback.  The Email database
must be explicitly configured and have a different identifiable PostgreSQL target.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote, urlsplit
import os
import re
import threading


EMAIL_RESEARCH_DB_URL_ENV_VAR = "EMAIL_RESEARCH_DB_URL"
RUNTIME_DB_URL_ENV_VAR = "SUPABASE_RUNTIME_DB_URL"
CLINIC_READONLY_DB_URL_ENV_VAR = "CLINIC_READONLY_DB_URL"
EMAIL_RESEARCH_DB_MODE_ENV_VAR = "EMAIL_RESEARCH_DB_MODE"
CROSS_DATABASE_MODE = "cross_database"
SAME_DATABASE_MODE = "same_database"
_VALID_DB_MODES = frozenset({CROSS_DATABASE_MODE, SAME_DATABASE_MODE})


def email_research_db_mode(environ=None) -> str:
    """Fail closed on anything but an exact, explicit opt-in to same_database mode.

    Unset, blank, or unrecognized values all resolve to cross_database -- the only mode
    that existed before this flag, and the only one that keeps today's "identity must
    differ" safety check in force without the operator asking for anything new.
    """
    env = os.environ if environ is None else environ
    value = str(env.get(EMAIL_RESEARCH_DB_MODE_ENV_VAR) or "").strip().lower()
    return value if value in _VALID_DB_MODES else CROSS_DATABASE_MODE


@dataclass(frozen=True)
class EmailDatabaseStatus:
    ready: bool
    message: str


def _database_identity(url: str):
    """Return a secret-free endpoint identity, or None when it cannot be compared."""
    try:
        parsed = urlsplit(str(url or ""))
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
            return None
        database = unquote(parsed.path.lstrip("/"))
        if not database:
            return None
        host = parsed.hostname.casefold().rstrip(".")
        port = parsed.port or 5432
        # Supabase direct and pooler URLs can name the same project through different
        # hosts. Compare project identity when it is safely recoverable; if a Supabase
        # pooler URL omits it, fail closed instead of treating its hostname as proof.
        if host.endswith(".supabase.co") and host.startswith("db."):
            project = host.split(".", 2)[1]
            return ("supabase-project", project)
        if host.endswith(".pooler.supabase.com"):
            username = unquote(parsed.username or "")
            match = re.search(r"(?:^|\.)([a-z0-9]{20,})$", username.casefold())
            if not match:
                return None
            return ("supabase-project", match.group(1))
        return ("postgres-endpoint", host, port, database)
    except (TypeError, ValueError):
        return None


def email_database_status(environ=None) -> EmailDatabaseStatus:
    env = os.environ if environ is None else environ
    email_url = (env.get(EMAIL_RESEARCH_DB_URL_ENV_VAR) or "").strip()
    runtime_url = (env.get(RUNTIME_DB_URL_ENV_VAR) or "").strip()
    if not email_url:
        return EmailDatabaseStatus(False, "Email Research用の共有DBが未接続です。")
    if not runtime_url:
        return EmailDatabaseStatus(False, "Clinic/HP DBとの分離を確認できないため開始できません。")
    email_identity = _database_identity(email_url)
    runtime_identity = _database_identity(runtime_url)
    if not email_identity or not runtime_identity:
        return EmailDatabaseStatus(False, "DB接続先の分離を確認できないため開始できません。")
    if email_identity == runtime_identity:
        mode = email_research_db_mode(env)
        if mode != SAME_DATABASE_MODE:
            return EmailDatabaseStatus(False, "Production Schema適用前のため開始できません。")
        # same_database mode: the safety boundary shifts from "a different database" to
        # "a different, least-privilege role" (clinic_email_runtime), enforced at the DB
        # grant/RLS level. Require that boundary to be visible from the connection URLs
        # themselves before trusting it -- a distinct database user is the one thing this
        # fail-closed check can still verify without opening a connection.
        email_user = unquote(urlsplit(email_url).username or "").strip().casefold()
        runtime_user = unquote(urlsplit(runtime_url).username or "").strip().casefold()
        if not email_user or not runtime_user or email_user == runtime_user:
            return EmailDatabaseStatus(
                False, "same_database modeではEmail専用roleとRuntime roleを分離してください。")
        return EmailDatabaseStatus(True, "same_database modeでEmail専用roleとして接続します。")
    return EmailDatabaseStatus(True, "Email Research用共有DBに接続できます。")


def email_target_reader_status(environ=None) -> EmailDatabaseStatus:
    env = os.environ if environ is None else environ
    email_url = (env.get(EMAIL_RESEARCH_DB_URL_ENV_VAR) or "").strip()
    read_url = (env.get(CLINIC_READONLY_DB_URL_ENV_VAR) or "").strip()
    runtime_url = (env.get(RUNTIME_DB_URL_ENV_VAR) or "").strip()
    if not read_url:
        return EmailDatabaseStatus(False, "Clinic/HP READ ONLY接続が未設定です。")
    email_identity = _database_identity(email_url)
    read_identity = _database_identity(read_url)
    runtime_identity = _database_identity(runtime_url)
    if not email_identity or not read_identity or not runtime_identity:
        return EmailDatabaseStatus(False, "Clinic/HP READ ONLY接続先との分離を確認できません。")
    if read_identity != runtime_identity:
        return EmailDatabaseStatus(False, "Clinic/HP READ ONLY接続先がClinic/HP DBと一致しません。")
    if email_identity == read_identity and email_research_db_mode(env) != SAME_DATABASE_MODE:
        return EmailDatabaseStatus(False, "ProductionとEmailのDB接続先が同一のため開始できません。")
    return EmailDatabaseStatus(True, "Clinic/HPをREAD ONLYで参照できます。")


_lock = threading.Lock()
_cached_bundle = None
_cached_key = None


def build_email_repositories(*, environ=None, connect_fn=None):
    """Create Email repositories only on the dedicated, verified Email DB connection.

    Returns ``(bundle, status)``.  An unsafe or missing configuration returns no
    repositories and never opens the Production runtime connection.
    """
    env = os.environ if environ is None else environ
    status = email_database_status(env)
    if not status.ready:
        return None, status
    email_url = env[EMAIL_RESEARCH_DB_URL_ENV_VAR].strip()
    if connect_fn is None:
        from src.repository.supabase_adapter import connect
        connect_fn = connect
    conn = connect_fn(email_url, autocommit=False)
    same_database = email_research_db_mode(env) == SAME_DATABASE_MODE
    # Canary mode: EMAIL_RESEARCH_COHORT_FILE set -> load_cohort_from_env() raises on any
    # validation failure (missing file, 0 rows, parse error, duplicates). There is no
    # except-and-fall-back-to-None here on purpose: a failure must block repository
    # construction entirely, never silently resolve to the unrestricted Production pool.
    from src.repository.email_cohort import load_cohort_from_env
    cohort = load_cohort_from_env(env)
    return _bundle_for_connection(conn, same_database=same_database, cohort=cohort,
                                   connect_fn=connect_fn), status


def _bundle_for_connection(conn, *, same_database: bool = False, cohort=None, connect_fn=None):
    from src.repository.email_research_repository import SupabaseEmailResearchRepository
    from src.repository.email_human_review_repository import SupabaseEmailHumanReviewRepository
    from src.repository.write_contracts import WriteRepositories

    return WriteRepositories(
        clinics=None, research=None, provenance=None, settings=None, jobs=None, search=None,
        email_research=SupabaseEmailResearchRepository(conn, same_database=same_database, cohort=cohort,
                                                        connect_fn=connect_fn),
        email_human_review=SupabaseEmailHumanReviewRepository(conn, connect_fn=connect_fn),
    )


def email_repositories_for_ui():
    """UI accessor that preserves the explicit Email-only connection boundary.

    Fail-soft boundary: ``build_email_repositories()`` is intentionally fail-closed
    (see its docstring) and must keep raising on any config/connectivity failure --
    callers that need the raw exception (e.g. the Auto Run worker factory) call it
    directly. This accessor is the UI-facing entry point used by every Streamlit
    fragment/page, and those callers already treat a ``None`` return as "Email
    unavailable". So any *expected*, config/connectivity-shaped failure out of the
    build call chain -- bad credential or dropped connection (``psycopg.OperationalError``
    / ``psycopg.InterfaceError``) or an invalid/missing cohort file
    (``ValueError`` from ``email_cohort.load_cohort``) -- is caught here and turned into
    the same ``None`` result, so a broken Email DB degrades the Email features only and
    never crashes the whole Streamlit app (Clinic Master / HP Research keep working).

    This must never become fail-open: on any such failure nothing is cached as a
    successful bundle, and the caught exception's text (which can contain host/IP/
    connection-string or file-path fragments) is never surfaced to the UI or logs --
    only the generic "unavailable" messages already shown by callers via
    ``email_database_status()``.

    A failure is also never cached as a permanent state: ``_cached_bundle``/``_cached_key``
    are left untouched on failure, so the very next call retries the connection/cohort
    load from scratch once the problem is fixed -- this is what lets Email features
    recover automatically once the DB (or cohort file) comes back, without a restart.

    Unexpected programming errors (``TypeError``, ``AttributeError``, ``KeyError``, etc.)
    are NOT caught here and continue to propagate, so real bugs aren't hidden.
    """
    import psycopg
    global _cached_bundle, _cached_key
    status = email_database_status()
    if not status.ready:
        return None
    cache_key = (os.environ[EMAIL_RESEARCH_DB_URL_ENV_VAR].strip(), os.environ[RUNTIME_DB_URL_ENV_VAR].strip(),
                 os.environ.get("EMAIL_RESEARCH_COHORT_FILE", "").strip())
    with _lock:
        if _cached_key == cache_key and _cached_bundle is not None:
            return _cached_bundle
        try:
            bundle, _ = build_email_repositories()
        except (psycopg.OperationalError, psycopg.InterfaceError, ValueError):
            # Do not touch _cached_bundle/_cached_key: a transient failure must never
            # be remembered as a stable "nothing to connect to" result that would
            # block recovery once the DB (or cohort file) is fixed.
            return None
        _cached_key, _cached_bundle = cache_key, bundle
        return bundle


def clear_email_repository_cache_for_tests():
    global _cached_bundle, _cached_key
    with _lock:
        if _cached_bundle:
            conn = _cached_bundle.email_research._conn
            try:
                conn.close()
            except Exception:
                pass
        _cached_bundle = _cached_key = None
