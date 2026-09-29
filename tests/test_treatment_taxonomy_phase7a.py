from collections import Counter
import sqlite3

import pytest

from src.enrichment.treatment_taxonomy import (
    CLINICAL_FOCUS_SCHEMA_SQL, LEGACY_RULE_VERSION, RULE_VERSION,
    TREATMENT_EVIDENCE_SCHEMA_SQL, active_treatment_category_names,
    department_category_view, evaluate_treatment_evidence, load_taxonomy,
    phase7b_research_categories, proposed_treatment_categories,
    treatment_keyword_view,
)
from src.master.sales_treatments import sales_treatment_master


def evidence(text, url="https://clinic.example/medical/", title="診療案内", source_type="OFFICIAL_HP"):
    return {"url": url, "text": text, "page_title": title, "source_type": source_type}


def test_taxonomy_v2_preserves_v1_and_has_nine_sales_categories():
    config = load_taxonomy()
    assert config["taxonomy_version"] == RULE_VERSION == "7A-v2"
    assert config["previous_taxonomy_version"] == LEGACY_RULE_VERSION == "7A-v1"
    assert len(config["crestix_sales_categories"]) == 9
    assert len(config["legacy_treatment_categories_v1"]) == 19
    assert len(phase7b_research_categories()) == 43
    assert len(proposed_treatment_categories()) == 13
    assert len(active_treatment_category_names()) == 43
    assert len(sales_treatment_master()) == 9
    assert sum(map(len, sales_treatment_master().values())) == 112
    assert Counter(x.support_status for v in sales_treatment_master().values() for x in v) == {
        "EXACT": 10, "ALIAS": 70, "MISSING": 31, "AMBIGUOUS": 1,
    }


def test_existing_aliases_and_requested_gi_and_vitreous_aliases_are_preserved():
    keywords = treatment_keyword_view()
    assert {"胃内視鏡", "上部消化管内視鏡", "上部内視鏡", "経鼻内視鏡", "経口内視鏡", "大腸カメラ"} <= set(keywords["内視鏡"])
    assert {"硝子体", "硝子体手術"} <= set(keywords["硝子体"])
    assert department_category_view()["眼科"] == ["白内障", "緑内障", "ICL", "オルソケラトロジー", "硝子体"]


def test_explicit_official_clinic_offer_confirms():
    result = evaluate_treatment_evidence("胃カメラ検査", [evidence("当院では胃カメラ検査を実施しています。")], clinic_id="c-1")
    assert result["status"] == "CONFIRMED"
    assert result["matched_alias"] == "胃カメラ"
    assert result["rule_version"] == "7A-v2"
    assert result["evidence_text"] and result["evidence_url"]


@pytest.mark.parametrize("sentence", [
    "当院では胃カメラ検査を行っていません。",
    "胃カメラをご希望の方は他院へ紹介します。",
])
def test_negative_and_referral_evidence_never_confirms(sentence):
    result = evaluate_treatment_evidence("胃カメラ検査", [evidence(sentence)])
    assert result["status"] != "CONFIRMED"


def test_other_provider_and_old_article_are_review_not_confirmation():
    other = evaluate_treatment_evidence("胃カメラ検査", [evidence("他院では胃カメラ検査を行っています。")])
    old_article = evaluate_treatment_evidence("胃カメラ検査", [evidence(
        "当院では胃カメラ検査を実施しています。", "https://clinic.example/blog/old.html", "過去のお知らせ" )])
    assert other["status"] == "REVIEW"
    assert old_article["status"] == "REVIEW"


def test_general_medical_education_is_review_not_confirmation():
    result = evaluate_treatment_evidence("胃カメラ検査", [evidence("胃カメラとは、胃の内部を観察する検査です。", title="胃カメラとは")])
    assert result["status"] == "REVIEW"


def test_aliases_normalize_to_same_formal_treatment_category():
    result = evaluate_treatment_evidence("胃カメラ検査", [evidence("当院では上部内視鏡検査を行っています。")])
    assert result["status"] == "CONFIRMED"
    assert result["treatment_category"] == "胃カメラ検査"
    assert result["matched_alias"] == "上部内視鏡"


def test_ascii_alias_substring_is_not_a_hit():
    result = evaluate_treatment_evidence("ED治療", [evidence("Our clinic is SCHEDULED to open soon.")])
    assert result["status"] == "NOT_CONFIRMED"


def test_diagnosis_and_department_are_not_evaluator_inputs():
    taxonomy = load_taxonomy()
    active_types = {v["item_type"] for v in phase7b_research_categories().values()}
    assert "DEPARTMENT" not in active_types
    assert "DISEASE" not in active_types
    assert "糖尿病" in taxonomy["clinical_focus"]
    assert "糖尿病" not in taxonomy["treatment_categories"]


