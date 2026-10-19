-- Repair default ACLs for installations that already applied the initial
-- identity migration. Safe to reapply; it does not alter clinic or UUID data.
alter function public.prevent_sales_entity_uuid_replacement()
  set search_path = pg_catalog;

create index if not exists sales_entity_identity_audit_entity_idx
  on public.sales_entity_identity_audit(common_clinic_id);

revoke all on public.sales_entities, public.sales_entity_clinics,
  public.sales_entity_comdesk_identity, public.sales_entity_project_memberships,
  public.sales_entity_resolution_queue, public.sales_entity_identity_audit
  from public, anon, authenticated, service_role;
grant select, insert, update on public.sales_entities, public.sales_entity_clinics,
  public.sales_entity_comdesk_identity, public.sales_entity_project_memberships,
  public.sales_entity_resolution_queue, public.sales_entity_identity_audit to service_role;

revoke all on sequence public.sales_entities_common_clinic_id_seq,
  public.sales_entity_resolution_queue_id_seq,
  public.sales_entity_identity_audit_id_seq
  from public, anon, authenticated, service_role;
grant usage, select on sequence public.sales_entities_common_clinic_id_seq,
  public.sales_entity_resolution_queue_id_seq,
  public.sales_entity_identity_audit_id_seq to service_role;
