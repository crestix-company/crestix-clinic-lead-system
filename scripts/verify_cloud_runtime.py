"""Cloud Run Web (clinic-lead-web) 起動前/起動後の設定・READ ONLY検証。

秘密値（接続URL・パスワード等）は一切標準出力・ログへ出さない。各チェックは
「設定されているか／期待値か」というbool判定のみを返す。DB接続チェックは任意
（--skip-db-check で省略可能、URL未設定時は自動的にスキップ）で、実行しても
SELECT文とセッション設定の確認（SHOW default_transaction_read_only等）のみ行い、
一切のINSERT/UPDATE/DELETE/DDLを実行しない。

想定される起動時エラーの原因（IAP未設定、Secret Manager未マウント等）はこの
スクリプトの責務外。Cloud Run/IAP/Secret Managerの実デプロイ・作成はここでは
一切行わない。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


REQUIRED_TRUE_ENV_VARS = ("CLINIC_READ_ONLY_UI",)
REQUIRED_FALSE_ENV_VARS = ("OUTREACH_GMAIL_DRAFTS_ENABLED", "EMAIL_AUTO_RUN_ENABLED")
REQUIRED_VALUE_ENV_VARS = {
    "CLINIC_DATA_BACKEND": "supabase",
    "CLINIC_WRITE_BACKEND": "supabase",
}
REQUIRED_PRESENT_ENV_VARS = ("SUPABASE_RUNTIME_DB_URL", "PORT")


def _truthy(value):
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _falsy(value):
    raw = str(value or "").strip().casefold()
    return raw == "" or raw in {"0", "false", "no", "off"}


def check_environment(env=None):
    """Return {check_name: bool} without ever including a secret value."""
    env = os.environ if env is None else env
    results = {}
    for key in REQUIRED_TRUE_ENV_VARS:
        results[f"{key}=true"] = _truthy(env.get(key))
    for key in REQUIRED_FALSE_ENV_VARS:
        results[f"{key}=false_or_unset"] = _falsy(env.get(key))
    for key, expected in REQUIRED_VALUE_ENV_VARS.items():
        results[f"{key}=={expected}"] = (env.get(key) or "").strip().casefold() == expected
    for key in REQUIRED_PRESENT_ENV_VARS:
        results[f"{key}_is_set"] = bool((env.get(key) or "").strip())
    return results


def check_read_only_db_session(url=None, *, connect=None):
    """Open one connection and confirm the server-enforced read-only guard is active.

    Only ever issues SELECT/SHOW statements. Returns a bool report dict; never raises
    on expected "not configured" conditions (url missing), so callers can treat a
    skipped check as distinct from a failed one.
    """
    if not url:
        return {"skipped": True, "reason": "SUPABASE_RUNTIME_DB_URL is not set"}
    if connect is None:
        from src.repository.supabase_adapter import connect as connect
    conn = connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW default_transaction_read_only")
            read_only_guard = str(cur.fetchone()[0]).strip().lower() == "on"
            cur.execute("SELECT 1")
            select_ok = cur.fetchone()[0] == 1
        return {"skipped": False, "read_only_guard_on": read_only_guard, "select_ok": select_ok}
    finally:
        conn.close()


def run(env=None, *, skip_db_check=False, connect=None):
    env = os.environ if env is None else env
    report = {"environment": check_environment(env)}
    if skip_db_check:
        report["database"] = {"skipped": True, "reason": "--skip-db-check"}
    else:
        report["database"] = check_read_only_db_session(
            (env.get("SUPABASE_RUNTIME_DB_URL") or "").strip(), connect=connect
        )
    environment_ok = all(report["environment"].values())
    database = report["database"]
    database_ok = database.get("skipped") or (
        database.get("read_only_guard_on") is True and database.get("select_ok") is True
    )
    report["ok"] = bool(environment_ok and database_ok)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-db-check", action="store_true")
    args = parser.parse_args(argv)
    report = run(skip_db_check=args.skip_db_check)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
