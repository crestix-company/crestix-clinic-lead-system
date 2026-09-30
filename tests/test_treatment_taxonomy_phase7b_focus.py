"""Regression tests for the Phase 7-B 'focus treatment' evidence engine improvements.

These reproduce the exact evidence_text found in the 270-clinic Pilot's known false
positives (baseline rule_version 7A-v2, evidence_engine before this change) and assert
the new context-aware checks in evaluate_treatment_evidence() now reject them, without
touching src/enrichment/treatment_taxonomy.py's existing Phase 7-A behavior
(see tests/test_treatment_taxonomy_phase7a.py, left unmodified and still green).
"""
from unittest.mock import patch

from src.enrichment.treatment_taxonomy import evaluate_treatment_evidence, load_taxonomy


def evidence(text, url="https://clinic.example/medical/", title="診療案内", source_type="OFFICIAL_HP"):
    return {"url": url, "text": text, "page_title": title, "source_type": source_type}


def test_known_fp_iidabashi_eye_clinic_other_facility_within_group():
    sentence = ("白内障手術について 眼科手術について 慶翔会グループの 両国眼科 にて、"
                "当院の担当医が日帰り白内障手術を行います")
    result = evaluate_treatment_evidence(
        "白内障手術", [evidence(sentence, url="https://www.iidabashi-eye.com/")],
        clinic_id=83, clinic_name="医療法人社団　慶翔会　飯田橋眼科クリニック")
    assert result["status"] != "CONFIRMED"
    assert result["exclusion_context"] == "OTHER_CLINIC"
    assert result["reason"] == "OTHER_FACILITY_WITHIN_GROUP"


def test_known_fp_shibaura_skin_clinic_publication_and_doctor_profile_page():
    sentence = ("第57回日本形成外科学会総会・学術集会 ALT flap を用いた手指再建 "
                "2014 Chang Gung Mayo Clinic, The 5th Symposium in Reconstructive Surgery "
                "Best paper Award 豊胸")
    result = evaluate_treatment_evidence(
        "豊胸手術", [evidence(sentence, url="https://shibaura-skin.com/greeting.html", title="院長挨拶")],
        clinic_id=1285, clinic_name="医療法人社団　広進会　芝浦アイランド皮フ科")
    assert result["status"] != "CONFIRMED"
    assert result["exclusion_context"] in {"PUBLICATION", "DOCTOR_HISTORY"}
    assert result["page_type"] == "DOCTOR_PROFILE"


def test_known_fp_heiwajima_ladies_clinic_referral_for_ivf_alt_phrasing_with_hospital_suffix():
    """A live re-fetch found this page's phrasing uses "専門病院", not "専門クリニック" —
    the referral pattern must cover both suffixes, not just the one in the first evidence sample."""
    sentence = "人工授精・体外受精に関しては、専門病院へご紹介させていただきます"
    result = evaluate_treatment_evidence(
        "体外受精（IVF）", [evidence(sentence, url="https://www.heiwajima-ladies.com/menu/")],
        clinic_id=4553, clinic_name="医療法人社団　一生会　平和島レディースクリニック")
    assert result["status"] != "CONFIRMED"
    assert result["exclusion_context"] == "REFERRAL"


def test_known_fp_heiwajima_ladies_clinic_referral_for_ivf():
    sentence = ("不妊治療は体外受精が必要になった段階で、不妊専門クリニックへご紹介させていただきますが、"
                "排卵誘発の為の日々の注射や卵胞チェックのみを継続して当院で受けていただくことは可能です")
    result = evaluate_treatment_evidence(
        "体外受精（IVF）", [evidence(sentence, url="https://www.heiwajima-ladies.com/menu/")],
        clinic_id=4553, clinic_name="医療法人社団　一生会　平和島レディースクリニック")
    assert result["status"] != "CONFIRMED"
    assert result["exclusion_context"] == "REFERRAL"
    assert result["reason"] == "REFERRAL_TO_OTHER_PROVIDER"


def test_lively_clinic_style_own_menu_still_confirms_true_positive():
    """The fix must not overcorrect: a clinic's own treatment-menu listing still confirms."""
    result = evaluate_treatment_evidence(
        "脂肪吸引",
        [{"url": "https://lively.example/menu/", "page_title": "施術一覧",
          "blocks": [{"heading_path": ["施術一覧", "外科"],
                      "text": "当院では脂肪吸引を行っています。"}],
          "source_type": "OFFICIAL_HP"}],
        clinic_id=9001, clinic_name="LIVELY CLINIC")
    assert result["status"] == "CONFIRMED"
    assert result["exclusion_context"] == "NONE"
    assert result["page_type"] == "TREATMENT_MENU"


def test_currently_suspended_service_is_not_confirmed():
    result = evaluate_treatment_evidence(
        "ED治療", [evidence("当院のED治療は現在休診中のため受付を停止しております")])
    assert result["status"] != "CONFIRMED"
    assert result["exclusion_context"] == "CURRENTLY_SUSPENDED"


def test_broad_alias_only_hit_is_capped_at_review_without_specific_corroboration():
    fake_taxonomy = {
        "treatment_categories": {
            "内視鏡カテゴリA": {
                "status": "ACTIVE", "item_type": "EXAM", "sales_filter_value": "HIGH",
                "source_sales_items": ["x"], "crestix_departments": ["内科"],
                "aliases": ["内視鏡検査", "専用内視鏡Aスコープ"],
                "broad_aliases": ["内視鏡検査"],
            },
        },
    }
    with patch("src.enrichment.treatment_taxonomy.load_taxonomy", return_value=fake_taxonomy):
        broad_only = evaluate_treatment_evidence(
            "内視鏡カテゴリA", [evidence("当院では内視鏡検査を行っています。")])
        assert broad_only["status"] == "REVIEW"
        assert broad_only["reason"] == "BROAD_ALIAS_WITHOUT_SPECIFIC_CORROBORATION"

        specific = evaluate_treatment_evidence(
            "内視鏡カテゴリA", [evidence("当院では専用内視鏡Aスコープを行っています。")])
        assert specific["status"] == "CONFIRMED"


def test_existing_active_categories_have_no_broad_aliases_configured_yet():
    """Documents that the mechanism is a lever for future tuning, not applied today."""
    taxonomy = load_taxonomy()
    for name, definition in taxonomy["treatment_categories"].items():
        if definition["status"] == "ACTIVE":
            assert not definition.get("broad_aliases"), f"unexpected broad_aliases on {name}"
