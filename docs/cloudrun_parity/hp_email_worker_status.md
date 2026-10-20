# HP / Email Cloud Run execution status

## Implemented in code

- HP uses the existing Cloud Run HP Job, local HP research engine, Web controls,
  dedicated Job Control DB connection, usage budget, dispatch reconciliation,
  and progress screens. A dedicated, bounded Cloud Run control panel is gated
  by `CLINIC_CLOUD_HP_JOB_CONTROL_ENABLED=1`, IAP identity, and the exact
  `CLINIC_CLOUD_WRITE_OPERATORS` allowlist. It offers single Jobs up to the
  executor's 50-clinic ceiling, checks budget before creating a Job, and will
  not resume if dispatch state cannot be read or remains ambiguous. It has no
  local-runner fallback, Auto Run, or reset operation. Job control fails
  closed when `SUPABASE_JOB_CONTROL_DB_URL` is absent. Existing general HP
  Human Review remains unavailable in Cloud Run because it still uses the
  general Clinic writer; use the approved local workflow until a dedicated
  least-privilege HP review writer is implemented.
- Email keeps the existing Email Research queue, candidate classifier,
  `AUTO_TRUE` / `AUTO_FALSE` / `REVIEW` decisions, Primary Email selector,
  Human Review and progress repository. In `EMAIL_AUTO_EXECUTOR=cloud_run`
  mode, the Web process creates/syncs the durable run and dispatches a Job;
  the worker recovers that run and processes a bounded cohort. Each execution
  claims at most 25 clinics by default (configurable to 1–50). The run is
  paused for a later explicit resume at the limit. It does not continually
  refresh the cohort from the Worker.
- Email Run controls are available only on the Cloud Run Web deployment when
  `EMAIL_AUTO_RUN_ENABLED=true`, the executor is explicitly set to
  `EMAIL_AUTO_EXECUTOR=cloud_run`, and the IAP operator appears in
  `CLINIC_CLOUD_WRITE_OPERATORS`. Email Human Review uses the existing
  dedicated Email Research repository and is separately gated by
  `CLINIC_CLOUD_EMAIL_HUMAN_REVIEW_ENABLED=1` plus the same operator check.
  Candidate decisions can confirm/reject candidates and set the Primary Email
  through that repository. On a non-Cloud-Run process, a Cloud Run executor
  setting disables the controls instead of falling back to the local runner.
- Duplicate executions are guarded by the existing durable controller lease;
  Job task retries and parallel tasks are rejected. An ambiguous Jobs API
  response is recorded as `UNKNOWN` in the existing run metrics JSON under a
  row lock; the UI blocks every further dispatch for that run until an operator
  reconciles the Cloud Run Execution. No schema migration is required. A
  confirmed dispatch is also recorded with its Execution name, and an active
  confirmed dispatch cannot be started a second time.

## Deployment configuration still required

This PR does not perform any of these operations:

1. Build/publish the Web and Worker images and create the Cloud Run HP/Email
   Jobs. Email uses `Dockerfile.cloudrun.email-worker`; configure tasks=1,
   parallelism=1, max retries=0, and a finite task timeout.
2. Keep the READ ONLY Web service unchanged. To expose these controls, create
   or explicitly approve a separate authenticated Cloud Run Web configuration
   with `CLINIC_READ_ONLY_UI=false`; `is_cloud_run_deployment()` still blocks
   all legacy write surfaces there. Dedicated controls require
   `CLINIC_CLOUD_WRITE_OPERATORS`,
   `CLINIC_CLOUD_HP_JOB_CONTROL_ENABLED=1` for HP, and
   `CLINIC_CLOUD_EMAIL_HUMAN_REVIEW_ENABLED=1` for Email Human Review. HP also
   requires the existing HP executor variables and dedicated
   `SUPABASE_JOB_CONTROL_DB_URL`. Keep the Web Reader connection separate.
3. Set `EMAIL_AUTO_RUN_ENABLED=true`, `EMAIL_AUTO_EXECUTOR=cloud_run`,
   `CLOUD_RUN_PROJECT`, `CLOUD_RUN_REGION`, `CLOUD_RUN_EMAIL_WORKER_JOB_NAME`,
   and a reviewed `EMAIL_CLOUD_RUN_MAX_CLINICS_PER_EXECUTION` (1–50).
4. Provision the already-designed Email Research DB writer and Clinic/HP target
   reader connections with least privilege, verify schema is already applied,
   and grant the Web service identity permission to execute only the named Job.
   IAM, Secret and database changes require separate approval.
   Configure IAP so the authenticated email header is forwarded; the app
   allowlist is an additional business authorization check, not a replacement
   for IAP. Keep each feature flag off until its DB role and operator list are
   reviewed.
5. Do not start real HP or Email jobs until a reviewer approves the runtime
   configuration, operator access, API cost limits, cohort size and recovery
   procedure. Gmail sending/draft creation is unrelated and remains disabled.

## Mock verification

`tests/test_cloud_email_worker.py` verifies bounded configuration, request
overrides, ambiguous dispatch handling, retry/task rejection and duplicate
standby behavior. `tests/test_email_auto_run.py` verifies the execution claim
ceiling and that local mode remains unbounded when the optional ceiling is not
provided. `tests/test_cloud_hp_email_ui_safety.py` pins the Cloud-only feature
flags, operator checks, bounded HP controls, dispatch-state fail-closed resume,
and no local Email fallback. No Cloud Run API, production database or real
research job is used. CI builds both worker images, starts the Web image and
checks its health endpoint, and verifies the Email Worker exits safely when
the required run ID is absent.
