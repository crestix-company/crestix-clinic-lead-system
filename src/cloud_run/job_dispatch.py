"""Google Cloud Run Jobs API dispatch boundary for the HP research worker.

This module only ever starts an Execution of the *existing* clinic-lead-hp-worker
Cloud Run Job (``jobs().run``) with exactly one override: the already-created HP
research job id (``scripts/cloud_hp_worker.py`` resolves what to do with it). It
never receives or forwards credentials, connection URLs, or any other secret --
the Worker container gets those from its own Secret Manager-mounted environment,
independent of whatever triggered the Execution.

The HTTP call to the Jobs API can fail ambiguously (timeout, 5xx, dropped
connection) without telling the caller whether the Execution actually started.
Treating that as "it failed, try again" risks a second concurrent Execution on
the same job id, so this module classifies every outcome into exactly one of:

- DISPATCHED: the API call succeeded; an Execution name is available.
- DISPATCH_UNKNOWN: the outcome could not be confirmed (timeout/network/5xx).
  The caller must reconcile against the Cloud Run Executions API or the Job's
  own DB state before dispatching again -- never re-dispatch on an UNKNOWN.
- REJECTED: the API itself rejected the request (4xx); safe to surface to the
  user as a clean failure, since nothing was started.

No GCP resource is created or deployed by this module; it only calls `run` on a
Job that a human has already deployed separately.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

DISPATCHED = "DISPATCHED"
DISPATCH_UNKNOWN = "DISPATCH_UNKNOWN"
REJECTED = "REJECTED"


class InvalidJobIdError(ValueError):
    pass


@dataclass(frozen=True)
class DispatchResult:
    status: str
    execution_name: str | None = None
    detail: str = ""


def _validate_job_id(clinic_job_id):
    value = str(clinic_job_id or "").strip()
    if not JOB_ID_PATTERN.fullmatch(value):
        raise InvalidJobIdError("clinic_job_idは既存Job ID（32桁16進数）である必要があります。")
    return value


def build_job_run_request(job_resource_name, clinic_job_id):
    """Build the Jobs API `run` request body.

    The only override ever included is HP_WORKER_JOB_ID. No other environment
    variable, secret, or credential may be added here -- this is the one place
    that enforces "オーバーライドで渡すのはジョブIDだけ".
    """
    job_id = _validate_job_id(clinic_job_id)
    return {
        "name": job_resource_name,
        "body": {
            "overrides": {
                "containerOverrides": [
                    {"env": [{"name": "HP_WORKER_JOB_ID", "value": job_id}]}
                ],
                "taskCount": 1,
            }
        },
    }


def _classify_error(exc):
    """Fail-safe classification: anything not clearly a clean 4xx is UNKNOWN."""
    status_code = getattr(exc, "status_code", None)
    resp = getattr(exc, "resp", None)
    if status_code is None and resp is not None:
        status_code = getattr(resp, "status", None)
    if isinstance(status_code, int) and 400 <= status_code < 500:
        return REJECTED
    return DISPATCH_UNKNOWN


def run_hp_worker_job(*, execute_fn, job_resource_name, clinic_job_id):
    """Start exactly one Execution of the HP worker Job for `clinic_job_id`.

    `execute_fn` performs the actual authenticated API call (injected so this
    stays testable without real Google credentials/network); it must accept the
    request dict from build_job_run_request() and return the API response dict,
    or raise on failure.
    """
    request = build_job_run_request(job_resource_name, clinic_job_id)
    try:
        response = execute_fn(request)
    except Exception as exc:
        return DispatchResult(status=_classify_error(exc), detail=str(exc)[:200])
    execution_name = ""
    if isinstance(response, dict):
        execution_name = str(response.get("metadata", {}).get("name") or response.get("name") or "")
    if not execution_name:
        # The call returned without raising but gave nothing to reconcile against --
        # that is exactly the ambiguous case this module exists to catch.
        return DispatchResult(status=DISPATCH_UNKNOWN, detail="Execution名を確認できません。")
    return DispatchResult(status=DISPATCHED, execution_name=execution_name)
