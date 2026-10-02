import pytest

from src.enrichment.hp_analysis import Page
from src.enrichment.hybrid_treatment import (
    ClinicalFocusResult,
    HYBRID_RULE_VERSION,
    clinical_focus_signals,
    evaluate_hybrid_treatments,
    project_hybrid_status,
)


def run(*pages, name="テストクリニック", departments=()):
    results, focuses = evaluate_hybrid_treatments(
        clinic_id=1, clinic_name=name, departments=departments, pages=list(pages),
        checked_at="2026-10-02T00:00:00Z",
    )
    return {r.treatment_category: r for r in results}, focuses


def home(body):
    return Page("https://clinic.example/", f"<title>テストクリニック</title><h1>テストクリニック</h1>{body}")


def test_result_version_and_legacy_projection():
    results, _ = run(name="東京ICLクリニック")
    result = results["ICL手術"]
    assert (result.hybrid_status, result.signal_source, result.signal_rank) == ("CONFIRMED", "CLINIC_NAME", "S")
    assert result.hybrid_rule_version == HYBRID_RULE_VERSION
    assert {s: project_hybrid_status(s) for s in ("CONFIRMED", "MENTIONED", "REVIEW", "NOT_CONFIRMED", "CANDIDATE_ONLY")} == {
        "CONFIRMED": "CONFIRMED", "MENTIONED": "REVIEW", "REVIEW": "REVIEW",
        "NOT_CONFIRMED": "NOT_CONFIRMED", "CANDIDATE_ONLY": "NOT_CONFIRMED",
    }


def test_disease_clinic_name_is_focus_not_cgm_treatment():
    results, focuses = run(name="東京糖尿病クリニック")
    assert "CGM・持続血糖モニタリング" not in results
    assert any(f.clinical_focus == "糖尿病" and f.source_type == "CLINIC_NAME" for f in focuses)


@pytest.mark.parametrize("path,source", [("/", "HOME_MENU"), ("/medical/", "INTRO_MENU")])
def test_home_and_intro_menu_are_confirmed(path, source):
    page = Page(f"https://clinic.example{path}", "<title>診療案内</title><h1>診療内容</h1><a href='/gastroscopy/'>胃カメラ</a><a href='/colonoscopy/'>大腸カメラ</a>")
    results, _ = run(page)
    assert results["胃カメラ検査"].hybrid_status == "CONFIRMED"
    assert results["胃カメラ検査"].signal_source == source
    assert results["大腸カメラ検査"].hybrid_status == "CONFIRMED"


def test_directly_linked_dedicated_page_is_rank_a_confirmed():
    first = home("<a href='/gastroscopy/'>胃カメラ</a>")
    detail = Page("https://clinic.example/gastroscopy/", "<title>胃カメラ検査</title><h1>胃カメラ検査</h1><p>検査の流れ</p>")
    results, _ = run(first, detail)
    # Rank S menu remains the winning signal; dedicated evidence is independently
    # testable by removing the menu result through direct internal evidence ordering.
    assert results["胃カメラ検査"].hybrid_status == "CONFIRMED"
    assert results["胃カメラ検査"].signal_rank == "S"
    signals, _ = evaluate_hybrid_treatments(
        clinic_id=1, clinic_name="テストクリニック", pages=[first, detail],
        include_supporting_signals=True,
    )
    dedicated = [r for r in signals if r.treatment_category == "胃カメラ検査" and r.signal_source == "DEDICATED_PAGE"]
    assert len(dedicated) == 1
    assert (dedicated[0].signal_rank, dedicated[0].hybrid_status) == ("A", "CONFIRMED")


def test_plain_official_text_concrete_alias_is_mentioned():
    results, _ = run(home("<main><p>胃カメラ検査についてご案内します。</p></main>"))
    assert results["胃カメラ検査"].hybrid_status == "MENTIONED"
    assert results["胃カメラ検査"].signal_rank == "C"


def test_unverified_identity_cannot_be_mentioned():
    page = home("<main><p>胃カメラ検査についてご案内します。</p></main>")
    results, _ = evaluate_hybrid_treatments(clinic_id=1, clinic_name="テストクリニック", pages=[page], identity_verified=False)
    result = {r.treatment_category: r for r in results}["胃カメラ検査"]
    assert (result.hybrid_status, result.signal_rank) == ("REVIEW", "X")


@pytest.mark.parametrize("departments,label,category", [
    (("乳腺外科",), "乳房再建インプラント", "歯科インプラント"),
    (("麻酔科",), "神経への高周波治療", "下肢静脈瘤血管内治療"),
    (("整形外科",), "膝周囲骨切り", "輪郭骨切り術"),
])
def test_false_positive_menu_labels_are_not_confirmed(departments, label, category):
    results, _ = run(home(f"<a href='/service/'>{label}</a>"), departments=departments)
    assert category not in results or results[category].hybrid_status != "CONFIRMED"


@pytest.mark.parametrize("html,expected", [
    ("<p>胃カメラとは一般的に胃を観察する検査です。</p>", "REVIEW"),
    ("<p>胃カメラ検査は他院で受けてください。</p>", "NOT_CONFIRMED"),
    ("<p>院長は前勤務先で胃カメラ検査を担当していました。</p>", "NOT_CONFIRMED"),
])
def test_safety_context_does_not_confirm(html, expected):
    results, _ = run(home(html))
    assert results["胃カメラ検査"].hybrid_status == expected


