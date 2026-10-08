"""Pure state transitions shared by the persistent Auto HP repository and tests."""
from __future__ import annotations


def initial_auto_state(*, run_id, prefecture, medical_types, force, batch_size,
                       initial_count, current_job_id, timestamp):
    return {
        "version": 1,
        "run_id": run_id,
        "status": "RUNNING",
        "prefecture": str(prefecture or ""),
        "medical_types": list(medical_types or []),
        "force": bool(force),
        "batch_size": min(500, max(1, int(batch_size))),
        "initial_count": int(initial_count),
        "current_job_id": current_job_id,
        "current_batch": 1,
        "completed_batches": 0,
        "started_at": timestamp,
        "last_progress_at": timestamp,
        "last_error": "",
        "requested_pause": False,
        "controller_id": "",
        "controller_lease_until": "",
        "network_retry_count": 0,
    }


def advance_completed_batch(state, *, remaining, next_job_id, timestamp):
    """Return a copy after one completed child; next_job_id must exist iff remaining > 0."""
    output = dict(state)
    output["completed_batches"] = int(output.get("completed_batches", 0)) + 1
    output["last_progress_at"] = timestamp
    if int(remaining) <= 0:
        output.update({"status": "COMPLETED", "controller_id": "", "controller_lease_until": ""})
        return output
    if not next_job_id:
        raise ValueError("remaining > 0 requires the next child job")
    output.update({
        "current_job_id": next_job_id,
        "current_batch": int(output.get("current_batch", 1)) + 1,
        "last_error": "",
    })
    return output
