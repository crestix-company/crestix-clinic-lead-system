# Clinic Lead System — Windows verification guide

This guide prepares a separate Windows validation clone for `chore/windows-minimal-slim-20261102`. The launch chain reports the checked-out branch and commit and does not switch branches, fetch, pull, or contact GitHub. Do not use the existing business clone as the transfer target.

## 1. Transfer the committed code without GitHub

After the local restructure commit is created, record its commit hash. On the source computer, from the repository root, create a bundle containing the branch history:

```bash
git bundle create /private/tmp/ClinicLeadSlimWindows-validation.bundle chore/windows-minimal-slim-20261102
git bundle verify /private/tmp/ClinicLeadSlimWindows-validation.bundle
shasum -a 256 /private/tmp/ClinicLeadSlimWindows-validation.bundle
```

Copy the bundle through an approved encrypted file-transfer method or encrypted removable media. The bundle contains committed Git history only; uncommitted files and credentials are not included. Transfer `.supabase-runtime.env.local` separately through the approved credential channel. Never put credentials in the bundle, repository, or logs.

On Windows, use a new validation folder so the existing clone remains untouched:

```powershell
$Bundle = 'D:\SecureTransfer\ClinicLeadSlimWindows-validation.bundle'
$ValidationRepo = Join-Path $env:USERPROFILE 'ClinicLeadSlimWindows-validation'
git bundle verify $Bundle
git clone --branch chore/windows-minimal-slim-20261102 $Bundle $ValidationRepo
Set-Location $ValidationRepo
git branch --show-current
git rev-parse HEAD
git status --short --branch
```

Confirm the branch and hash match the handoff before continuing. Use `Get-FileHash -Algorithm SHA256 $Bundle` to compare the transferred bundle hash. Do not run `git switch main`, `git pull`, or `git fetch`. The Windows launcher is intentionally branch-preserving.

## 2. Read-only credential and first setup

Use a dedicated Supabase PostgreSQL role with **SELECT-only** permissions for validation. Do not copy a production credential that has INSERT/UPDATE/DELETE privileges. Place the credential file at `.supabase-runtime.env.local` using the organization-approved secure transfer method; do not print its contents. Keep `CLINIC_READ_ONLY_UI=1` in the launching PowerShell session. Database-side read-only grants are the hard write boundary; the UI flag alone is not a database security boundary.

Run the first setup from the validation clone:

```powershell
Set-Location $ValidationRepo
$env:CLINIC_READ_ONLY_UI = '1'
.\setup_v2_windows.bat
```

The setup creates `.venv` and installs the locked Python dependencies. If the optional `config\email_research.env.local` is absent, Email Research is unavailable; do not copy that file unless it is needed and approved. No production database write is part of setup.

## 3. Start through the user-facing entrypoint

```powershell
$env:CLINIC_READ_ONLY_UI = '1'
.\ClinicLead.bat
```

The console must show `chore/windows-minimal-slim-20261102` and the expected commit hash. It must not switch branches or update from a remote. Leave the console open. Stop with `Ctrl+C`. `start_v2_windows.bat` is also supported and uses the same branch-preserving chain.

## 4. Safe verification checklist

Use the read-only role throughout. Do not press buttons that import data, create jobs, reclassify clinics, save manual edits, or trigger research.

- **Supabase READ:** confirm the app starts, dashboard counts load, and menus render. Any permission error should be captured for the administrator; do not retry with a write credential.
- **Kouseikyoku:** open the import screen and load `data\master\tokyo_current.csv` or an approved local source file. Confirm the preview, headers, and row counts. Stop before committing/importing it.
- **Google Maps:** load the unpacked extension from `extensions\google-maps-clinic-collector\google-maps-clinic-collector`. Confirm the popup and queue file work. If an external Maps collection is authorized, limit it to one record and save the result CSV locally; do not import the result into Supabase.
- **HP research:** inspect existing HP status/results and confirm filters render. Do not start or resume a research job against production. A real job test requires a non-production database and credentials.
- **Sales targeting:** apply and clear filters, verify counts and clinic IDs, and confirm output excludes non-targets as expected. Keep the session read-only.
- **Comdesk CSV:** download the preview/export locally. Confirm the 28-column header and UUID/clinic-ID mapping. Do not re-upload the file.
- **Migration and protected data:** do not run migrations, import scripts, sidecar rebuilds, or acceptance probes during this UI check.

Record each item as PASS, FAIL, or NOT RUN. A preview is not a completed database import, and a read-only page check is not a successful research-job test.

## 5. Capture errors without exposing credentials

From PowerShell, capture the launcher console to a user-local log:

```powershell
$Log = Join-Path $env:USERPROFILE ("Desktop\ClinicLead-validation-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
Start-Transcript -Path $Log
try { .\ClinicLead.bat } finally { Stop-Transcript }
```

If the app is already running, save a screenshot of the visible error and the console output. Before sharing, redact usernames, clinic/person data, hostnames, URLs containing query tokens, and all credential values. Never attach `.supabase-runtime.env.local`, `.venv`, or database files.

## Items requiring Windows/non-production access

Windows BAT/PowerShell execution, Chrome extension behavior, local CSV download behavior, and visual menu layout require Windows testing. Production Supabase writes and production research jobs are intentionally out of scope.
