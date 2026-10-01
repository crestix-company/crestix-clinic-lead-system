"""Tests for the Treatment Candidate Map pre-filter (Step 2).

Candidate Map must only narrow which categories reach the heavy v4 evidence engine;
it must never itself decide CONFIRMED, and missing department data must not silently
drop a clinic (that guarantee lives in candidate_union()/global_alias_hits(), see
tests/test_alias_rescue.py and tests/test_candidate_union.py).
"""
from src.enrichment.candidate_map import (
    build_department_candidate_map,
    department_candidates,
    evidence_text,
)
from src.enrichment.treatment_taxonomy import phase7b_research_categories


def test_department_candidate_map_matches_taxonomy_source_of_truth():
    mapping = build_department_candidate_map()
    active = phase7b_research_categories()
    for name, item in active.items():
        for dept in item["crestix_departments"]:
            assert name in mapping[dept]


def test_department_candidates_known_examples():
    mapping = build_department_candidate_map()
    eye = department_candidates(["眼科"], mapping)
    assert {"白内障手術", "ICL手術", "MIGS・緑内障レーザー治療", "オルソケラトロジー"} <= eye

    gi = department_candidates(["消化器内科"], mapping)
    assert {"胃カメラ検査", "大腸カメラ検査"} <= gi

    urology = department_candidates(["泌尿器科"], mapping)
    assert len(urology) > 0


def test_department_candidates_empty_department_returns_empty_set():
    # No department signal must not raise or crash -- caller falls back to alias rescue.
    assert department_candidates([]) == set()
    assert department_candidates(None) == set()


def test_department_candidates_unknown_department_returns_empty_set():
    assert department_candidates(["心療内科"]) == set()


def test_department_candidates_never_returns_deprecated_categories():
    mapping = build_department_candidate_map()
    active_names = set(phase7b_research_categories())
    for candidates in mapping.values():
        assert candidates <= active_names


def test_evidence_text_flattens_blocks_across_pages():
    evidence = [
        {"url": "https://a.example/", "blocks": [{"text": "胃カメラ検査を実施しています"}]},
        {"url": "https://a.example/access", "blocks": [{"text": "アクセス"}, {"text": "住所"}]},
    ]
    text = evidence_text(evidence)
    assert "胃カメラ検査を実施しています" in text
    assert "アクセス" in text
    assert "住所" in text


def test_evidence_text_handles_empty_evidence():
    assert evidence_text([]) == ""
    assert evidence_text(None) == ""
