"""Streamlit Runtimeを空きポートで単一起動する。"""
from pathlib import Path
import argparse
import hashlib
import os
import shlex
import socket
import subprocess
import sys
import tempfile

from filelock import FileLock, Timeout


RUNTIME_ENV_FILE = ".supabase-runtime.env.local"
PRODUCTION_ENV_FILE = "config/production_runtime.env"
EMAIL_ENV_FILE = "config/email_research.env.local"
MAC_EMAIL_ENV_FILE = Path.home() / ".config" / "clinic-lead" / "email_research.env"
RUNTIME_ENV_KEYS = {
    "SUPABASE_RUNTIME_DB_URL",
    "CLINIC_WRITE_BACKEND",
    "CLINIC_DATA_BACKEND",
    "CLINIC_COMMON_IDENTITY_ENABLED",
}
EMAIL_ENV_KEYS = {
    "EMAIL_RESEARCH_DB_URL",
    "EMAIL_TARGET_READER_DB_URL",
    "CLINIC_READONLY_DB_URL",
    # Optional, stricter dedicated-role path for src.outreach.email_readonly_access.
    # Not provisioned in Supabase today -- see config/email_research.env.example.
    # Until a role exists, GAS/outreach Email Research reads use EMAIL_RESEARCH_DB_URL
    # via src.outreach.gas_email_lookup, the same connection already proven in
    # scripts/outreach_mail_canary.py.
    "EMAIL_READONLY_DB_URL",
    "EMAIL_AUTO_RUN_ENABLED",
    "SERPAPI_API_KEY",
    "EMAIL_SEARCH_PROVIDER",
    "EMAIL_RESEARCH_DB_MODE",
    "EMAIL_RESEARCH_COHORT_FILE",
    "EMAIL_RESEARCH_WORKERS",
    "EMAIL_RESEARCH_CONNECT_TIMEOUT_SECONDS",
    "EMAIL_RESEARCH_READ_TIMEOUT_SECONDS",
    "EMAIL_RESEARCH_RETRY_COUNT",
    "EMAIL_RESEARCH_SEARCH_QUERY_LIMIT",
    "EMAIL_RESEARCH_CLAIM_LEASE_SECONDS",
    "EMAIL_RESEARCH_IDLE_POLL_SECONDS",
    "EMAIL_RESEARCH_TARGET_SYNC_INTERVAL_SECONDS",
    "EMAIL_RESEARCH_PRIMARY_MINIMUM_CONFIDENCE",
    "OUTREACH_GMAIL_DRAFTS_ENABLED",
    "OUTREACH_GMAIL_EXPECTED_ACCOUNT",
    "OUTREACH_GMAIL_OAUTH_CLIENT_JSON",
    "OUTREACH_GMAIL_OAUTH_TOKEN_JSON",
}
PREVIEW_ONLY_ENV_VAR = "CLINIC_COMDESK_PREVIEW_ONLY"
RUNTIME_ENV_PATH_VAR = "CLINIC_RUNTIME_ENV_FILE"

BOOLEAN_SETTING_VALUES = {
    "1": "1",
    "true": "true",
    "yes": "yes",
    "on": "on",
    "0": "0",
    "false": "false",
    "no": "no",
    "off": "off",
}


def _validated_boolean_setting(name, value):
    normalized = str(value).strip().lower()
    if normalized not in BOOLEAN_SETTING_VALUES:
        raise RuntimeError(f"{name}は0/1/true/false/yes/no/on/offで指定してください。")
    return BOOLEAN_SETTING_VALUES[normalized]


def _parse_env_file(path, *, require_private=False, allowed_keys=None):
    if not path.exists():
        return {}
    if require_private and os.name != "nt" and path.stat().st_mode & 0o077:
        raise RuntimeError(f"{path.name} の権限を600にしてください。")
    parsed = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = shlex.split(line, posix=True)
        if parts and parts[0] == "export":
            parts = parts[1:]
        if len(parts) != 1 or "=" not in parts[0]:
            raise RuntimeError(f"{path}:{number} の形式を確認してください。")
        key, value = parts[0].split("=", 1)
        if key not in (RUNTIME_ENV_KEYS if allowed_keys is None else allowed_keys):
            raise RuntimeError(f"{path}:{number} に許可されていない設定があります。")
        parsed[key] = value
    return parsed


