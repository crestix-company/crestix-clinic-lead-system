-- Local proposal only; DO NOT APPLY without a separately approved schema review.
-- Shared reservation API for future GAS and Clinic Lead adapters.
-- Preserves the existing stricter clinic_draft_guard policy as an additional gate.

begin;

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'clinic_outreach_runtime') then
    raise exception 'clinic_outreach_runtime role must exist before this migration';
  end if;
end $$;

create table outreach.shared_draft_operations (
  operation_key text primary key,
  clinic_id bigint not null references public.clinics(id) on delete restrict,
  clinic_uuid text not null,
  campaign_id text not null,
  gas_case_id text not null default '',
  status text not null default 'PENDING'
    check (status in ('PENDING','DRAFT','SCHEDULED','SENT','REPLIED','TRASHED','UNKNOWN','HOLD')),
  lease_owner uuid,
  lease_expires_at timestamptz,
  fencing_token bigint not null default 0 check (fencing_token >= 0),
  gmail_draft_id text,
  gmail_message_id text,
  gmail_thread_id text,
  scheduled_at timestamptz,
  sent_at timestamptz,
  replied_at timestamptz,
  last_observed_at timestamptz,
  last_error_code text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check ((lease_owner is null) = (lease_expires_at is null)),
  unique (clinic_id, campaign_id),
  check (gas_case_id = '' or length(gas_case_id) <= 200)
);
create unique index shared_draft_operations_gas_case_uq
  on outreach.shared_draft_operations(gas_case_id) where gas_case_id <> '';
create unique index shared_draft_operations_gmail_draft_uq
  on outreach.shared_draft_operations(gmail_draft_id) where gmail_draft_id is not null;
create unique index shared_draft_operations_gmail_message_uq
  on outreach.shared_draft_operations(gmail_message_id) where gmail_message_id is not null;
create index shared_draft_operations_lease_expiry_idx
  on outreach.shared_draft_operations(lease_expires_at) where status in ('PENDING','UNKNOWN','HOLD');

create table outreach.shared_draft_operation_events (
  event_id uuid primary key default gen_random_uuid(),
  operation_key text not null references outreach.shared_draft_operations(operation_key) on delete restrict,
  event_type text not null check (event_type in
    ('RESERVED','LEASE_RECLAIMED','GMAIL_DRAFT_FOUND','GMAIL_CREATE_CONFIRMED',
     'GMAIL_CREATE_AMBIGUOUS','DB_RECONCILED','STATE_OBSERVED','HELD')),
  fencing_token bigint not null,
  gmail_status text not null default 'UNKNOWN'
    check (gmail_status in ('DRAFT','SCHEDULED','SENT','REPLIED','TRASHED','UNKNOWN')),
  gmail_draft_id text,
  gmail_message_id text,
  gmail_thread_id text,
  evidence_key text not null default '',
  created_at timestamptz not null default now()
);
create index shared_draft_operation_events_history_idx
  on outreach.shared_draft_operation_events(operation_key,created_at desc);

-- Atomic shared throttling window for multiple Cloud Run instances. Local
-- in-memory throttling is only a development fallback and is not a guarantee.
create table outreach.shared_draft_api_rate_windows (
  caller_id text not null,
  window_start timestamptz not null,
  request_count integer not null check (request_count >= 0),
  primary key (caller_id, window_start)
);
create or replace function outreach.consume_shared_draft_api_rate(
  p_caller_id text,p_window_start timestamptz,p_limit integer
) returns boolean language plpgsql security definer
set search_path = '' as $$
declare n integer;
begin
  if p_limit < 1 or p_limit > 1000 then raise exception 'invalid rate limit'; end if;
  insert into outreach.shared_draft_api_rate_windows(caller_id,window_start,request_count)
  values (p_caller_id,p_window_start,1)
  on conflict (caller_id,window_start) do update
    set request_count=outreach.shared_draft_api_rate_windows.request_count+1
    where outreach.shared_draft_api_rate_windows.request_count < p_limit
  returning request_count into n;
  return n is not null;
end $$;

