-- Prepare only: enables ID-keyed historical Gmail import for existing clinics whose
-- clinic_uuid is legitimately blank. Do not apply as part of the read-only audit.
begin;

alter table outreach.email_delivery_records
  alter column clinic_uuid drop not null;

grant insert (gmail_draft_id, gmail_message_id, gmail_thread_id)
  on outreach.email_delivery_records to clinic_outreach_runtime;

commit;
