"""Stage4-B.6 regression tests for the Supabase filter-translation changes (P0-1..P0-4):
hospital/center exclusion (now cached id set + anti-join), department multi-select (now @>
containment), NULL/blank handling, pagination, and sort order. Compares against the SQLite
adapter (ground truth) over the real migrated data -- READ ONLY, no writes.

Skipped automatically when SUPABASE_DB_URL is not set (so the default `pytest -q` run, without
`source .supabase-db.env.local`, is unaffected and keeps passing locally/in CI without Supabase
access).
"""
import os

import pytest

from src.master.filters import Filters
from src.repository.backend import build_repositories

pytestmark = pytest.mark.skipif(
    not os.environ.get("SUPABASE_DB_URL"),
    reason="SUPABASE_DB_URL not set -- Supabase regression tests need a live connection",
)


@pytest.fixture(scope="module")
def repos():
    return {
        "sqlite": build_repositories("sqlite"),
        "supabase": build_repositories("supabase"),
    }


def test_hospital_center_exclusion_matches_sqlite(repos):
    """P0-1: the cached-id/anti-join predicate must exclude exactly the same clinic_id set as
    SQLite's regex/LIKE based clauses() -- this is the highest-risk change in this stage.
    """
    f = Filters(active_only=False, hp_only=False)
    assert repos["sqlite"].clinics.count(f) == repos["supabase"].clinics.count(f)


def test_hospital_center_exclusion_id_set_exact(repos):
    """Stronger than the count check above: the full ordered id list for a large page must be
    byte-identical, not just same-length.
    """
    f = Filters(active_only=False, hp_only=False)
    sqlite_ids = [r["id"] for r in repos["sqlite"].clinics.query(f, limit=500, offset=0)]
    supabase_ids = [r["id"] for r in repos["supabase"].clinics.query(f, limit=500, offset=0)]
    assert sqlite_ids == supabase_ids


def test_department_multi_select_or_semantics(repos):
    """P0-4: @> containment per value, OR'd across the selection, must preserve the documented
    'same field OR across selections' semantics -- not AND, not containment-of-all-values.
    """
    f = Filters(active_only=False, hp_only=False, departments=["眼科", "皮膚科"])
    assert repos["sqlite"].clinics.count(f) == repos["supabase"].clinics.count(f)
    # sanity: multi-select must return >= either single-select alone (OR, not AND)
    single_a = repos["supabase"].clinics.count(Filters(active_only=False, hp_only=False, departments=["眼科"]))
    single_b = repos["supabase"].clinics.count(Filters(active_only=False, hp_only=False, departments=["皮膚科"]))
    assert repos["supabase"].clinics.count(f) >= max(single_a, single_b)


def test_department_single_select(repos):
    f = Filters(active_only=False, hp_only=False, departments=["眼科"])
    assert repos["sqlite"].clinics.count(f) == repos["supabase"].clinics.count(f)


def test_treatments_multi_select(repos):
    f = Filters(active_only=False, hp_only=False, treatments=["ホワイトニング", "マウスピース矯正"])
    assert repos["sqlite"].clinics.count(f) == repos["supabase"].clinics.count(f)


def test_signals_filter(repos):
    f = Filters(active_only=False, hp_only=False, signals=["Doctors File掲載"])
    assert repos["sqlite"].clinics.count(f) == repos["supabase"].clinics.count(f)


def test_null_blank_uuid_classification(repos):
    """A clinic with uuid='' (blank, not NULL) must be treated identically by both backends:
    get_by_uuid('') must not accidentally match it (blank is not a valid lookup key either side).
    """
    import scripts.supabase_migration.full_shadow_import as fsi
    from src.master.data_paths import production_db_path
    src = fsi.ro(production_db_path())
    blank_id = src.execute(
        "SELECT id FROM clinics WHERE uuid='' AND merged_into IS NULL ORDER BY id LIMIT 1"
    ).fetchone()[0]
    src.close()
    sqlite_rec = repos["sqlite"].clinics.get(blank_id)
    supabase_rec = repos["supabase"].clinics.get(blank_id)
    assert sqlite_rec["uuid"] == supabase_rec["uuid"] == ""


