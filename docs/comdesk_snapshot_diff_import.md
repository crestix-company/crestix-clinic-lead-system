# Comdesk CRM snapshot diff import

## CSV input contract

The importer accepts the sales/progress export and the clinic/UUID export as a
pair. It reads UTF-8 with an optional BOM, skips leading blank rows, trims a
trailing tab from headers, and requires 30 columns on each populated row. The
documented positions are checked against the header before any values are
used. The pair is joined only by non-empty Comdesk UUID; duplicate or missing
UUID rows are held. Clinic name, address, and phone must match at least two
independent populated identity fields, and any populated disagreement is held.
The importer never generates a UUID or changes `clinic_id` or
`common_clinic_id`.

The implementation maps the provided contract:

- Sales export: UUID 1, progress 3, last call date 5, caller 6, call count 10,
  filter project 13, project memberships 14, history 19.
- Clinic export: UUID 1, name 3, address 7, phone 10, history 19. Manager and
  email columns are validated by position but discarded immediately.

Projects are split on line breaks and de-duplicated. Each import stores the
latest progress/call state and complete project membership list. A blank
history field is treated as missing and does not erase a previously known
history snapshot. If both exports have non-empty history text, the values must
match.

The supplied original files are not present in the checkout. Tests therefore
use synthetic anonymized CSV bytes constructed from the documented positions;
they validate parser and workflow behavior but do not independently confirm
the original 1,411-row files or every non-key header spelling.

## Snapshot history semantics

`履歴` is a per-clinic free-text snapshot, not a set of call events. The first
non-empty snapshot creates an initial before/after record. Later text changes
create one change record; identical text creates none. No text is expanded into
fabricated calls. A repeated file pair is skipped by its SHA-256 digest pair.
Changed files with unchanged content update observation metadata without
adding a history record. Older snapshots, inconsistent files, and ambiguous
identity matches are held.

## Application path and safety

The existing Comdesk 30-column importer and exporter remain available. The
paired snapshot flow is a separate expander in the existing Comdesk import UI
and calls the new repository preview/import methods. It is disabled unless
`CLINIC_ENVIRONMENT=development`,
`CLINIC_COMDESK_SNAPSHOT_DIFF_ENABLED=1`, and an explicit
`CLINIC_COMDESK_SNAPSHOT_DEVELOPMENT_DB_NAME` are set. It also requires the
`clinic_runtime` login and matching database name, then switches to the
`clinic_comdesk_snapshot_runtime` no-login role. Preview performs SELECT only.

Apply migrations in timestamp order: common clinic identity, identity ACL
hardening, then Comdesk CRM snapshots. The snapshot migration is additive and
uses RLS with narrowly scoped grants; runtime cannot DELETE/TRUNCATE or update
clinic IDs or UUIDs. The paired importer is optional and remains inactive in
environments without the development opt-in. Production rollout still needs a
separate review of the deployment environment, grants, and rollback plan.

## Call-event identifiers

The documented files expose only per-clinic `履歴` text, not a stable call ID
or event update timestamp. This implementation cannot guarantee call-level
deduplication or edits. Obtain a tenant-specific Comdesk API schema or a
dedicated call-history export and confirm stable event ID and update timestamp
before adding event synchronization.

## Validation

`tests/test_comdesk_snapshot_diff.py` covers BOM, leading blank lines, the
trailing-tab project header, positional mappings, paired UUID matching,
identity mismatch, multiple projects, and history snapshot behavior. A local
disposable PostgreSQL run also exercised initial import, identical-file
reimport, changed history and project membership, and linking a Comdesk UUID
to an existing clinic that previously had no Comdesk UUID. The test database
used synthetic clinic data only.
