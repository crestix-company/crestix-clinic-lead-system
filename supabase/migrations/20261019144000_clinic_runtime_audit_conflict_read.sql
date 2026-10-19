-- The importer uses ON CONFLICT(event_key) DO NOTHING. PostgreSQL requires
-- SELECT privilege and a SELECT policy for an arbiter-targeted conflict check.
grant select on public.sales_entity_identity_audit to clinic_runtime;
drop policy if exists sales_entity_identity_audit_runtime_select on public.sales_entity_identity_audit;
create policy sales_entity_identity_audit_runtime_select on public.sales_entity_identity_audit
  for select to clinic_runtime using (true);
