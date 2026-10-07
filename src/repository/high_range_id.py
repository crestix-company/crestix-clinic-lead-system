"""Stage4-D Owner Decision 2: reserved numeric ID namespace for Supabase-generated rows.

Only two tables have application code that captures a newly generated integer PK and reuses
it within the same operation (re-derived from the three live `lastrowid` callsites in
src/master/store.py -- see docs/supabase_migration/22_stage4d_write_inventory_gate.md):

  - public.clinics.id          (store.py:396, store.py:694)
  - provenance.source_records.id (store.py:402)

Reserved floor: Supabase-generated IDs for these two tables must be >= RESERVED_ID_FLOOR, so a
later rollback to CLINIC_WRITE_BACKEND=sqlite can never have SQLite independently allocate an ID
that collides with one Supabase already generated (dual-write / backfill stays forbidden; this
is the disjoint-namespace alternative). Existing imported IDs are never touched by this guard.
"""

RESERVED_ID_FLOOR = 1_000_000_000

HIGH_RANGE_TABLES = ("public.clinics", "provenance.source_records")


class HighRangeIdViolation(Exception):
    """A Supabase-generated ID for a high-range table came back below RESERVED_ID_FLOOR.

    Per Owner Decision 2, this is a guard failure, not a silent acceptance: the caller must
    treat the INSERT as failed (the transaction must not be committed) and surface this as an
    explicit error -- never fall back to SQLite and never retry with a different floor.
    """


def assert_high_range(generated_id, *, table):
    if table not in HIGH_RANGE_TABLES:
        raise AssertionError(f"{table} is not a high-range-guarded table: {HIGH_RANGE_TABLES}")
    if generated_id < RESERVED_ID_FLOOR:
        raise HighRangeIdViolation(
            f"{table}: Supabase-generated id={generated_id} is below the reserved floor "
            f"{RESERVED_ID_FLOOR}. This indicates the sequence/identity was not created with "
            f"the required start value -- STOP, do not insert, do not retry with a lower floor."
        )
    return generated_id