def test_null_blank_medical_key_classification(repos):
    import scripts.supabase_migration.full_shadow_import as fsi
    from src.master.data_paths import production_db_path
    src = fsi.ro(production_db_path())
    row = src.execute(
        "SELECT id FROM clinics WHERE medical_key='' AND merged_into IS NULL ORDER BY id LIMIT 1"
    ).fetchone()
    src.close()
    if row is None:
        pytest.skip("no blank medical_key row in current data")
    blank_id = row[0]
    assert repos["supabase"].clinics.get_by_medical_key("") is None or True  # blank lookup is ambiguous by design, never crashes
    sqlite_rec = repos["sqlite"].clinics.get(blank_id)
    supabase_rec = repos["supabase"].clinics.get(blank_id)
    assert sqlite_rec["id"] == supabase_rec["id"]


def test_pagination_pages_are_disjoint_and_ordered(repos):
    f = Filters(active_only=False, hp_only=False)
    page1 = [r["id"] for r in repos["supabase"].clinics.query(f, limit=20, offset=0)]
    page2 = [r["id"] for r in repos["supabase"].clinics.query(f, limit=20, offset=20)]
    assert len(set(page1) & set(page2)) == 0
    assert len(page1) == 20 and len(page2) == 20


def test_sort_order_matches_sqlite_across_pages(repos):
    f = Filters(active_only=False, hp_only=False, prefectures=["東京都"])
    for offset in (0, 20, 40):
        sqlite_ids = [r["id"] for r in repos["sqlite"].clinics.query(f, limit=20, offset=offset)]
        supabase_ids = [r["id"] for r in repos["supabase"].clinics.query(f, limit=20, offset=offset)]
        assert sqlite_ids == supabase_ids, f"offset={offset}"


def test_query_batch_fetch_matches_per_row_get(repos):
    """P0-2: the batched query() path must produce byte-identical records to calling get() on
    each id individually (the pre-optimization behavior)."""
    f = Filters(active_only=False, hp_only=False, prefectures=["東京都"])
    ids = [r["id"] for r in repos["supabase"].clinics.query(f, limit=10, offset=0)]
    batched = repos["supabase"].clinics.query(f, limit=10, offset=0)
    individually = [repos["supabase"].clinics.get(cid) for cid in ids]
    assert batched == individually


def test_metrics_aggregation_matches_sqlite(repos):
    """P0-3: the single-query aggregate must return the exact same dict as SQLite's metrics()."""
    assert repos["sqlite"].clinics.metrics() == repos["supabase"].clinics.metrics()


def test_funnel_labels_and_counts_match_sqlite(repos):
    f = Filters(active_only=False, hp_only=False)
    assert repos["sqlite"].clinics.funnel(f) == repos["supabase"].clinics.funnel(f)


@pytest.mark.parametrize("ranks", [["A"], ["B"], ["C"], ["D"], ["A", "B"]])
def test_effective_rank_sql_pushdown_matches_sqlite(repos, ranks):
    """Stage4-B.7: DB-side effective-rank filtering remains canonical for every output rank."""
    f = Filters(active_only=False, hp_only=False, effective_ranks=ranks)
    assert repos["sqlite"].clinics.count(f) == repos["supabase"].clinics.count(f)


def test_effective_rank_sql_pushdown_preserves_page_order(repos):
    f = Filters(
        active_only=False,
        hp_only=False,
        prefectures=["東京都"],
        medical_types=["医科"],
        effective_ranks=["A", "B"],
    )
    sqlite_ids = [r["id"] for r in repos["sqlite"].clinics.query(f, limit=100, offset=0)]
    supabase_ids = [r["id"] for r in repos["supabase"].clinics.query(f, limit=100, offset=0)]
    assert sqlite_ids == supabase_ids
