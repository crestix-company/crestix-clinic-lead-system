# Stage5 External Acceptance

Date: 2026-10-07 (Asia/Tokyo)

## Checkpoint

- Stage5 implementation checkpoint: `93e54aa`
- Checkpoint message: `refactor: complete Supabase-only runtime implementation`
- The checkpoint records implementation completion only. It does not claim Stage5 PASS.

## Safety contract

The helper uses the existing `monthly_limit` setting as a short-lived numeric marker. It first
captures the exact canonical JSON text and all 19 table counts, writes only through the
production Supabase Repository using `clinic_runtime`, and restores the original value
byte-for-byte. It never modifies a clinic, creates a table, prints a credential, uses an admin
URL, or opens a persistent SQLite database.

If a multi-PC run is interrupted, run cleanup on PC-A with the same token before starting a new
test. Cleanup refuses to act unless the live value is one of the exact markers derived from that
token.

## Windows actual-machine procedure

Prerequisites on the Windows PC:

- code containing checkpoint `93e54aa` and the acceptance helper;
- `.venv` already installed;
- ignored `.supabase-runtime.env.local` securely provisioned with the runtime URL;
- no clinic/Treatment/HP SQLite files are required.

From Windows PowerShell 5.1 at the repository root:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\supabase_migration\stage5_windows_acceptance.ps1
```

Required safe output:

```text
RUNTIME_ENV=SET
RUNTIME_ROLE=clinic_runtime
READ=PASS
WRITE=PASS
WRITE_CLEANUP=PASS
UI_SMOKE=PASS
SQLITE_REQUIRED=NO
SQLITE_OPEN=0
SECRET_OUTPUT=0
UNEXPECTED_MUTATION=0
WINDOWS_ACCEPTANCE=PASS
WINDOWS_POWERSHELL_5_1=PASS
WINDOWS_ACTUAL_MACHINE_ACCEPTANCE=PASS
```

Current result: **PENDING — no Windows machine is available to this Codex session.**

The cross-platform Python probe beneath the PowerShell wrapper was run on the current Mac with
the production runtime configuration. Runtime identity, READ, reversible WRITE, exact cleanup,
UI smoke, invalid/unset SQLite-path independence and `sqlite3.connect` open count 0 all passed.
This validates the probe implementation only and is not reported as Windows actual-machine
acceptance.

## Real multi-PC procedure

Generate one non-secret token on PC-A and keep it unchanged for every command. No files are
copied between PCs. The token is coordination metadata only; persistent synchronization is
exclusively through Supabase.

PC-A writes the first marker and saves its exact cleanup snapshot locally:

```bash
TOKEN="__stage5_multipc_acceptance_$(date +%Y%m%d%H%M%S)-$RANDOM"
.venv/bin/python scripts/supabase_migration/stage5_external_acceptance.py multipc-write --token "$TOKEN" --phase a_to_b --save-original
printf '%s\n' "$TOKEN"
```

On PC-B, set `TOKEN` to the displayed non-secret token, then read and reverse-write:

```bash
.venv/bin/python scripts/supabase_migration/stage5_external_acceptance.py multipc-read --token "$TOKEN" --phase a_to_b
.venv/bin/python scripts/supabase_migration/stage5_external_acceptance.py multipc-write --token "$TOKEN" --phase b_to_a
```

Back on PC-A, verify the reverse direction and restore the exact prior setting:

```bash
.venv/bin/python scripts/supabase_migration/stage5_external_acceptance.py multipc-read --token "$TOKEN" --phase b_to_a
.venv/bin/python scripts/supabase_migration/stage5_external_acceptance.py multipc-cleanup --token "$TOKEN"
```

Required evidence:

- PC-A write: `WRITE=PASS`, `MARKER_PHASE=a_to_b`
- PC-B read: `READ=PASS`, `MARKER_PHASE=a_to_b`
- PC-B write: `WRITE=PASS`, `MARKER_PHASE=b_to_a`
- PC-A read: `READ=PASS`, `MARKER_PHASE=b_to_a`
- PC-A cleanup: `CLEANUP=PASS`, `REMAINING_ACCEPTANCE_ROWS=0`,
  `UNEXPECTED_MUTATION=0`
- every step: `RUNTIME_ROLE=clinic_runtime` and `LOCAL_DB_SYNC=0`

Current result: **PENDING — a genuine second PC is not available to this Codex session.**

The complete forward/reverse protocol was run using separate processes on this Mac. Both reads
observed the Supabase marker and cleanup restored the prior setting exactly, with all 19 table
counts unchanged. This is a runbook/helper validation, not genuine multi-PC acceptance.

## Concurrency evidence

The existing PostgreSQL repository tests cover atomic claim/lease behavior, double-claim
prevention, retry, recovery and idempotent finish. A live business job is intentionally not
created for this acceptance. Real multi-PC acceptance validates the shared Source of Truth;
concurrency remains proven by the PostgreSQL automated suite.

## Final status

- Windows actual acceptance: PENDING
- PC-A → Supabase → PC-B: PENDING
- PC-B → Supabase → PC-A: PENDING
- Local helper/runbook validation: PASS
- Full pytest: 1103 passed, 0 failed, 25 skipped; critical skips 0
- Stage5: PENDING EXTERNAL ACCEPTANCE
- Full Supabase Runtime Migration: NOT COMPLETE
