"""Regression tests for cross-department Treatment candidate guards.

Department context is only a false-positive guard for ambiguous alias rescue.
It must never be used as proof of treatment delivery and must not suppress
explicit treatment names offered outside the usual department.
"""
import pytest

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


def test_iidabashi_regression_keeps_explicit_ed_and_liposuction_but_not_broad_laser():
    departments = ["アレルギー科", "内科", "形成外科", "皮膚科", "美容外科"]
    result = candidate_union(
        departments,
        _evidence("ED治療と脂肪吸引を実施しています。美容皮膚科ではレーザー治療も行います。"),
    )
    assert {"ED治療", "脂肪吸引"} <= result
    assert "下肢静脈瘤血管内治療" not in result


def test_hanzomon_gi_regression_keeps_gastroscopy_candidate():
    result = candidate_union(["消化器内科"], _evidence("上部消化管内視鏡検査（胃カメラ）を実施"))
    assert "胃カメラ検査" in result


def test_sanno_cpap_regression_is_not_dropped_by_department_context():
    result = candidate_union(["内科", "皮膚科"], _evidence("睡眠時無呼吸症候群のCPAP治療に対応"))
    assert "CPAP療法" in result


def test_breast_implant_does_not_rescue_dental_implant():
    result = candidate_union(["美容外科"], _evidence("シリコンインプラントによる豊胸を行います"))
    assert "歯科インプラント" not in result


def test_breast_reconstruction_implant_does_not_rescue_dental_implant():
    result = candidate_union(["乳腺外科"], _evidence("乳房再建用インプラントに入れ替える手術を行います"))
    assert "歯科インプラント" not in result


def test_nerve_radiofrequency_does_not_rescue_varicose_treatment():
    result = candidate_union(["麻酔科"], _evidence("神経への高周波治療で痛みを和らげます"))
    assert "下肢静脈瘤血管内治療" not in result


def test_knee_osteotomy_does_not_rescue_facial_contouring():
    result = candidate_union(["整形外科"], _evidence("膝周囲骨切り術を実施します"))
    assert "輪郭骨切り術" not in result


def test_high_tibial_osteotomy_does_not_rescue_facial_contouring():
    result = candidate_union(["整形外科"], _evidence("脛骨高位骨切り術を実施します"))
    assert "輪郭骨切り術" not in result


def test_cgm_evidence_context_rule_is_not_reused_as_candidate_guard():
    result = candidate_union(["小児科"], _evidence("リブレ京成を通り過ぎてください"))
    assert "CGM・持続血糖モニタリング" in result


def test_kojimachi_regression_keeps_ed_candidate():
    result = candidate_union(["内科", "泌尿器科"], _evidence("ED治療を行っています"))
    assert "ED治療" in result


@pytest.mark.parametrize("clinic_name", ["山岡クリニック", "渡邊内科"])
def test_named_internal_medicine_regressions_keep_cpap_candidate(clinic_name):
    # clinic_name makes the two production regression fixtures independently visible.
    result = candidate_union(["内科"], _evidence(f"{clinic_name}ではCPAP治療を行っています"))
    assert "CPAP療法" in result