def test_article_does_not_confirm():
    article = Page("https://clinic.example/blog/gastroscopy/", "<title>ブログ</title><h1>胃カメラ</h1><p>胃カメラ検査について</p>")
    results, _ = run(article)
    assert results["胃カメラ検査"].hybrid_status == "REVIEW"


@pytest.mark.parametrize("departments,text,category", [
    (("美容外科",), "シリコンインプラント豊胸", "歯科インプラント"),
    (("乳腺外科",), "乳房再建インプラント", "歯科インプラント"),
    (("麻酔科",), "神経への高周波治療", "下肢静脈瘤血管内治療"),
    (("整形外科",), "膝周囲骨切り", "輪郭骨切り術"),
    (("整形外科",), "脛骨高位骨切り", "輪郭骨切り術"),
    (("小児科",), "リブレ京成を通り過ぎてください", "CGM・持続血糖モニタリング"),
])
def test_known_false_positives_never_become_treatment_signal(departments, text, category):
    results, _ = run(home(f"<p>{text}</p>"), departments=departments)
    assert category not in results or results[category].hybrid_status in {"CANDIDATE_ONLY", "REVIEW", "NOT_CONFIRMED"}


@pytest.mark.parametrize("name,departments,text,category", [
    ("飯田橋クリニック", ("皮膚科",), "当院ではED治療を行っています", "ED治療"),
    ("飯田橋クリニック", ("皮膚科",), "当院では脂肪吸引を行っています", "脂肪吸引"),
    ("麹町クリニック", ("内科",), "当院ではED治療を行っています", "ED治療"),
    ("半蔵門胃腸クリニック", ("消化器内科",), "当院では胃カメラ検査を行っています", "胃カメラ検査"),
    ("山王クリニック", ("内科",), "当院ではCPAP療法を行っています", "CPAP療法"),
    ("山岡クリニック", ("内科",), "当院ではCPAP療法を行っています", "CPAP療法"),
    ("渡邊内科", ("内科",), "当院ではCPAP療法を行っています", "CPAP療法"),
])
def test_legitimate_regressions_stay_confirmed(name, departments, text, category):
    results, _ = run(home(f"<p>{text}</p>"), name=name, departments=departments)
    assert results[category].hybrid_status == "CONFIRMED"


def test_department_alone_is_candidate_only_and_focus_is_separate():
    results, focuses = run(departments=("眼科",))
    assert results
    assert all(r.hybrid_status == "CANDIDATE_ONLY" for r in results.values())
    assert any(f.clinical_focus == "眼科" for f in focuses)


def test_explicit_suspension_hard_vetoes_home_menu_confirmation():
    first = home("<a href='/gastroscopy/'>胃カメラ</a>")
    suspension = Page(
        "https://clinic.example/medical/",
        "<title>診療案内</title><h1>お知らせ</h1><p>現在、当院では胃カメラ検査を休止しています。</p>",
    )
    results, _ = run(first, suspension)
    result = results["胃カメラ検査"]
    assert (result.hybrid_status, result.signal_rank) == ("NOT_CONFIRMED", "X")
    assert result.exclusion_context == "CURRENTLY_SUSPENDED"


def test_blog_other_provider_mention_does_not_veto_home_menu_confirmation():
    first = home("<a href='/gastroscopy/'>胃カメラ</a>")
    blog = Page(
        "https://clinic.example/blog/endoscopy/",
        "<title>ブログ</title><h1>内視鏡のお話</h1><p>他院でも胃カメラ検査が行われています。</p>",
    )
    results, _ = run(first, blog)
    result = results["胃カメラ検査"]
    assert (result.hybrid_status, result.signal_source) == ("CONFIRMED", "HOME_MENU")


def test_official_hp_clinical_focus_is_separate_from_treatment():
    page = home("<main><p>当院では糖尿病診療に力を入れています。</p></main>")
    results, focuses = run(page)
    focus = next(f for f in focuses if f.clinical_focus == "糖尿病")
    assert (focus.source_type, focus.evidence_url, focus.confidence) == ("OFFICIAL_HP", page.url, .92)
    assert "CGM・持続血糖モニタリング" not in results
    assert "インスリン導入・療法" not in results


@pytest.mark.parametrize("text,expected", [
    ("当院は消化器内科を専門としています。", "消化器内科"),
    ("循環器疾患を中心に診療しています。", "循環器内科"),
])
def test_official_hp_departmental_focus(text, expected):
    _, focuses = run(home(f"<main><p>{text}</p></main>"))
    assert any(f.clinical_focus == expected and f.source_type == "OFFICIAL_HP" for f in focuses)


@pytest.mark.parametrize("url,title,text", [
    ("https://clinic.example/blog/diabetes/", "ブログ", "糖尿病診療に力を入れています"),
    ("https://clinic.example/doctor/", "院長経歴", "前勤務先では糖尿病診療に力を入れていました"),
    ("https://clinic.example/medical/", "診療案内", "他院は糖尿病診療に力を入れています"),
])
def test_excluded_context_does_not_create_official_hp_focus(url, title, text):
    page = Page(url, f"<title>{title}</title><h1>{title}</h1><p>{text}</p>")
    _, focuses = run(page)
    assert not any(f.clinical_focus == "糖尿病" and f.source_type == "OFFICIAL_HP" for f in focuses)


def test_clinical_focus_rejects_unknown_source_type():
    with pytest.raises(ValueError, match="invalid Clinical Focus source type"):
        ClinicalFocusResult(1, "糖尿病", "BLOG", "糖尿病")
