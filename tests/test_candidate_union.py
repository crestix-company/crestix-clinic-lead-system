"""Tests for candidate_union (Step 2 + Step 3 combined): the only set of categories
that may reach the heavy v4 evidence engine per clinic.

candidate_union = department_candidates ∪ global_alias_hits. Must never be used to
CONFIRM anything by itself, and must never be empty-because-no-department -- a
clinic with no department signal still gets full alias coverage.
"""
from src.enrichment.candidate_map import candidate_union


def _evidence(text):
    return [{"url": "https://clinic.example/", "blocks": [{"text": text}]}]


def test_candidate_union_department_only_no_alias_hit():
    result = candidate_union(["眼科"], _evidence("当院のご案内です"))
    assert {"白内障手術", "ICL手術", "MIGS・緑内障レーザー治療", "オルソケラトロジー"} <= result


def test_candidate_union_alias_rescues_category_outside_department_candidates():
    # No department signal at all, but the HP text itself mentions a treatment.
    result = candidate_union([], _evidence("胃カメラ検査を行っています"))
    assert "胃カメラ検査" in result


def test_candidate_union_missing_department_does_not_drop_clinic():
    # Guarantee: absent/unknown department must not make candidate_union empty when
    # the HP text has real evidence -- alias rescue must still fire.
    result = candidate_union(None, _evidence("ED治療を提供しております"))
    assert "ED治療" in result


def test_candidate_union_no_department_and_no_alias_hit_is_legitimately_empty():
    # A clean miss (no department, no alias text match) is a correct empty result,
    # not a bug -- there is genuinely nothing for the v4 engine to evaluate.
    result = candidate_union([], _evidence("本日は晴天なり"))
    assert result == set()

    result_no_text = candidate_union([], [])
    assert result_no_text == set()


def test_candidate_union_is_union_not_intersection():
    result = candidate_union(["眼科"], _evidence("胃カメラ検査を行っています"))
    assert "白内障手術" in result  # from department
    assert "胃カメラ検査" in result  # from alias rescue, unrelated department