create table outreach.gas_case_identity_links (
  gas_case_id text primary key,
  clinic_id bigint not null references public.clinics(id) on delete restrict,
  clinic_uuid text not null,
  operation_key text references outreach.shared_draft_operations(operation_key) on delete restrict,
  match_status text not null default 'REVIEW'
    check (match_status in ('CONFIRMED','REVIEW','CONFLICT')),
  evidence jsonb not null default '{}'::jsonb check (jsonb_typeof(evidence)='object'),
  confirmed_by text not null default '',
  confirmed_at timestamptz,
  source_payload_hash text not null default '',
  created_at timestamptz not null default now(),
  check (match_status <> 'CONFIRMED' or (confirmed_by <> '' and confirmed_at is not null))
);
create index gas_case_identity_links_clinic_idx on outreach.gas_case_identity_links(clinic_id,created_at desc);

create table outreach.shared_calendar_cycles (
  cycle_id uuid primary key default gen_random_uuid(),
  operation_key text not null references outreach.shared_draft_operations(operation_key) on delete restrict,
  cycle_key text not null,
  status text not null check (status in ('PROPOSED','CONFIRMED','CANCELLED','COMPLETED','CONFLICT')),
  calendar_event_id text not null default '',
  starts_at timestamptz,
  ends_at timestamptz,
  meet_url text not null default '',
  source_system text not null check (source_system in ('GAS','CLINIC_LEAD','GOOGLE_CALENDAR')),
  source_event_key text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(operation_key,cycle_key),
  check (ends_at is null or starts_at is null or ends_at > starts_at)
);
create unique index shared_calendar_cycles_event_uq
  on outreach.shared_calendar_cycles(calendar_event_id) where calendar_event_id <> '';

create table outreach.shared_draft_sync_preview (
  preview_id uuid primary key default gen_random_uuid(),
  source_system text not null check (source_system in ('GOOGLE_SHEETS','GAS','CLINIC_LEAD')),
  source_id text not null default '',
  source_tab text not null default '',
  source_row_number integer,
  source_revision text not null default '',
  source_row_hash text not null,
  operation_key text not null default '',
  clinic_uuid text not null default '',
  gas_case_id text not null default '',
  proposed_action text not null check (proposed_action in ('INSERT','UPDATE','NOOP','HOLD')),
  reasons jsonb not null default '[]'::jsonb check (jsonb_typeof(reasons)='array'),
  changed_fields jsonb not null default '[]'::jsonb check (jsonb_typeof(changed_fields)='array'),
  reviewed_by text not null default '',
  reviewed_at timestamptz,
  created_at timestamptz not null default now()
);
create index shared_draft_sync_preview_source_idx
  on outreach.shared_draft_sync_preview(source_system,source_id,source_tab,source_row_number,created_at desc);

-- Fenced atomic lease claim. Reclaim increments fencing_token, so an expired worker
-- cannot finalize after a new worker has claimed the same operation.
create or replace function outreach.acquire_shared_draft_lease(
  p_operation_key text, p_clinic_uuid text, p_clinic_id bigint, p_campaign_id text,
  p_gas_case_id text, p_owner uuid, p_lease_seconds integer
) returns table(acquired boolean, status text, fencing_token bigint,
                gmail_draft_id text, lease_expires_at timestamptz)
language plpgsql security definer set search_path = '' as $$
declare
  r outreach.shared_draft_operations%rowtype;
  guard_campaign text;
  guard_claimed boolean;
  linked_clinic_id bigint;
  linked_clinic_uuid text;
  lease_was_expired boolean := false;
