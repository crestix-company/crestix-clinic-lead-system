"""Run one bounded Email Research Auto Run as a Cloud Run Job task."""
from __future__ import annotations

import logging
import os
import sys

from src.cloud_run.email_executor import build_worker_runner

log = logging.getLogger("cloud_email_worker")


def run(env=None, runner_factory=None):
    env = os.environ if env is None else env
    run_id = (env.get("EMAIL_WORKER_RUN_ID") or "").strip()
    if not run_id:
        raise RuntimeError("EMAIL_WORKER_RUN_ID is required")
    task_index = (env.get("CLOUD_RUN_TASK_INDEX") or "0").strip()
    task_attempt = (env.get("CLOUD_RUN_TASK_ATTEMPT") or "0").strip()
    if task_index != "0" or task_attempt != "0":
        raise RuntimeError("Email Worker requires tasks=1 and max_retries=0")
    runner = (runner_factory or build_worker_runner)(env=env)
    started = runner.recover(run_id)
    if not started:
        # A live controller already owns this durable run. Its lease is the
        # cross-Execution lock; a duplicate task exits without claiming work.
        log.info("email_worker_standby run_id=%s", run_id)
        return 0
    thread = getattr(runner, "_thread", None)
    if thread is not None:
        thread.join()
    return 0


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        return run()
    except Exception as exc:
        # Avoid emitting exception strings that could contain DB endpoints/secrets.
        log.error("email_worker_failed exception_type=%s", type(exc).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
