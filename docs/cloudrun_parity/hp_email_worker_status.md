# HP / Email Cloud Run execution status

## Implemented in code

- HP uses the existing Cloud Run HP Job, local HP research engine, Web controls,
  dedicated Job Control DB connection, usage budget, dispatch reconciliation,
  and review screens. The Cloud Web UI remains unreachable while its READ ONLY
  gate is enabled. Job control fails closed when
  `SUPABASE_JOB_CONTROL_DB_URL` is absent.
- Email keeps the existing Email Research queue, candidate classifier,
  `AUTO_TRUE` / `AUTO_FALSE` / `REVIEW` decisions, Primary Email selector,
  Human Review and progress repository. In `EMAIL_AUTO_EXECUTOR=cloud_run`
  mode, the Web process creates/syncs the durable run and dispatches a Job;
  the worker recovers that run and processes a bounded cohort. Each execution
  claims at most 25 clinics by default (configurable to 1–50). The run is
  paused for a later explicit resume at the limit. It does not continually
  refresh the cohort from the Worker.
- Duplicate executions are guarded by the existing durable controller lease;
  Job task retries and parallel tasks are rejected. An ambiguous Jobs API
  response is not retried automatically.

## Deployment configuration still required

This PR does not perform any of these operations:

1. Build/publish the Web and Worker images and create the Cloud Run HP/Email
   Jobs. Email uses `Dockerfile.cloudrun.email-worker`; configure tasks=1,
   parallelism=1, max retries=0, and a finite task timeout.
2. Set `CLINIC_READ_ONLY_UI=false` only on an approved, authenticated operator
   deployment, plus the existing HP executor variables and a dedicated
   `SUPABASE_JOB_CONTROL_DB_URL`. Keep the Web Reader connection separate.
3. Set `EMAIL_AUTO_RUN_ENABLED=true`, `EMAIL_AUTO_EXECUTOR=cloud_run`,
   `CLOUD_RUN_PROJECT`, `CLOUD_RUN_REGION`, `CLOUD_RUN_EMAIL_WORKER_JOB_NAME`,
   and a reviewed `EMAIL_CLOUD_RUN_MAX_CLINICS_PER_EXECUTION` (1–50).
4. Provision the already-designed Email Research DB writer and Clinic/HP target
   reader connections with least privilege, verify schema is already applied,
   and grant the Web service identity permission to execute only the named Job.
   IAM, Secret and database changes require separate approval.
5. Do not start real HP or Email jobs until a reviewer approves the runtime
   configuration, operator access, API cost limits, cohort size and recovery
   procedure. Gmail sending/draft creation is unrelated and remains disabled.

## Mock verification

`tests/test_cloud_email_worker.py` verifies bounded configuration, request
overrides, ambiguous dispatch handling, retry/task rejection and duplicate
standby behavior. `tests/test_email_auto_run.py` verifies the execution claim
ceiling and that local mode remains unbounded when the optional ceiling is not
provided. No Cloud Run API, production database or real research job is used.