begin
  if p_lease_seconds < 15 or p_lease_seconds > 600 then
    raise exception 'lease duration is outside allowed range';
  end if;
  -- Do not name only the operation_key conflict target. Two concurrent callers
  -- with the same clinic/campaign but different operation keys otherwise race
  -- on two unique indexes and can deadlock. Any unique conflict is followed by
  -- a deterministic locked read and explicit identity/key validation.
  insert into outreach.shared_draft_operations
    (operation_key,clinic_uuid,clinic_id,campaign_id,gas_case_id)
  values (p_operation_key,p_clinic_uuid,p_clinic_id,p_campaign_id,coalesce(p_gas_case_id,''))
  on conflict do nothing;
  select * into r from outreach.shared_draft_operations
    where operation_key=p_operation_key or (clinic_id=p_clinic_id and campaign_id=p_campaign_id)
    order by (operation_key=p_operation_key) desc
    limit 1 for update;
  if not found then
    raise exception 'operation reservation conflicts with an existing unique identity';
  end if;
  if r.operation_key <> p_operation_key then
    return query select false,'OPERATION_KEY_CONFLICT',r.fencing_token,r.gmail_draft_id,r.lease_expires_at;
    return;
  end if;
  if r.clinic_uuid <> p_clinic_uuid or r.clinic_id <> p_clinic_id
     or r.campaign_id <> p_campaign_id
     or (r.gas_case_id <> '' and p_gas_case_id <> '' and r.gas_case_id <> p_gas_case_id) then
    raise exception 'operation identity conflict';
  end if;
  -- Preserve the existing permanent clinic-wide draft guard. It intentionally
  -- blocks a different campaign until an operator resolves the prior history.
  insert into outreach.clinic_draft_guard(clinic_id,first_campaign_id,first_outreach_id)
    values (p_clinic_id,p_campaign_id,gen_random_uuid())
    on conflict (clinic_id) do nothing returning true into guard_claimed;
  if not coalesce(guard_claimed,false) then
    select first_campaign_id into guard_campaign from outreach.clinic_draft_guard
      where clinic_id=p_clinic_id;
    if guard_campaign is distinct from p_campaign_id then
      return query select false,'CROSS_CAMPAIGN_HOLD',r.fencing_token,r.gmail_draft_id,r.lease_expires_at;
      return;
    end if;
  end if;
  if p_gas_case_id <> '' then
    insert into outreach.gas_case_identity_links
      (gas_case_id,clinic_id,clinic_uuid,operation_key,match_status,evidence,confirmed_by,confirmed_at)
    values (p_gas_case_id,p_clinic_id,p_clinic_uuid,p_operation_key,'CONFIRMED',
      jsonb_build_object('method','shared_api_confirmed_identity'), 'shared_draft_api', now())
    on conflict (gas_case_id) do nothing;
    select clinic_id,clinic_uuid into linked_clinic_id,linked_clinic_uuid
      from outreach.gas_case_identity_links where gas_case_id=p_gas_case_id;
    if linked_clinic_id is distinct from p_clinic_id or linked_clinic_uuid is distinct from p_clinic_uuid then
      raise exception 'GAS case identity conflict';
    end if;
  end if;
  if r.status in ('DRAFT','SCHEDULED','SENT','REPLIED','TRASHED') then
    return query select false,r.status,r.fencing_token,r.gmail_draft_id,r.lease_expires_at;
    return;
  end if;
  if r.lease_expires_at > now() and r.lease_owner is distinct from p_owner then
    return query select false,'LEASED',r.fencing_token,r.gmail_draft_id,r.lease_expires_at;
    return;
  end if;
  if r.status in ('UNKNOWN','HOLD') and r.lease_owner is null then
    return query select false,r.status,r.fencing_token,r.gmail_draft_id,r.lease_expires_at;
    return;
  end if;
  lease_was_expired := r.lease_owner is not null and r.lease_expires_at <= now()
    and r.status in ('PENDING','UNKNOWN','HOLD');
  if lease_was_expired then
    update outreach.shared_draft_operations o set status='UNKNOWN',lease_owner=null,
      lease_expires_at=null,fencing_token=o.fencing_token+1,updated_at=now()
    where o.operation_key=p_operation_key returning * into r;
    insert into outreach.shared_draft_operation_events
      (operation_key,event_type,fencing_token,gmail_status,evidence_key)
    values (p_operation_key,'HELD',r.fencing_token,'UNKNOWN',p_operation_key);
    return query select false,'UNKNOWN',r.fencing_token,r.gmail_draft_id,r.lease_expires_at;
    return;
  end if;
  update outreach.shared_draft_operations o set
    status='PENDING', lease_owner=p_owner,
    lease_expires_at=now() + make_interval(secs=>p_lease_seconds),
    fencing_token=o.fencing_token+1, updated_at=now()
  where o.operation_key=p_operation_key returning * into r;
  insert into outreach.shared_draft_operation_events
    (operation_key,event_type,fencing_token,evidence_key)
  values (p_operation_key,case when r.fencing_token > 1 then 'LEASE_RECLAIMED' else 'RESERVED' end,
          r.fencing_token,p_operation_key);
  return query select true,r.status,r.fencing_token,r.gmail_draft_id,r.lease_expires_at;
