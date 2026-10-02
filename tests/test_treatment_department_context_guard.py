"""Regression tests for cross-department Treatment candidate guards.

Department context is only a false-positive guard for ambiguous alias rescue.
It must never be used as proof of treatment delivery and must not suppress
explicit treatment names offered outside the usual department.
"""
from src.enrichment.candidate_map import candidate_union


def _evidence(text):
    return [{"url": "https://clinic.example/", "blocks": [{"text": text}]}]


def test_eye_laser_does_not_rescue_varicose_vein_treatment():
    result = candidate_union(["眼科"], _evidence("当院ではレーザー治療を行っています"))
    assert "下肢静脈瘤血管内治療" not in result


def test_eye_explicit_varicose_treatment_name_is_rescued():
    result = candidate_union(["眼科"], _evidence("当院では下肢静脈瘤血管内治療を行っています"))
    assert "下肢静脈瘤血管内治療" in result


def test_cardiology_laser_keeps_department_candidate():
    result = candidate_union(["循環器内科"], _evidence("レーザー治療"))
    assert "下肢静脈瘤血管内治療" in result


def test_orthopedics_implant_does_not_rescue_dental_implant():
    result = candidate_union(["整形外科"], _evidence("インプラント治療を行っています"))
    assert "歯科インプラント" not in result


def test_dentistry_implant_keeps_department_candidate():
    result = candidate_union(["歯科"], _evidence("インプラント治療を行っています"))
    assert "歯科インプラント" in result


def test_orthopedics_bone_cut_does_not_rescue_facial_contouring():
    result = candidate_union(["整形外科"], _evidence("骨切り術を行っています"))
    assert "輪郭骨切り術" not in result


def test_dermatology_explicit_ed_offer_is_preserved():
    result = candidate_union(["皮膚科"], _evidence("ED治療：バイアグラ、シアリスを取り扱っています"))
    assert "ED治療" in result


def test_internal_medicine_explicit_cpap_offer_is_preserved():
    result = candidate_union(["内科"], _evidence("睡眠時無呼吸症候群に対してCPAP治療を行っています"))
    assert "CPAP療法" in result


def test_cross_department_ambiguous_alias_with_same_sentence_context_is_rescued():
    result = candidate_union(["眼科"], _evidence("下肢静脈瘤に対するレーザー治療を行っています"))
    assert "下肢静脈瘤血管内治療" in result


def test_unrelated_context_elsewhere_does_not_rescue_broad_alias():
    result = candidate_union(
        ["眼科"],
        _evidence("眼科レーザー治療を行っています。下肢のむくみについてもご相談ください。"),
    )
    assert "下肢静脈瘤血管内治療" not in result
