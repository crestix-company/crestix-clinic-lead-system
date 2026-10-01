"""Tests for the Global Alias Rescue pre-filter (Step 3).

Must scan ALL ACTIVE treatment-category aliases regardless of department, so a
clinic with no department/Navi signal still gets full ACTIVE43 alias coverage.
"""
from src.enrichment.alias_rescue import build_active_alias_index, global_alias_hits
from src.enrichment.treatment_taxonomy import active_treatment_category_names, phase7b_research_categories


def test_alias_index_covers_every_active_category():
    index = build_active_alias_index()
    assert set(index) == set(active_treatment_category_names())


def test_alias_index_aliases_match_taxonomy_source_of_truth():
    index = build_active_alias_index()
    active = phase7b_research_categories()
    for name, item in active.items():
        assert index[name] == tuple(item.get("aliases", ()))


def test_global_alias_hits_finds_category_by_alias_without_department_context():
    text = "当院では胃カメラを実施しております"
    hits = global_alias_hits(text)
    assert "胃カメラ検査" in hits


def test_global_alias_hits_multiple_categories():
    text = "白内障手術とICLに対応しています。ED治療も行っております。"
    hits = global_alias_hits(text)
    assert "白内障手術" in hits
    assert "ICL手術" in hits
    assert "ED治療" in hits


def test_global_alias_hits_empty_text_returns_empty_set():
    assert global_alias_hits("") == set()
    assert global_alias_hits(None) == set()


def test_global_alias_hits_no_match_returns_empty_set():
    assert global_alias_hits("本日は晴天なり") == set()


def test_global_alias_hits_never_matches_deprecated_categories():
    index = build_active_alias_index()
    active_names = set(active_treatment_category_names())
    assert set(index) <= active_names
