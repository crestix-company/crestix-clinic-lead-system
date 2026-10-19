"""Application-level monthly usage caps for the Cloud Run HP worker.

Cloud Billing budget alerts do not guarantee a hard stop
(docs/cloud_run_on_demand_hp_implementation_plan.md §5: "予算通知は0円停止を
保証しない"). This module is the app-side backstop: it tracks dispatched
Executions and clinics-requested per month in the existing
``app_config.settings`` table (no new schema object, same key-value pattern as
``src.repository.hp_auto_run_repository``) and refuses to dispatch once either
cap is reached -- independent of, and in addition to, the per-Execution limits
enforced elsewhere (1 concurrent Execution via the advisory lock in
scripts/cloud_hp_worker.py, <=50 clinics via
src.cloud_run.hp_executor.CLOUD_RUN_MAX_SINGLE_JOB_COUNT, 30 min task_timeout
set at Cloud Run Job deploy time by a human).
"""
from __future__ import annotations

import json
import os
from datetime import date

KEY_PREFIX = "hp_cloud_usage:"

MAX_MONTHLY_EXECUTIONS_ENV_VAR = "CLOUD_RUN_MAX_MONTHLY_EXECUTIONS"
MAX_MONTHLY_CLINICS_ENV_VAR = "CLOUD_RUN_MAX_MONTHLY_CLINICS"
DEFAULT_MAX_MONTHLY_EXECUTIONS = 20
DEFAULT_MAX_MONTHLY_CLINICS = 500


def _month_key(today=None):
    today = today or date.today()
    return KEY_PREFIX + today.strftime("%Y-%m")


def _as_int(raw, default):
    raw = (raw or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def monthly_limits(env=None):
    env = os.environ if env is None else env
    return (
        _as_int(env.get(MAX_MONTHLY_EXECUTIONS_ENV_VAR), DEFAULT_MAX_MONTHLY_EXECUTIONS),
        _as_int(env.get(MAX_MONTHLY_CLINICS_ENV_VAR), DEFAULT_MAX_MONTHLY_CLINICS),
    )


def read_usage(conn, today=None):
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM app_config.settings WHERE key=%s", (_month_key(today),))
        row = cur.fetchone()
    conn.commit()
    if not row:
        return {"executions": 0, "clinics": 0}
    value = row[0]
    value = value if isinstance(value, dict) else json.loads(value)
    return {"executions": int(value.get("executions", 0)), "clinics": int(value.get("clinics", 0))}


def check_budget(conn, requested_clinic_count, *, env=None, today=None):
    """Returns (allowed, reason). Pure read -- never mutates usage. Callers
    must call record_dispatch_usage() separately, and only after a dispatch
    is actually confirmed DISPATCHED (never for DISPATCH_UNKNOWN/REJECTED)."""
    max_executions, max_clinics = monthly_limits(env)
    usage = read_usage(conn, today)
    if usage["executions"] >= max_executions:
        return False, f"今月のCloud Run実行回数が上限（{max_executions}回）に達しています。来月まで待つか、上限設定を人間が見直してください。"
    if usage["clinics"] + int(requested_clinic_count) > max_clinics:
        return False, (
            f"今月の累積調査件数が上限（{max_clinics}件）を超えます"
            f"（現在{usage['clinics']}件 + 今回{requested_clinic_count}件）。"
        )
    return True, ""


def record_dispatch_usage(conn, clinic_count, *, today=None):
    """Call exactly once per confirmed DISPATCHED outcome."""
    key = _month_key(today)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app_config.settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO NOTHING",
            (key, json.dumps({"executions": 0, "clinics": 0})),
        )
        cur.execute("SELECT value FROM app_config.settings WHERE key=%s FOR UPDATE", (key,))
        value = cur.fetchone()[0]
        value = value if isinstance(value, dict) else json.loads(value)
        value["executions"] = int(value.get("executions", 0)) + 1
        value["clinics"] = int(value.get("clinics", 0)) + int(clinic_count)
        cur.execute(
            "UPDATE app_config.settings SET value=%s WHERE key=%s",
            (json.dumps(value, ensure_ascii=False), key),
        )
    conn.commit()
