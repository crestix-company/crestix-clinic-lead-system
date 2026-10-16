# Outreach Gmail OAuth / Canary runbook

This runbook prepares Gmail access for `info@crestix-inc.com`. The preparation
command cannot create drafts and cannot send email.

## OAuth setup and account authorization

OAuth consent and machine-local token state are separate from repository setup. The current
connection must be checked locally with the `status` command; do not record or commit token
contents or infer authorization from this document. The Desktop OAuth client path is
`.secrets/outreach_gmail/client_secret.json`. Do not paste either file's contents into chat, a
ticket, a log, or a command.

1. In Google Cloud Console, choose the Crestix project and enable **Gmail API** under APIs &
   Services → Library. An API enablement check has not been run from this checkout.
2. In Google Auth Platform, configure Branding and Audience. Use **Internal** only if the Cloud
   project belongs to the Crestix Workspace organization. Otherwise use **External** and add
   `info@crestix-inc.com` as a test user. The test-user list, project audience and
   publishing state must be checked in the Cloud Console; this checkout cannot see them.
3. In Data Access, request only
   `https://www.googleapis.com/auth/gmail.compose`. Gmail API draft create/list/get and profile
   verification support this scope. It is classified as **restricted** and it also permits
   sending; Google does not provide a Gmail API scope that permits draft creation while denying
   send. No send operation is implemented in this repository. The token must therefore be
   protected as a send-capable credential.
4. Create an OAuth client of type **Desktop app** (not Web). The code uses Google's installed-app
   browser flow and a temporary loopback callback at `http://127.0.0.1:<ephemeral-port>`; there
   is no fixed web callback URL to register. Keep the local process running while the browser
   consent flow completes.
5. The approved account defaults to `info@crestix-inc.com`. Other representatives can use the same
   implementation by setting `OUTREACH_GMAIL_EXPECTED_ACCOUNT` before starting the command; the
   allowlist check remains mandatory. Any existing token is read/profile-verified before a fresh
   authorization may proceed, and a mismatched token is neither reused nor replaced.
6. Save the downloaded client JSON as
   `.secrets/outreach_gmail/client_secret.json` (or another private path). Set the containing
   directory to `0700`, the JSON file to `0600`, and set
   `OUTREACH_GMAIL_OAUTH_CLIENT_JSON` to its path. The file is Git-ignored. The client file is
   treated as sensitive even though an installed-app client secret is not a reliable secret.
7. From the repository root, the approved account owner runs this locally:

   ```sh
   OUTREACH_GMAIL_EXPECTED_ACCOUNT="info@crestix-inc.com" \
   OUTREACH_GMAIL_OAUTH_CLIENT_JSON="$PWD/.secrets/outreach_gmail/client_secret.json" \
     .venv/bin/python scripts/outreach_gmail_oauth.py authorize
   ```

   In the browser, choose `info@crestix-inc.com` explicitly and inspect the requested
   `gmail.compose` permission before granting. The program calls only Gmail `users.getProfile`
   during account verification, rejects any other mailbox, and saves a token only after the
   exact account matches. It does not create drafts or send email.
8. The refresh token is stored at `.secrets/outreach_gmail/token.json` by default, with parent
   directory `0700` and file `0600`; a refreshed token is written atomically only after account
   verification. Do not copy it to another machine or commit it. To check an existing connection,
   run `.venv/bin/python scripts/outreach_gmail_oauth.py status`; this makes a read-only Gmail
   profile request. `check-duplicates` reads existing draft IDs and only `To`, `Subject`, and
   `X-Clinic-Lead-Draft-Key` metadata. Neither command writes drafts or sends mail.
9. If the app is External and remains in Testing, Google limits access to listed test users and
   test-user authorizations/refresh tokens may expire after seven days for non-basic scopes.
   Expired, revoked, or admin-blocked tokens fail closed and require fresh interactive consent;
   never fall back to another account. For ongoing use, the Workspace administrator must review
   Admin console → Security → Access and data control → API controls → Manage App Access and
   allow only the required app/scope as appropriate. Organization policy can block restricted
   Gmail scopes even when the OAuth project is configured correctly.

OAuth client type and files were inspected without displaying credential values: the local
client JSON is a Desktop (`installed`) client with private file permissions. Google Cloud API
enablement, audience, admin restrictions, and authorization are machine/account-specific and
must be checked through the relevant local command or Cloud Console. The project owner reports
the Cloud setup is Internal.

The Gmail API has no draft-create-only permission: `gmail.compose` permits draft management and
sending. OAuth/preflight uses profile and existing-draft read calls. The separate
`scripts/outreach_gmail_create_drafts.py` implementation creates drafts only, defaults to local
DRY_RUN, requires an explicit mode-specific confirmation for live writes, rechecks the exact
account and existing drafts, and requires human-approved v2 content. No send API is implemented.

## Five-clinic Canary gate

Before invoking the live Canary command (which is not part of ordinary testing):

1. Set future candidate dates in the existing outreach management UI and regenerate the
   protected five-row Canary CSV.
2. Re-run template, unresolved-placeholder, phone-history, listing-status, and article-master
   checks.
3. Verify OAuth reports the exact allowlisted mailbox.
4. Run `.venv/bin/python scripts/outreach_gmail_oauth.py check-duplicates --candidates
   <protected-canary.csv>`. It uses `list_existing_drafts` to read draft IDs plus `To`, `Subject`, and
   `X-Clinic-Lead-Draft-Key` metadata only. Block both exact key matches and conservative
   recipient-plus-subject matches.
5. Have a human approve the intended candidate's recipient, subject, body, URLs, and dates.
   The initial Canary CLI accepts exactly one row and only the internal account
   `info@crestix-inc.com`; the existing five-clinic review cohort is not a batch-write shortcut.
   Live execution requires separate explicit approval and the documented confirmation phrase.
   Sending remains prohibited.

The deterministic draft key covers Clinic UUID, Email Candidate ID, recipient, template
version, recommended article URL, and verified listing URL. The key is attached as
`X-Clinic-Lead-Draft-Key` and associated with the returned Gmail Draft ID in the private local
audit ledger. An uncertain timeout is re-queried and is never automatically retried.