def load_runtime_env(root):
    """Load tracked production routing, then the ignored credential, without printing values."""
    preview_setting = (os.environ.get(PREVIEW_ONLY_ENV_VAR) or "").strip()
    if preview_setting not in {"", "0", "1"}:
        raise RuntimeError(f"{PREVIEW_ONLY_ENV_VAR}は1または0で指定してください。")
    preview_only = preview_setting == "1"
    normal_mac = os.environ.get("CLINIC_NORMAL_MAC_RUNTIME") == "1"
    read_only_ui = os.environ.get("CLINIC_READ_ONLY_UI", "").strip().casefold() in {"1", "true", "yes", "on"}
    parsed = _parse_env_file(root / PRODUCTION_ENV_FILE)
    # Preview and dedicated READ ONLY launches must not even load a Writer secret.
    runtime_file = Path(os.environ.get(RUNTIME_ENV_PATH_VAR) or (root / RUNTIME_ENV_FILE))
    if not (preview_only or read_only_ui):
        parsed.update(_parse_env_file(runtime_file, require_private=True))
    else:
        parsed.pop("SUPABASE_RUNTIME_DB_URL", None)
    # Normal Mac may use existing dedicated Email repositories from the private
    # Runtime config. Gmail OAuth settings are excluded, and the switches below
    # keep Auto Run and Gmail drafts disabled. Preview/Reader-only never load it.
    if preview_only or read_only_ui:
        email_db_config = {}
    elif normal_mac:
        email_db_config = _parse_env_file(
            MAC_EMAIL_ENV_FILE, require_private=True, allowed_keys=EMAIL_ENV_KEYS
        )
        for key in (
            "OUTREACH_GMAIL_OAUTH_CLIENT_JSON", "OUTREACH_GMAIL_OAUTH_TOKEN_JSON",
            "OUTREACH_GMAIL_EXPECTED_ACCOUNT", "OUTREACH_GMAIL_DRAFTS_ENABLED",
        ):
            email_db_config.pop(key, None)
    else:
        email_db_config = _parse_env_file(
            root / EMAIL_ENV_FILE, require_private=True, allowed_keys=EMAIL_ENV_KEYS
        )
    if "EMAIL_AUTO_RUN_ENABLED" in email_db_config:
        auto_run_value = email_db_config["EMAIL_AUTO_RUN_ENABLED"].strip().lower()
        if auto_run_value not in {"true", "false"}:
            raise RuntimeError("EMAIL_AUTO_RUN_ENABLEDはtrueまたはfalseで指定してください。")
        email_db_config["EMAIL_AUTO_RUN_ENABLED"] = auto_run_value
    if "CLINIC_COMMON_IDENTITY_ENABLED" in parsed:
        parsed["CLINIC_COMMON_IDENTITY_ENABLED"] = _validated_boolean_setting(
            "CLINIC_COMMON_IDENTITY_ENABLED", parsed["CLINIC_COMMON_IDENTITY_ENABLED"]
        )
    ambient_identity = (os.environ.get("CLINIC_COMMON_IDENTITY_ENABLED") or "").strip()
    if ambient_identity:
        os.environ["CLINIC_COMMON_IDENTITY_ENABLED"] = _validated_boolean_setting(
            "CLINIC_COMMON_IDENTITY_ENABLED", ambient_identity
        )
    parsed.update(email_db_config)
    selected_write = (os.environ.get("CLINIC_WRITE_BACKEND") or parsed.get("CLINIC_WRITE_BACKEND", "sqlite")).strip()
    runtime_url = (os.environ.get("SUPABASE_RUNTIME_DB_URL") or parsed.get("SUPABASE_RUNTIME_DB_URL", "")).strip()
    if not (preview_only or read_only_ui) and selected_write.strip().lower() == "supabase" and not runtime_url:
        raise RuntimeError("Supabase WRITEにはSUPABASE_RUNTIME_DB_URLが必要です。admin URLへのfallbackは行いません。")
    for key, value in parsed.items():
        if key == "EMAIL_AUTO_RUN_ENABLED":
            # The private local config is the explicit fail-closed switch.
            os.environ[key] = value
        elif not (os.environ.get(key) or "").strip():
            # An explicitly exported but empty variable must not shadow the private config.
            os.environ[key] = value
    if preview_only or read_only_ui:
        if (os.environ.get("CLINIC_DATA_BACKEND", "").strip().casefold() != "supabase"
                or not os.environ.get("SUPABASE_READONLY_DB_URL", "").strip()):
            raise RuntimeError("Preview/READ ONLY起動にはSupabaseの専用Reader接続設定が必要です。")
        os.environ.pop("SUPABASE_RUNTIME_DB_URL", None)
        # Defense in depth: existing read-only connection setup is also enabled, while
        # the dedicated role and server-side guards remain the actual write boundary.
        os.environ["CLINIC_READ_ONLY_UI"] = "1"
        os.environ["EMAIL_AUTO_RUN_ENABLED"] = "false"
        os.environ["OUTREACH_GMAIL_DRAFTS_ENABLED"] = "false"
    elif normal_mac:
        os.environ["EMAIL_AUTO_RUN_ENABLED"] = "false"
        os.environ["OUTREACH_GMAIL_DRAFTS_ENABLED"] = "false"
    return bool(parsed)


def choose_port(preferred=8501):
    for port in range(preferred,preferred+20):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1",port))
                return port
            except OSError:
                continue
    raise RuntimeError("利用できるポートがありません。古いアプリのターミナルを終了してください。")


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--headless",action="store_true")
    parser.add_argument("--port",type=int,default=8501)
    parser.add_argument("--strict-port",action="store_true")
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    load_runtime_env(root)
    lock_name = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]
    lock = FileLock(str(Path(tempfile.gettempdir()) / f"clinic-lead-runtime-{lock_name}.lock"))
    try:
        with lock.acquire(timeout=0):
            port=choose_port(args.port)
            if args.strict_port and port != args.port:
                raise RuntimeError(
                    f"正式ポート{args.port}は使用中です。別ポートでは起動しません。")
            print(f"クリニック営業マスター： http://127.0.0.1:{port}/",flush=True)
            command=[sys.executable,"-m","streamlit","run",str(root/"app_v2.py"),"--server.address","127.0.0.1",
                     "--server.port",str(port),"--browser.serverAddress","127.0.0.1","--browser.serverPort",str(port),
                     "--browser.gatherUsageStats","false","--global.developmentMode","false",
                     # Never block a one-click start on Streamlit's first-run "Email:" console prompt.
                     "--server.showEmailPrompt","false"]
            if args.headless:
                command += ["--server.headless","true"]
            return subprocess.call(command,cwd=root)
    except Timeout:
        raise RuntimeError(
            "Clinic Lead Systemは既にこのRuntimeから起動中です。二重起動は行いません。") from None


if __name__=="__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        print(str(exc))
        sys.exit(1)
