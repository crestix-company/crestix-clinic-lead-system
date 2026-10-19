-- Additive only. Widens the status check constraint on
-- outreach.email_delivery_records to allow 'TRASHED' as its own distinct value.
-- Previously a trashed Gmail match was recorded as a generic 'REVIEW', which is
-- safe but loses the specific reason once stored. Does not touch any other
-- table, role, or policy.
--
-- Must run AFTER 20261016172806_outreach_email_delivery_records.sql (that
-- migration creates outreach.email_delivery_records; this one only alters it).
-- Verified locally (initdb + psql, not against Supabase) against a from-scratch
-- reproduction of the current production schema: applies cleanly, and rolls
-- back atomically if any statement in this file fails.

begin;

alter table outreach.email_delivery_records
  drop constraint if exists email_delivery_records_status_check;
alter table outreach.email_delivery_records
  add constraint email_delivery_records_status_check
  check (status in ('DRAFT','SENT','SCHEDULED','REPLIED','TRASHED','FAILED','UNKNOWN','REVIEW'));

commit;
