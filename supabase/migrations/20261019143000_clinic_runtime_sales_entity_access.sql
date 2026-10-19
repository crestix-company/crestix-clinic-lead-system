-- Least-privilege access for the existing application login. Common identity
-- remains disabled in app config until this migration and runtime-role checks pass.
revoke all on public.sales_entities, public.sales_entity_clinics,
  public.sales_entity_comdesk_identity, public.sales_entity_project_memberships,
  public.sales_entity_resolution_queue, public.sales_entity_identity_audit
  from clinic_runtime;
revoke update (clinic_id) on public.sales_entity_clinics from clinic_runtime;
revoke all on sequence public.sales_entities_common_clinic_id_seq,
  public.sales_entity_resolution_queue_id_seq,
  public.sales_entity_identity_audit_id_seq from clinic_runtime;
revoke all on sequence public.sales_entities_common_clinic_id_seq,
  public.sales_entity_resolution_queue_id_seq,
  public.sales_entity_identity_audit_id_seq from public, anon, authenticated;

grant usage on schema public to clinic_runtime;
grant select, insert on public.sales_entities to clinic_runtime;
grant select, insert on public.sales_entity_clinics to clinic_runtime;
-- Required only for SELECT ... FOR UPDATE OF m in the importer. The UPDATE
-- policy's false WITH CHECK rejects all actual row updates.
grant update (clinic_id) on public.sales_entity_clinics to clinic_runtime;
grant select, insert on public.sales_entity_comdesk_identity to clinic_runtime;
grant select, insert on public.sales_entity_identity_audit to clinic_runtime;
grant usage on sequence public.sales_entities_common_clinic_id_seq,
  public.sales_entity_identity_audit_id_seq to clinic_runtime;

drop policy if exists sales_entities_runtime_select on public.sales_entities;
create policy sales_entities_runtime_select on public.sales_entities
  for select to clinic_runtime using (true);
drop policy if exists sales_entities_runtime_insert on public.sales_entities;
create policy sales_entities_runtime_insert on public.sales_entities
  for insert to clinic_runtime with check (true);

drop policy if exists sales_entity_clinics_runtime_select on public.sales_entity_clinics;
create policy sales_entity_clinics_runtime_select on public.sales_entity_clinics
  for select to clinic_runtime using (true);
drop policy if exists sales_entity_clinics_runtime_insert on public.sales_entity_clinics;
create policy sales_entity_clinics_runtime_insert on public.sales_entity_clinics
  for insert to clinic_runtime with check (true);
drop policy if exists sales_entity_clinics_runtime_lock on public.sales_entity_clinics;
create policy sales_entity_clinics_runtime_lock on public.sales_entity_clinics
  for update to clinic_runtime using (true) with check (false);

drop policy if exists sales_entity_comdesk_identity_runtime_select on public.sales_entity_comdesk_identity;
create policy sales_entity_comdesk_identity_runtime_select on public.sales_entity_comdesk_identity
  for select to clinic_runtime using (true);
drop policy if exists sales_entity_comdesk_identity_runtime_insert on public.sales_entity_comdesk_identity;
create policy sales_entity_comdesk_identity_runtime_insert on public.sales_entity_comdesk_identity
  for insert to clinic_runtime with check (true);

drop policy if exists sales_entity_identity_audit_runtime_insert on public.sales_entity_identity_audit;
create policy sales_entity_identity_audit_runtime_insert on public.sales_entity_identity_audit
  for insert to clinic_runtime with check (true);
drop policy if exists sales_entity_identity_audit_runtime_select on public.sales_entity_identity_audit;
create policy sales_entity_identity_audit_runtime_select on public.sales_entity_identity_audit
  for select to clinic_runtime using (true);
