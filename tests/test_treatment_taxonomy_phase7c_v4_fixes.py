"""Regression tests for the Phase 7-C v4 Canary fixes (3 severe FP + 2 FN found in the
500-clinic Canary audit, root-caused to whole-block/whole-sentence exclusion evaluation
crossing unrelated topics in flattened nav/list HTML).

Each test is grounded in either a literal spec example (section 17) or a real Canary row
that misfired. Existing behavior locked in by test_treatment_taxonomy_phase7a.py,
test_treatment_taxonomy_phase7b_focus.py, and test_treatment_taxonomy_phase7b_v3_fixes.py
must remain untouched (verified by running those files unmodified alongside this one).
"""
from src.enrichment.hp_analysis import Page
from src.enrichment.treatment_context import (
    build_evidence_blocks,
    is_negative_compound_alias_mention,
)
from src.enrichment.treatment_taxonomy import evaluate_treatment_evidence


def evidence(text, url="https://clinic.example/medical/", title="診療案内", source_type="OFFICIAL_HP"):
    return {"url": url, "text": text, "page_title": title, "source_type": source_type}


def evidence_from_html(html, url="https://clinic.example/", title="診療案内"):
    page = Page(url, html)
    return {"url": url, "page_title": title, "blocks": build_evidence_blocks(page),
            "source_type": "OFFICIAL_HP"}


# --- Scope FN 1: an unrelated suspended item must not contaminate a genuine offer ---
# Real case: clinic 11767 (CPAP provided; a separate 禁煙治療 item is suspended).

def test_scope_fn_cpap_unaffected_by_unrelated_suspended_smoking_program():
    html = """
    <html><body>
    <h2>診療内容</h2>
    <ul>
      <li>CPAP療法によるSAS治療を実施しています（保険診療のみ）</li>
      <li>禁煙治療 休止中 機器メンテナンス中のため、現在休止しています</li>
    </ul>
    </body></html>
    """
    result = evaluate_treatment_evidence("CPAP療法", [evidence_from_html(html)])
    assert result["status"] == "CONFIRMED"
    assert "CPAP" in result["evidence_text"]


# --- Scope FN 2: a referral clause that targets a later, escalated exam must not veto
# the clinic's own preceding offer of the alias itself.
# Real case: clinic 3934 (胃カメラ provided; referral is for "さらに詳しい検査").

def test_scope_fn_gastroscope_unaffected_by_referral_for_a_different_followup_exam():
    text = "診察のうえ、必要に応じて腹部エコーや胃カメラ検査を行い、さらに詳しい検査が必要な場合は提携病院をご紹介します"
    result = evaluate_treatment_evidence("胃カメラ検査", [evidence(text)])
    assert result["status"] == "CONFIRMED"


def test_referral_that_targets_the_alias_itself_still_excludes():
    # Contrast case: here the alias itself (not a later exam) is what gets referred out,
    # so REFERRAL exclusion must still apply (no self-offer verb precedes the referral).
    text = "高度先進的な体外受精や顕微授精を必要とする場合は専門施設をご紹介しています"
    result = evaluate_treatment_evidence("体外受精（IVF）", [evidence(text)])
    assert result["status"] == "NOT_CONFIRMED"
    result2 = evaluate_treatment_evidence("顕微授精（ICSI）", [evidence(text)])
    assert result2["status"] == "NOT_CONFIRMED"


# --- Same-group referral must not confirm the clinic's own provision ---
# Real case: clinic 13131 (白内障手術 credited to a same-group clinic, not this one).

def test_same_group_referral_does_not_confirm_white_cataract_surgery():
    text = "同グループクリニックで白内障手術を受けられます"
    result = evaluate_treatment_evidence("白内障手術", [evidence(text)])
    assert result["status"] != "CONFIRMED"