end $$;

create or replace function outreach.complete_shared_draft_operation(
  p_operation_key text,p_owner uuid,p_fencing_token bigint,
  p_draft_id text,p_message_id text,p_thread_id text
) returns boolean language plpgsql security definer
set search_path = '' as $$
declare n integer;
begin
  update outreach.shared_draft_operations set status='DRAFT',
    gmail_draft_id=nullif(p_draft_id,''),gmail_message_id=nullif(p_message_id,''),
    gmail_thread_id=nullif(p_thread_id,''),lease_owner=null,lease_expires_at=null,
    updated_at=now(),last_error_code=''
  where operation_key=p_operation_key and lease_owner=p_owner
    and fencing_token=p_fencing_token and lease_expires_at > now()
    and status in ('PENDING','UNKNOWN','HOLD');
  get diagnostics n = row_count;
  if n = 1 then
    insert into outreach.shared_draft_operation_events
      (operation_key,event_type,fencing_token,gmail_status,gmail_draft_id,gmail_message_id,gmail_thread_id)
    values (p_operation_key,'GMAIL_CREATE_CONFIRMED',p_fencing_token,'DRAFT',
            nullif(p_draft_id,''),nullif(p_message_id,''),nullif(p_thread_id,''));
  end if;
  return n=1;
end $$;

-- Record a read-verified Gmail state when the original Draft ID is no longer
-- present (for example Draft -> Scheduled/Sent/Trash). The trusted API adapter
-- supplies the operation key observed in the message metadata as evidence_key.
create or replace function outreach.observe_shared_draft_state(
  p_operation_key text,p_clinic_uuid text,p_clinic_id bigint,p_campaign_id text,
  p_status text,p_evidence_key text,p_draft_id text,p_message_id text,p_thread_id text
) returns boolean language plpgsql security definer
set search_path = '' as $$
declare
  n integer;
  next_status text := upper(p_status);
  token bigint;
  current_status text;
begin
  if next_status not in ('DRAFT','SCHEDULED','SENT','REPLIED','TRASHED','UNKNOWN','HOLD')
     or p_evidence_key is distinct from p_operation_key then
    raise exception 'invalid Gmail observation evidence';
  end if;
  select status,fencing_token into current_status,token
    from outreach.shared_draft_operations
    where operation_key=p_operation_key and clinic_uuid=p_clinic_uuid and clinic_id=p_clinic_id
      and campaign_id=p_campaign_id for update;
  if not found then return false; end if;
  if current_status <> next_status and not (
    (current_status in ('PENDING','UNKNOWN','HOLD') and next_status in ('DRAFT','SCHEDULED','SENT','REPLIED','TRASHED','UNKNOWN','HOLD'))
    or (current_status='DRAFT' and next_status in ('SCHEDULED','SENT','TRASHED','REPLIED'))
    or (current_status='SCHEDULED' and next_status in ('SENT','TRASHED','REPLIED'))
    or (current_status='SENT' and next_status='REPLIED')
  ) then
    raise exception 'invalid Gmail state regression';
  end if;
  update outreach.shared_draft_operations set status=next_status,
    gmail_draft_id=coalesce(nullif(p_draft_id,''),gmail_draft_id),
    gmail_message_id=coalesce(nullif(p_message_id,''),gmail_message_id),
    gmail_thread_id=coalesce(nullif(p_thread_id,''),gmail_thread_id),
    scheduled_at=case when next_status='SCHEDULED' then coalesce(scheduled_at,now()) else scheduled_at end,
    sent_at=case when next_status='SENT' then coalesce(sent_at,now()) else sent_at end,
    replied_at=case when next_status='REPLIED' then coalesce(replied_at,now()) else replied_at end,
    last_observed_at=now(),lease_owner=null,lease_expires_at=null,updated_at=now()
  where operation_key=p_operation_key and clinic_uuid=p_clinic_uuid and clinic_id=p_clinic_id
    and campaign_id=p_campaign_id;
  get diagnostics n = row_count;
  if n = 1 then
    insert into outreach.shared_draft_operation_events
      (operation_key,event_type,fencing_token,gmail_status,gmail_draft_id,gmail_message_id,gmail_thread_id,evidence_key)
    values (p_operation_key,'STATE_OBSERVED',token,next_status,nullif(p_draft_id,''),
            nullif(p_message_id,''),nullif(p_thread_id,''),p_evidence_key);
  end if;
  return n=1;
