# Gmail outreach draft creation runbook

This feature only creates Gmail drafts. It contains no message-send operation. The
OAuth `gmail.compose` scope is nevertheless capable of sending mail through other
code, so protect the OAuth token as a credential and do not broaden this tool.

## Preconditions

- Use only a human-approved v2 body exported in the `v2_body` field. The legacy
  Phase 2 body is not read and must never be used as a fallback.
- The candidate CSV must be a private regular file with mode `0600` and contain
  all v2 evidence, approval, schedule, and identity fields required by the CLI.
- The immutable article master is hash-checked before candidates are loaded.
- The local draft ledger is stored below `.secrets/outreach_gmail/`, mode `0600`,
  with its parent directory mode `0700`; the directory is Git-ignored.
- A valid schedule, verified director name, approved Primary Email, verified
  article evidence, and exact template-v2 replay are required.

## DRY_RUN (default)

The following only validates locally; it does not connect to Gmail, read drafts,
write the audit ledger, or create drafts:

```sh
.venv/bin/python scripts/outreach_gmail_create_drafts.py canary \
  --candidates /path/to/approved-canary.csv
```

Canary mode accepts exactly one row and requires its recipient to be
`info@crestix-inc.com`. A dry run reports that OAuth account and duplicate checks
were not performed. A successful content preflight is not a Gmail-ready status.

## Explicit live Canary (do not run without separate approval)

Only after a separate explicit approval for the single write, and after a human
has independently verified the recipient, subject, complete body, article URL,
schedule, and Gmail account, the command shape is:

```sh
.venv/bin/python scripts/outreach_gmail_create_drafts.py canary \
  --candidates /path/to/approved-canary.csv \
  --execute \
  --confirmation I_APPROVE_ONE_CANARY_DRAFT_TO_INFO
```

The command verifies the OAuth account equals the configured account
(`info@crestix-inc.com`), checks existing drafts, reserves the Draft Key in the
local ledger, then calls only `users.drafts.create`. The exact confirmation is a
technical guard, not a substitute for business/user approval. Do not run the
live command as part of ordinary validation or CI.

Batch mode is separately gated, limited to 25 rows per invocation, rejects the
internal Canary recipient, and requires a separate confirmation phrase plus an
exact `--confirm-count`. Batch creation needs separate approval and is out of
scope for the initial Canary.

## Recovery and audit

The Draft Key is included as a custom MIME header and is also checked against the
private JSONL ledger. If a create request times out, the tool re-reads existing
draft metadata and records a recovered draft when it can identify a match. If
the outcome remains uncertain, it records `UNKNOWN` and blocks automatic retry;
an operator must inspect Gmail before any further action. The ledger stores IDs,
status, and timestamps, not email bodies, recipients, or credentials.

No `messages.send` or `drafts.send` call is used. Excel approval alone does not
send a message and does not create a draft.