def test_bare_group_affiliation_without_referral_action_is_not_itself_an_exclusion_trigger():
    # "当院は○○グループです" alone (a corporate description, no referral verb) must not
    # be treated as a same-group referral.
    from src.enrichment.treatment_context import GROUP_REFERRAL_PATTERN
    assert GROUP_REFERRAL_PATTERN.search("当院はさくら会グループです") is None


# --- NOT_OFFERED noun-phrase / heading-list style negation ---
# Real case: clinic 6046 (IVF and 胚移植 both listed under a "できないこと" heading).

def test_not_offered_heading_applies_section_wide_to_ivf():
    html = """
    <html><body>
    <h3>当院で実施困難なこと</h3>
    <ul><li>体外受精</li></ul>
    </body></html>
    """
    result = evaluate_treatment_evidence("体外受精（IVF）", [evidence_from_html(html)])
    assert result["status"] == "NOT_CONFIRMED"


def test_not_offered_heading_applies_section_wide_to_embryo_transfer():
    html = """
    <html><body>
    <h3>対応が難しい治療</h3>
    <ul><li>胚移植</li></ul>
    </body></html>
    """
    result = evaluate_treatment_evidence("胚移植", [evidence_from_html(html)])
    assert result["status"] == "NOT_CONFIRMED"


# --- OFFER_CONTEXT additions ---

def test_offer_context_recognizes_alternate_okurigana_for_行う():
    text = "胃カメラ検査を行なっております"
    result = evaluate_treatment_evidence("胃カメラ検査", [evidence(text)])
    assert result["status"] == "CONFIRMED"


def test_offer_context_recognizes_generalized_verb_plus_wo_okonaimasu():
    text = "採卵術を行います"
    result = evaluate_treatment_evidence("採卵", [evidence(text)])
    assert result["status"] == "CONFIRMED"


# --- GENERAL_CONTEXT additions must stay non-CONFIRMED ---

def test_general_context_recent_widespread_use_does_not_confirm():
    text = "近年はICLが広く利用されています"
    result = evaluate_treatment_evidence("ICL手術", [evidence(text)])
    assert result["status"] != "CONFIRMED"
    assert result["reason"] == "GENERAL_INFORMATION_CONTEXT"


# --- NEGATIVE_COMPOUND additions ---

def test_negative_compound_surgery_before_and_after_does_not_confirm():
    assert is_negative_compound_alias_mention("白内障手術前後の診察", "白内障手術") is True
    text = "白内障手術前後の診察"
    result = evaluate_treatment_evidence("白内障手術", [evidence(text)])
    assert result["status"] != "CONFIRMED"


def test_negative_compound_does_not_veto_a_separate_clean_mention_alongside_zengo():
    # A clean, separate offer mention must still confirm even if a "前後" compound also
    # appears elsewhere in the same block (blocks, not bare sentences, so DOM segmentation
    # keeps them in the same list — regression check for the ALL-occurrences rule).
    html = """
    <html><body>
    <ul>
      <li>白内障手術を行っております</li>
      <li>白内障手術前後の注意事項について</li>
    </ul>
    </body></html>
    """
    result = evaluate_treatment_evidence("白内障手術", [evidence_from_html(html)])
    assert result["status"] == "CONFIRMED"


# --- CMS placeholder text must be skipped entirely, not merely excluded ---

def test_cms_placeholder_text_is_not_used_as_evidence_at_all():
    text = "ここにICLの説明文を入力してください"
    result = evaluate_treatment_evidence("ICL手術", [evidence(text)])
    assert result["status"] == "NOT_CONFIRMED"
    assert result["reason"] == "NO_QUALIFYING_OFFICIAL_HP_EVIDENCE"
    assert result["evidence_text"] == ""


def test_cms_placeholder_variants_are_all_skipped():
    from src.enrichment.treatment_context import PLACEHOLDER_TEXT_PATTERN
    for sample in ("テキストを入力してください", "ダミーテキストです", "サンプルテキストが入ります",
                   "Lorem ipsum dolor sit amet"):
        assert PLACEHOLDER_TEXT_PATTERN.search(sample), sample
