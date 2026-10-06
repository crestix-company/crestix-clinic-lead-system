from src.master.filters import Filters
from src.repository import supabase_filters
from src.repository.supabase_adapter import SupabaseClinicRepository


def test_effective_rank_is_sql_filter_not_python_post_filter():
    filters = Filters(active_only=False, hp_only=False, effective_ranks=["A", "B"])
    assert not supabase_filters.needs_post_filter(filters)
    steps = supabase_filters.clauses(filters, excluded_ids=[])
    label, sql, args = next(step for step in steps if step[0] == "HP ABC判定")
    assert label == "HP ABC判定"
    assert "_h.fetch_status='OK'" in sql
    assert "upper(btrim" in sql
    assert args == ["A", "B", "A", "B"]


def test_effective_rank_join_is_only_added_when_needed():
    base = Filters(active_only=False, hp_only=False)
    ranked = Filters(active_only=False, hp_only=False, effective_ranks=["A", "B"])
    assert SupabaseClinicRepository._from_sql(base) == "public.clinics"
    assert SupabaseClinicRepository._from_sql(ranked) == "public.clinics"
    generic = Filters(active_only=False, hp_only=False, effective_ranks=["D"])
    assert "hp_research.clinic_hp_research" in SupabaseClinicRepository._from_sql(generic)
