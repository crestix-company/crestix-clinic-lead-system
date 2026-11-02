"""Secret-free classification for expected Postgres connection failures."""
from __future__ import annotations


def connection_error_category(exc: BaseException) -> str:
    """Classify a database failure without returning its driver message."""
    chain = []
    states = set()
    current = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(str(current).casefold())
        state = getattr(current, "sqlstate", None)
        if state:
            states.add(str(state).upper())
        current = current.__cause__ or current.__context__
    message = " ".join(chain)

    if any(state.startswith("28") for state in states) or any(
        item in message for item in (
            "password authentication failed", "authentication failed", "invalid password",
            "no password supplied", "role does not exist",
        )
    ):
        return "authentication"
    if any(item in message for item in (
        "supabase_runtime_db_url is not set", "supabase_runtime_db_url is missing",
        "clinic runtime configuration is missing",
    )):
        return "configuration"
    if "57014" in states or "statement timeout" in message:
        return "query_timeout"
    if isinstance(exc, TimeoutError) or any(
        item in message for item in ("timeout expired", "timed out", "connection timeout")
    ):
        return "timeout"
    if any(state.startswith("08") for state in states) or any(
        item in message for item in (
            "connection is closed", "connection already closed", "server closed the connection",
            "connection reset", "connection lost", "broken pipe", "connection terminated",
            "unexpectedly closed", "ssl syscall error: eof",
        )
    ):
        return "disconnected"
    try:
        import psycopg
    except ImportError:
        return "unavailable"
    if isinstance(exc, psycopg.InterfaceError):
        return "disconnected"
    if any(item in message for item in (
        "could not translate host name", "name or service not known", "temporary failure in name resolution",
    )):
        return "network"
    if "connection refused" in message:
        return "unavailable"
    return "unavailable"


def retryable_read_connection_error(exc: BaseException) -> bool:
    """Allow one replay only for a read whose connection/transport likely failed."""
    try:
        import psycopg
    except ImportError:
        return False
    if not isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError, TimeoutError)):
        return False
    return connection_error_category(exc) in {"disconnected", "timeout", "network", "unavailable"}


def is_database_connection_exception(exc: BaseException) -> bool:
    if connection_error_category(exc) == "configuration":
        return True
    try:
        import psycopg
    except ImportError:
        return isinstance(exc, TimeoutError)
    return isinstance(exc, (psycopg.Error, TimeoutError))


def safe_connection_message(exc: BaseException, *, service="Clinic/HP") -> str:
    """A UI-ready explanation containing no URL, host, username, or driver text."""
    category = connection_error_category(exc)
    messages = {
        "authentication": f"{service} DB認証に失敗しました。正式ランチャーで読み込む接続資格情報を確認してください。",
        "configuration": f"{service} DB接続設定がありません。正式ランチャーから起動し、接続設定を確認してください。",
        "query_timeout": f"{service} DBの読取がタイムアウトしました。時間をおいて再試行してください。",
        "timeout": f"{service} DB接続がタイムアウトしました。ネットワーク状態を確認してください。",
        "disconnected": f"{service} DB接続が切断されました。読取は最大1回だけ再接続して再試行します。",
        "network": f"{service} DBホストへ接続できません。ネットワーク設定を確認してください。",
        "unavailable": f"{service} DBに接続できません。接続状態を確認してください。",
    }
    return messages[category]
