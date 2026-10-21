# Clinic identity & Comdesk cross-project UUID design (draft, non-production)

## Goal
One *verified sales destination* = one stable sales_entity_id = at most one Comdesk UUID,
regardless of how many departments, therapy tags, or Comdesk project memberships it has.
The same exported UUID must be reused in every project for that destination.

## Identity boundaries
- public.clinics.id is an immutable database row PK, NOT a shared sales-destination identity.
- public.clinics.medical_key and medical_type represent medical/dental registrations; preserve every record.
- sales_entity_id is a new canonical business identity; clinic IDs point to it via an audited membership table.
- Comdesk UUID belongs to the sales entity, never to the medical specialty or export batch.
- Comdesk project memberships belong to (sales_entity_id, project_id), uniquely; projects may have distinct categories while sharing UUID.
- Store existing UUID mappings with original source, time, owner and verification evidence. Never regenerate a mapped UUID.

## Proposed schema (NOT applied)
sales_entities(entity_id UUID PK, canonical_name, canonical_address, canonical_phone, status, created_at, updated_at)
sales_entity_clinics(entity_id FK, clinic_id FK UNIQUE, match_rule, confidence, reviewed_by, reviewed_at)
sales_entity_comdesk_identity(entity_id FK UNIQUE, comdesk_uuid TEXT UNIQUE NOT NULL, provenance, verified_at)
sales_entity_project_memberships(entity_id FK, project_key TEXT, PRIMARY KEY(entity_id, project_key))
sales_entity_resolution_queue(clinic_ids, reasons, status, reviewer, reviewed_at)
sales_entity_identity_audit(entity_id, old_values, new_values, reason, actor, event_time)

## Match policy
1. Normalize name, full address and telephone; account for building/floor distinctions.
2. A combination of same normalized name + full address + phone is a candidate (not sufficient proof for every medical/dental pair).
3. Different phone, different address, multiple current UUIDs, contradictory hospital units, ambiguous codes, or same-name chain clinics: HOLD for review.
4. If a canonical entity already exists, reuse its ID and Comdesk UUID.
5. Otherwise create entity only after verified matching; do NOT change legacy clinic.id, clinics.uuid, or source medical_key.
6. For multiple UUIDs pointing to a single clinic: do not pick one silently; reconcile against Comdesk history and approve.
7. Preserve project-specific treatment tags; do not flatten clinic source records.
8. Reimport must be idempotent; concurrency must not create duplicate entities. Enforce unique constraints and transactional claim/resolve.
9. Export should be unique within each project on (project_key, sales_entity_id), and share the exact UUID value across projects. An existing UUID is never replaced with a newly generated value.
10. If a project is intended to independently call the same entity, define shared history vs shared suppression policy deliberately; a shared UUID is NOT proof of automatic cross-project call history synchronization.

## Rollout gates
0. READ ONLY audit of UUIDs in all relevant datasets, external Comdesk UUID semantics and any foreign-key dependencies.
1. Add isolated schema in a development branch (not production); review RLS, access control, and migration rollback.
2. Backfill candidate mappings as preview only; approve no-ambiguity samples and review conflicting ones.
3. Integrate resolver into all ingestion/update paths including new clinics and source imports.
4. Integrate Comdesk 30-column CSV export; preserve column order, UUID field and project-specific content.
5. Test inter-project same UUID on Comdesk with two controlled test projects: import, call or simulated activity, history/status propagation, de-duplication, permissions, re-import and no data loss.
6. Shadow exports & compare counts; human-approved cutover only. Monitor missing UUID, duplicate entity, reused UUID mismatch and cross-project history.
7. No destructive deletes, existing UUID overwrites, PR merge or production migration before approval and testing.

## Known October 19 audit snapshot
- 648 same-normalized-name+address groups.
- 643 cross medical/dental groups; 629 share normalized phone.
- 5 same-medical-type groups require careful registration review.
- Existing public.clinics UUID unique index only covers nonempty values with merged_into NULL.
- Previous standalone planner in src/sales_entity_dedupe.py is READ ONLY; it does NOT establish persistent entity identity and is NOT connected to Comdesk export.

## Acceptance
- Every verified sales entity receives precisely one canonical ID.
- All constituent clinic row IDs remain intact with reversible links.
- The same entity always exports the same existing Comdesk UUID in all projects and successive exports.
- Duplicate records inside a project and missing/conflicting UUID fail closed.
- No historical call/data loss; cross-project propagation must be observed in Comdesk test and documented separately from our export invariant.
