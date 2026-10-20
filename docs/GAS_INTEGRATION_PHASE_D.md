# Shared Draft API Phase D: local integration and verification

Status: local runtime composition, UI/API and GAS/API mock integration, plus isolated PostgreSQL verification. No production connection or Cloud Run deploy.

## Runtime factory

`src/outreach/shared_draft_runtime_factory.py::create_runtime_app()` builds the Phase B `SharedDraftCreationAPI` from a psycopg pool, `SharedDraftLeaseRepository`, `GoogleGmailDraftGateway`, `PooledIdentityResolver`, bearer-token verifier, Postgres rate limiter, and structured audit sink. Importing the module performs no I/O. Factory validation requires all database, API-token, Gmail DWD, and mailbox values before it creates a pool or Gmail service; Gmail OAuth initialization uses a 20-second API transport timeout by default. Any config/provider failure stops startup or returns a sanitized 503/HOLD. There is no Gmail send or scheduled-send method.

The pool applies `SET ROLE clinic_outreach_runtime` on each new connection. The role needs execute on the four protected functions, schema USAGE, SELECT on the shared ledgers, and read access to `public.clinics.uuid` under the project's existing RLS policy. The migration does not grant access to the clinic master table. Prefer a Supabase direct/session connection for the pool; transaction pooling can reset session role state.

The Gmail adapter uses a Google service account with Workspace domain-wide delegation. The administrator must authorize only `gmail.readonly` and `gmail.compose`; the API verifies the configured mailbox at initialization. The service account JSON is a Secret Manager value. The API's JSON token secret has shape:

```json
{
  "clinic-lead-test": {
    "token": "secret-manager-value",
    "roles": ["draft:create"],
    "clinic_ids": [101],
    "campaign_ids": ["campaign-test"]
  }
}
```

Use separate tokens/scopes for the GAS caller and Clinic Lead service. Never use a broad `admin` role for normal callers. The standalone GAS adapter includes injected Secret Manager token retrieval and IAM Credentials ID-token providers; its future script manifest needs the Secret Manager access and IAM Credentials scopes, and its execution identity needs accessor permission only on the API-token secret plus token-creator permission on the dedicated Cloud Run invoker service account.

## Environment contract

Required API runtime values:

| Variable | Source / purpose |
|---|---|
| `PORT` | Cloud Run container port, normally 8080 |
| `SHARED_DRAFT_DATABASE_URL` | Secret Manager; restricted runtime DB login, direct/session connection |
| `SHARED_DRAFT_API_TOKENS_JSON` | Secret Manager; caller credentials and clinic/campaign scopes |
| `GMAIL_DWD_SERVICE_ACCOUNT_JSON` | Secret Manager; Workspace DWD service account JSON |
| `SHARED_DRAFT_GMAIL_MAILBOX` | Explicit allowlisted mailbox; defaults to `info@crestix-inc.com` |
| `SHARED_DRAFT_GMAIL_TIMEOUT_SECONDS` | 1–60; defaults to 20 |
| `SHARED_DRAFT_LEASE_SECONDS` | 15–600; defaults to 90 |
| `SHARED_DRAFT_RATE_LIMIT` | Shared Postgres window limit; defaults to 30 |
| `SHARED_DRAFT_RATE_WINDOW_SECONDS` | Shared Postgres window; defaults to 60 |
| `SHARED_DRAFT_API_FACTORY` | Optional `module:function`; defaults to this runtime factory |

The Clinic Lead caller additionally uses `SHARED_DRAFT_API_ENABLED=true`, `SHARED_DRAFT_API_URL`, `SHARED_DRAFT_API_TOKEN`, and `SHARED_DRAFT_CLOUD_RUN_AUDIENCE`. The Streamlit client obtains an audience ID token from its Cloud Run service identity and sends the API secret in `X-Shared-Draft-Token`.

## Cloud Run verification service (not deployed)

Build a separate image with `Dockerfile.shared-draft-api`. Recommended isolated service controls:

