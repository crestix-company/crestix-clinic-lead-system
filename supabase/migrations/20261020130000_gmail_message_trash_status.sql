-- Forward-only proposal; do not apply to Production without a separate approval.
-- Keep Gmail Trash distinct from a user/API cancellation.
-- API and historical `TRASHED` labels are normalized to `TRASH` before persistence.

begin;

alter table outreach.gmail_messages
  drop constraint if exists gmail_messages_message_status_check;

alter table outreach.gmail_messages
  add constraint gmail_messages_message_status_check
  check (message_status in (
    'DRAFT', 'SCHEDULED', 'SENT', 'REPLIED', 'TRASH',
    'CANCELLED', 'UNKNOWN', 'CONFLICT'
  ));

comment on column outreach.gmail_messages.message_status is
  'Gmail lifecycle state. TRASH is distinct from CANCELLED; legacy/API TRASHED values must be normalized to TRASH. State changes are retained in email_state_history.';

commit;
