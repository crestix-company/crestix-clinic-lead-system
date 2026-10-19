-- Prepare only. A unique Clinic ID guard makes reservations atomic across campaigns.
-- Existing ledger clinics are preloaded so a new campaign cannot race around history.
begin;

create table outreach.clinic_draft_guard (
  clinic_id bigint primary key references public.clinics(id) on delete restrict,
  first_campaign_id text not null,
  first_outreach_id uuid not null,
  created_at timestamptz not null default now()
);

insert into outreach.clinic_draft_guard (clinic_id, first_campaign_id, first_outreach_id)
select distinct on (clinic_id) clinic_id, campaign_id, outreach_id
from outreach.email_delivery_records
order by clinic_id, created_at, outreach_id
on conflict (clinic_id) do nothing;

alter table outreach.clinic_draft_guard enable row level security;
alter table outreach.clinic_draft_guard force row level security;
revoke all on outreach.clinic_draft_guard from public, anon, authenticated, clinic_runtime;
grant select (clinic_id, first_campaign_id, first_outreach_id)
  on outreach.clinic_draft_guard to clinic_outreach_runtime;
grant insert (clinic_id, first_campaign_id, first_outreach_id)
  on outreach.clinic_draft_guard to clinic_outreach_runtime;

create policy clinic_outreach_runtime_draft_guard_select
  on outreach.clinic_draft_guard for select to clinic_outreach_runtime using (true);
create policy clinic_outreach_runtime_draft_guard_insert
  on outreach.clinic_draft_guard for insert to clinic_outreach_runtime with check (true);

commit;