end $$;

alter table outreach.shared_draft_operations enable row level security;
alter table outreach.shared_draft_operations force row level security;
alter table outreach.shared_draft_operation_events enable row level security;
alter table outreach.shared_draft_operation_events force row level security;
alter table outreach.shared_draft_api_rate_windows enable row level security;
alter table outreach.shared_draft_api_rate_windows force row level security;
alter table outreach.gas_case_identity_links enable row level security;
alter table outreach.gas_case_identity_links force row level security;
alter table outreach.shared_calendar_cycles enable row level security;
alter table outreach.shared_calendar_cycles force row level security;
alter table outreach.shared_draft_sync_preview enable row level security;
alter table outreach.shared_draft_sync_preview force row level security;

do $$ declare t text; begin
  foreach t in array array['shared_draft_operations','shared_draft_operation_events',
                           'shared_draft_api_rate_windows',
    'gas_case_identity_links','shared_calendar_cycles','shared_draft_sync_preview'] loop
    execute format('revoke all on outreach.%I from public, anon, authenticated, clinic_runtime',t);
    execute format('grant select on outreach.%I to clinic_outreach_runtime',t);
    execute format('create policy %I on outreach.%I for all to clinic_outreach_runtime using (true) with check (true)',
      'clinic_outreach_runtime_'||t||'_access',t);
  end loop;
end $$;
revoke delete,truncate on outreach.shared_draft_operations from clinic_outreach_runtime;
revoke insert,update,delete,truncate on outreach.shared_draft_operations from clinic_outreach_runtime;
revoke insert,update,delete,truncate on outreach.shared_draft_operation_events from clinic_outreach_runtime;
revoke insert,update,delete,truncate on outreach.gas_case_identity_links from clinic_outreach_runtime;
revoke insert,update,delete,truncate on outreach.shared_calendar_cycles from clinic_outreach_runtime;
revoke insert,update,delete,truncate on outreach.shared_draft_sync_preview from clinic_outreach_runtime;
revoke delete,truncate on outreach.gas_case_identity_links from clinic_outreach_runtime;
revoke delete,truncate on outreach.shared_calendar_cycles from clinic_outreach_runtime;
revoke delete,truncate on outreach.shared_draft_sync_preview from clinic_outreach_runtime;
revoke all on function outreach.acquire_shared_draft_lease(text,text,bigint,text,text,uuid,integer) from public,anon,authenticated;
revoke all on function outreach.complete_shared_draft_operation(text,uuid,bigint,text,text,text) from public,anon,authenticated;
revoke all on function outreach.observe_shared_draft_state(text,text,bigint,text,text,text,text,text,text) from public,anon,authenticated;
revoke all on function outreach.consume_shared_draft_api_rate(text,timestamptz,integer) from public,anon,authenticated;
grant execute on function outreach.acquire_shared_draft_lease(text,text,bigint,text,text,uuid,integer) to clinic_outreach_runtime;
grant execute on function outreach.complete_shared_draft_operation(text,uuid,bigint,text,text,text) to clinic_outreach_runtime;
grant execute on function outreach.observe_shared_draft_state(text,text,bigint,text,text,text,text,text,text) to clinic_outreach_runtime;
grant execute on function outreach.consume_shared_draft_api_rate(text,timestamptz,integer) to clinic_outreach_runtime;
grant usage on schema outreach to clinic_outreach_runtime;

commit;
