-- Additive only. clinic_outreach_runtime currently has zero grants on
-- outreach.clinic_email_summary (confirmed: only the view owner has any
-- privilege on it today). Grant SELECT on the VIEW ONLY -- never on the
-- underlying outreach.email_contact_events base table -- so the application can
-- display per-clinic draft/sent counts without being able to read or write
-- individual event rows. Does not redefine the view or change its columns.

begin;

grant usage on schema outreach to clinic_outreach_runtime;
grant select on outreach.clinic_email_summary to clinic_outreach_runtime;

commit;
