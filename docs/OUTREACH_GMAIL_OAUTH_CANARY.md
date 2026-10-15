# Outreach Gmail OAuth / Canary runbook

This runbook prepares Gmail access for `kazuma.takahara@crestix-inc.com`. The preparation
command cannot create drafts and cannot send email.

## OAuth setup

1. In the Crestix Google Cloud project, enable the Gmail API and configure an Internal OAuth
   consent screen.
2. Create a Desktop application OAuth client. Add only
   `https://www.googleapis.com/auth/gmail.compose` to the requested Gmail scopes.
3. Store the downloaded client JSON outside Git, with directory mode `0700` and file mode
   `0600`. Never paste its contents into a ticket, log, report, or terminal command.
4. Set `OUTREACH_GMAIL_OAUTH_CLIENT_JSON` to that path. Optionally set
   `OUTREACH_GMAIL_OAUTH_TOKEN_JSON`; otherwise the ignored path
   `.secrets/outreach_gmail/token.json` is used.
5. Run `.venv/bin/python scripts/outreach_gmail_oauth.py authorize`, select
   `kazuma.takahara@crestix-inc.com`, and confirm the reported account. Any other account is
   rejected and its token is not saved.
6. Later read-only checks use `.venv/bin/python scripts/outreach_gmail_oauth.py status`.

The Gmail API does not offer a draft-create-only scope. `gmail.compose` is the narrowest scope
that supports draft creation, but it also authorizes sending. This repository therefore keeps
all send operations absent from the OAuth/preflight implementation and requires an explicit
future draft-create action after Canary approval.

## Five-clinic Canary gate

Before any Gmail write implementation is invoked:

1. Set future candidate dates in the existing outreach management UI and regenerate the
   protected five-row Canary CSV.
2. Re-run template, unresolved-placeholder, phone-history, listing-status, and article-master
   checks.
3. Verify OAuth reports the exact allowlisted mailbox.
4. Run `.venv/bin/python scripts/outreach_gmail_oauth.py check-duplicates --candidates
   <protected-canary.csv>`. It uses `list_existing_drafts` to read draft IDs plus `To`, `Subject`, and
   `X-Clinic-Lead-Draft-Key` metadata only. Block both exact key matches and conservative
   recipient-plus-subject matches.
5. Have a human approve all five recipients, subjects, bodies, URLs, and dates. Only a separate,
   explicit future operation may create the five drafts. Sending remains prohibited.

The deterministic draft key covers Clinic UUID, Email Candidate ID, recipient, template
version, recommended article URL, and verified listing URL. When draft creation is implemented,
the same key must be saved as `X-Clinic-Lead-Draft-Key` and recorded with the returned Gmail
Draft ID.