* New service name and non-production project; never update an existing service/revision.
* `--no-allow-unauthenticated`; HTTPS ingress `all` only because GAS may call from outside the VPC. Cloud Run IAM remains mandatory.
* Grant `roles/run.invoker` only to the verification GAS and Clinic Lead service identities. GAS mints an audience ID token using IAM Credentials; its execution identity receives narrowly scoped `roles/iam.serviceAccountTokenCreator` on the invoker identity. The app token is a second independent check sent in `X-Shared-Draft-Token`.
* Attach the three secrets above through Secret Manager version references. Do not use `.env` in an image or log request headers.
* Set min instances 0, max instances 3, concurrency 8, request timeout 60 seconds, and 1 CPU / 512 MiB for initial isolated verification. The per-instance pool max is 8; confirm the Supabase connection budget before increasing max instances.
* Emit JSON audit events to stdout for Cloud Logging. Do not log body, recipient, API token, or OAuth secret. Alert on 401/403/429/503, HOLD/UNKNOWN, and Gmail/Supabase latency.
* Verify IAM denial and app-token denial independently before any non-production Gmail test. Confirm DWD scopes and target mailbox before enabling API requests.

Example local container run (does not run without injected test-only values):

```sh
docker build -f Dockerfile.shared-draft-api -t shared-draft-api:phase-d .
docker run --rm -p 8080:8080 \
  -e SHARED_DRAFT_DATABASE_URL="$PHASE_D_TEST_DATABASE_URL" \
  -e SHARED_DRAFT_API_TOKENS_JSON="$PHASE_D_TEST_TOKENS_JSON" \
  -e GMAIL_DWD_SERVICE_ACCOUNT_JSON="$PHASE_D_TEST_GMAIL_SECRET" \
  -e SHARED_DRAFT_GMAIL_MAILBOX="draft-test@example.invalid" \
  shared-draft-api:phase-d
```

Do not substitute production values. Unit tests use injected fakes and do not call this container.

## UI and GAS rehearsal

`src/outreach_ui.py` now uses the shared API client only when `SHARED_DRAFT_API_ENABLED=true`. All existing pre-save checks and the typed confirmation remain. Missing URL/token disables the button and does not fall back to the legacy Gmail path. The UI displays clinic name, director, UUID, recipient, article verdict/URL, formal body, Gmail state, save result, Draft/Message/Thread IDs, and HOLD reason. The direct Gmail path remains available only under its prior explicit enablement when API mode is off. Sending and scheduled sending remain absent.

`reference/gas_adapter/SharedDraftApiAdapter.gs` is still a separate, uninstalled adapter. Its mocked rehearsal checks payload, API token, optional Cloud Run IAM ID token, UNKNOWN on fetch timeout, and no internal retry. GAS migration sequence: stop old GAS Draft writer → verify no queued trigger can run it → enable only new API call → verify non-production Draft/state/Sheets preview → rollback by disabling API path before restarting old writer. Never run old/new writers concurrently; no shared lock reaches unmodified GAS.

## Local database verification

Apply the proposal only to an isolated Postgres fixture with the minimal prerequisites `public.clinics(id,uuid)`, `outreach.clinic_draft_guard`, and test roles. `tests/test_shared_draft_local_postgres.py` refuses any non-loopback host or database name other than `phase_d_shared_draft_verify_v2`. It checks concurrent same-key acquisition, distinct operation-key conflict for one clinic/campaign, schema/function permissions, direct table write denial, unique constraint, and lease-expiry fencing. It does not contain a Production DSN or operate on any configured app database.

Initial verification surfaced and fixed two DDL issues before final verification: the runtime role needed `USAGE` on `outreach`; concurrent inserts using only `ON CONFLICT(operation_key)` could deadlock against the clinic/campaign unique index. Reservation now uses untargeted `ON CONFLICT DO NOTHING` followed by a locked deterministic read and explicit operation-key/identity checks. Final isolated PostgreSQL test: **1 passed**.

## Phase D test result

The selected Phase A–D HTTP/runtime/Gmail/Sheets/GAS repository regression set completed **161 passed, 1 skipped** (the opt-in PostgreSQL test is skipped unless its exact local fixture DSN is supplied). The isolated PostgreSQL test separately completed **1 passed**. GAS Node mock completed **5 fixture calls, PASS**. Python compilation, `src.outreach_ui` import, and `git diff --check` passed. No Gmail API request or production database call was made by the test suite. Two existing app-level test modules could not be collected under the host Python 3.9 because unrelated application code uses Python 3.10 union annotation syntax; the project target is Python 3.12. No unrelated compatibility edits were made.

## Remaining production gates

The migration remains unapplied in Production. Before deployment, review table ownership/ACLs and `public.clinics.uuid` RLS, confirm DWD authorization and Secret Manager rotation owners, issue verification identities/tokens, deploy a new isolated Cloud Run service only after approval, run non-production Gmail/Sheets checks, and obtain GAS/Spreadsheet owner approval for the single-writer cutover and rollback. This worktree has not changed Production or existing services.