def test_non_official_site_cannot_confirm():
    result = evaluate_treatment_evidence("胃カメラ検査", [evidence("当院では胃カメラ検査を実施しています。", "https://www.caloo.jp/clinic/123")])
    assert result["status"] == "NOT_CONFIRMED"


def test_evidence_schema_is_declared_and_validates_status(tmp_path):
    db = sqlite3.connect(tmp_path / "schema.db")
    db.execute(TREATMENT_EVIDENCE_SCHEMA_SQL)
    db.execute(CLINICAL_FOCUS_SCHEMA_SQL)
    db.execute("INSERT INTO clinic_treatment_evidence(clinic_id,treatment_category,status,evidence_text,evidence_url,rule_version) VALUES(?,?,?,?,?,?)",
               ("c1", "胃カメラ検査", "CONFIRMED", "当院では胃カメラを実施", "https://clinic.example/", RULE_VERSION))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO clinic_treatment_evidence(clinic_id,treatment_category,status,rule_version) VALUES(?,?,?,?)",
                   ("c2", "内視鏡", "INVALID", RULE_VERSION))
    db.close()


def test_phase_7b_sampling_is_design_only_and_targets_200_unique_clinics():
    sampling = load_taxonomy()["phase_7b_sampling"]
    assert sampling["target_unique_clinics"] == 270
    assert 200 <= sampling["target_unique_clinics"] <= 300
    assert sampling["per_active_treatment_category_minimum_candidate_rows"] >= 6
    assert sampling["likely_positive_candidate_rows_per_category"] > 0
    assert sampling["likely_negative_candidate_rows_per_category"] > 0
    assert sampling["human_audit_target"] > 0
    assert "http_requests_per_clinic" in sampling["metrics"]


def test_only_active_categories_can_be_researched_and_proposed_are_rejected():
    assert "硝子体手術" in proposed_treatment_categories()
    with pytest.raises(ValueError, match="not active"):
        evaluate_treatment_evidence("硝子体手術", [evidence("当院で硝子体手術を実施しています。")])


def test_beauty_active_candidates_are_high_sales_value_only():
    beauty = [v for v in phase7b_research_categories().values()
              if "美容整形外科" in v["crestix_departments"]]
    assert beauty
    assert all(v["sales_filter_value"] == "HIGH" for v in beauty)


def test_gastroscopy_and_colonoscopy_are_separate_categories():
    stomach = evaluate_treatment_evidence("胃カメラ検査", [evidence("当院では上部消化管内視鏡を実施しています。")])
    colon = evaluate_treatment_evidence("大腸カメラ検査", [evidence("当院では大腸内視鏡を実施しています。")])
    assert stomach["status"] == colon["status"] == "CONFIRMED"
    assert stomach["treatment_category"] != colon["treatment_category"]
    assert "胃カメラ" not in load_taxonomy()["treatment_categories"]["大腸カメラ検査"]["aliases"]


def test_cataract_disease_and_surgery_are_separate_and_day_surgery_is_deprecated():
    taxonomy = load_taxonomy()
    assert "白内障" in taxonomy["clinical_focus"]
    assert "白内障" not in taxonomy["treatment_categories"]
    assert taxonomy["treatment_categories"]["白内障手術"]["item_type"] == "SURGERY"
    assert "日帰り手術" not in taxonomy["treatment_categories"]
    assert taxonomy["deprecated_sales_items"]["日帰り手術"]["status"] == "DEPRECATED"


def test_112_item_action_totals_are_reconciled():
    import csv
    from pathlib import Path
    rows = list(csv.DictReader((Path(__file__).resolve().parents[1] / "phase7a1_taxonomy_review.csv").open(encoding="utf-8", newline="")))
    assert len(rows) == 112
    assert Counter(r["action"] for r in rows) == {
        "KEEP": 19, "RENAME": 30, "SPLIT": 3, "ALIAS": 14,
        "MOVE_TO_DISEASE": 9, "MOVE_TO_DEPARTMENT": 4, "PROPOSE": 29, "DROP": 4,
    }


def test_every_sales_item_has_a_v2_treatment_focus_department_or_deprecation_disposition():
    taxonomy = load_taxonomy()
    represented = set(taxonomy["deprecated_sales_items"]) | set(taxonomy["department_sales_items"])
    for definition in taxonomy["treatment_categories"].values():
        represented.update(definition.get("source_sales_items", ()))
    for definition in taxonomy["clinical_focus"].values():
        represented.update(definition.get("source_sales_items", ()))
    all_sales_items = {item["treatment"] for items in taxonomy["crestix_sales_categories"].values() for item in items}
    assert all_sales_items <= represented
