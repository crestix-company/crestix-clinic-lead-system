"""Regression tests for the Phase 7-B v3 evidence-engine fixes (5 bugs found in v2 Human Audit).

Each test is grounded in either a literal spec example (section 12) or a real Pilot
row that misfired in v2. Existing behavior locked in by test_treatment_taxonomy_phase7a.py
and test_treatment_taxonomy_phase7b_focus.py must remain untouched (verified by running
those files unmodified alongside this one).
"""
from src.enrichment.treatment_context import classify_page_type, detect_exclusion_context
from src.enrichment.treatment_taxonomy import evaluate_treatment_evidence


def evidence(text, url="https://clinic.example/medical/", title="診療案内", source_type="OFFICIAL_HP"):
    return {"url": url, "text": text, "page_title": title, "source_type": source_type}


# --- Fix 1: PUBLICATION_PATTERN must not fire on a bare 学会 credential title ---

def test_publication_does_not_exclude_board_certification_title():
    assert detect_exclusion_context("内視鏡学会専門医2名が常勤しております") == "NONE"


def test_publication_still_excludes_real_conference_presentation():
    assert detect_exclusion_context("日本形成外科学会で発表しました") == "PUBLICATION"


def test_real_clinic_9991_case_now_confirms_instead_of_false_publication_exclusion():
    """v2 bug: 内視鏡学会専門医 in the sentence wrongly triggered PUBLICATION exclusion,
    hiding an explicit self-offer statement (行っております)."""
    text = "内視鏡学会専門医2名が常勤しており、上部消化管内視鏡検査及び下部消化管内視鏡検査を行っております"
    result = evaluate_treatment_evidence("大腸カメラ検査", [evidence(text, title="院内紹介")])
    assert result["status"] == "CONFIRMED"
    assert result["exclusion_context"] == "NONE"


# --- Fix 2: page_type must not mislabel facility-tour pages as DOCTOR_PROFILE ---

def test_facility_tour_page_is_not_doctor_profile():
    assert classify_page_type("https://x/about.html", "院内紹介 / 内視鏡検査", []) != "DOCTOR_PROFILE"


def test_director_bio_page_is_doctor_profile():
    assert classify_page_type("https://x/greeting.html", "院長紹介 / 経歴", []) == "DOCTOR_PROFILE"


def test_real_clinic_12784_case_is_no_longer_falsely_excluded_as_doctor_history():
    """v2 bug: page titled 院長紹介 forced DOCTOR_HISTORY exclusion on an unrelated
    facility-ambiance sentence that merely lists exam names."""
    result = evaluate_treatment_evidence(
        "胃カメラ検査",
        [{"url": "https://clinic.example/about.html", "page_title": "院長紹介 | 南平山の上クリニック",
          "blocks": [{"heading_path": ["院長紹介"],
                      "text": "胃内視鏡検査 レントゲン 心電図 樹々にかこまれたコテージの様な外観でリゾートに居る気分を味わっていただけるよう、待合室も落ち着ける雰囲気にしています"}]}])
    assert result["exclusion_context"] != "DOCTOR_HISTORY"


# --- Fix 3: REFERRAL generalization ---

def test_referral_with_no_connector():
    assert detect_exclusion_context("体外受精が必要な方は専門の病院へ紹介します") == "REFERRAL"


def test_referral_affiliated_medical_institution():
    assert detect_exclusion_context("提携医療機関をご紹介します") == "REFERRAL"


def test_referral_to_named_facility_without_hardcoding_the_name():
    assert detect_exclusion_context(
        "白内障の進行が見られた患者様は、真鍋クリニックへの紹介をさせていただきます",
        clinic_name="真愛眼科医院") == "REFERRAL"


def test_referral_to_a_different_named_facility_is_also_caught():
    """Confirms the rule is general (not hard-coded to 真鍋クリニック specifically)."""
    assert detect_exclusion_context(
        "当科では対応が難しい場合、山田眼科クリニックへ紹介しております",
        clinic_name="テスト眼科") == "REFERRAL"


# --- Fix 4: OFFER_CONTEXT expansion + hedge guard ---

def test_offer_context_recognizes_started_offering_phrasing():
    result = evaluate_treatment_evidence(
        "デュピクセント治療", [evidence("当院ではデュピクセント治療を開始しました。", title="診療案内")])
    assert result["status"] == "CONFIRMED"


def test_offer_context_recognizes_started_carrying_phrasing():
    result = evaluate_treatment_evidence(
        "デュピクセント治療", [evidence("デュピクセントの取り扱いを開始しました。", title="診療案内")])
    assert result["status"] == "CONFIRMED"


def test_hedge_future_plan_is_not_treated_as_provided():
    result = evaluate_treatment_evidence(
        "デュピクセント治療", [evidence("当院ではデュピクセントを今後導入予定です。", title="診療案内")])
    assert result["status"] != "CONFIRMED"
    assert result["provider_context"] != "PROVIDED"


# --- Fix 5: OVER_BROAD_ALIAS / negative-compound context ---

def test_broad_alias_laser_treatment_without_disease_context_stays_review():
    result = evaluate_treatment_evidence(
        "下肢静脈瘤血管内治療", [evidence("クリニック紹介 レーザー治療器 炭酸ガスレーザー", title="クリニック紹介")])
    assert result["status"] != "CONFIRMED"


def test_broad_alias_laser_treatment_with_disease_context_can_confirm():
    result = evaluate_treatment_evidence(
        "下肢静脈瘤血管内治療",
        [evidence("下肢静脈瘤に対するレーザー治療を行っています。", title="診療案内")])
    assert result["status"] == "CONFIRMED"


def test_negative_compound_post_surgery_mention_does_not_confirm_the_surgery():
    result = evaluate_treatment_evidence(
        "白内障手術", [evidence("白内障手術後の診察も行っております。", title="診療案内")])
    assert result["status"] != "CONFIRMED"
    assert result["exclusion_context"] == "NEGATIVE_COMPOUND"


def test_negative_compound_does_not_veto_a_separate_clean_mention_of_the_same_alias():
    """Regression: a real Pilot row (clinic 891) had 大腸ポリープ切除 listed as a service
    AND 大腸ポリープ切除後の注意事項 (post-op guidance) in the same nav dump. The bad
    occurrence must not disqualify the clean one."""
    from src.enrichment.treatment_context import is_negative_compound_alias_mention
    sentence = "日帰り大腸ポリープ切除を実施しています。大腸ポリープ切除後の注意事項はスタッフがご説明します。"
    assert is_negative_compound_alias_mention(sentence, "大腸ポリープ切除") is False


# --- Fix 6: CGM リブレ context-required alias ---

def test_cgm_libre_colloquial_name_hits_with_glucose_context():
    result = evaluate_treatment_evidence(
        "CGM・持続血糖モニタリング", [evidence("リブレを用いた血糖モニタリングを行っています。", title="糖尿病外来")])
    assert result["matched_alias"] == "リブレ"


def test_cgm_libre_without_diabetes_context_does_not_auto_confirm():
    result = evaluate_treatment_evidence(
        "CGM・持続血糖モニタリング", [evidence("リブレという製品を扱っています。", title="その他")])
    assert result["status"] != "CONFIRMED"
