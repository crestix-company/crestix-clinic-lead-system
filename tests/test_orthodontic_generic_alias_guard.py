from src.enrichment.hp_analysis import Page
from src.enrichment.hybrid_treatment import evaluate_hybrid_treatments


def seikyo_results(clinic_id, clinic_name, departments, url, html):
    pages = [Page(url, html)]
    results, _ = evaluate_hybrid_treatments(
        clinic_id=clinic_id, clinic_name=clinic_name, departments=departments,
        pages=pages, checked_at="2026-10-04T00:00:00Z", identity_verified=True,
    )
    return [r for r in results if r.treatment_category == "歯列矯正"]


def test_dermatology_nail_correction_is_not_confirmed_as_orthodontics():
    """Phase 4-B false positive: clinic_id=5861 ひろみ皮フ科クリニック.

    "矯正治療" here refers to ingrown-nail (巻き爪) wire correction, a
    dermatology procedure, not dental orthodontics. Human review labeled
    this INCORRECT; the fix must suppress it going forward.
    """
    results = seikyo_results(
        5861, "ひろみ皮フ科クリニック", ("皮膚科",), "https://hiromi-clinic.jp/",
        "<html><body><p>難治性の陥入爪に対してはワイヤーを用いた矯正治療"
        "（VHO巻き爪矯正法）や手術療法（フェノール法含む）も行っております</p></body></html>",
    )
    assert not any(r.hybrid_status == "CONFIRMED" for r in results)


def test_ophthalmology_orthokeratology_is_not_confirmed_as_orthodontics():
    """Phase 4-B false positive: clinic_id=13029 国分寺さくら眼科.

    "視力矯正治療" is orthokeratology (vision correction contact lenses), an
    ophthalmology procedure, not dental orthodontics.
    """
    results = seikyo_results(
        13029, "国分寺さくら眼科", ("眼科",), "https://kokubunji.sakuraganka.jp/guide/",
        "<html><body><p>オルソケラトロジー処方 オルソケラトロジーとは、夜間就眠中のみ装用する"
        "特殊な形状をしたハードコンタクトレンズのことで、レンズを外した後もしばらく視力が回復して"
        "いるため、起床時に外して日中は裸眼で生活できる、という視力矯正治療です。</p></body></html>",
    )
    assert not any(r.hybrid_status == "CONFIRMED" for r in results)


def test_genuine_dental_orthodontics_is_still_confirmed():
    """The guard must not suppress real 歯列矯正 offers from dental clinics."""
    results = seikyo_results(
        99901, "テスト歯科矯正クリニック", ("歯科", "矯正歯科"), "https://example-dental.jp/",
        "<html><body><p>当院では歯列矯正治療を行っております。"
        "矯正歯科専門医による矯正治療です。</p></body></html>",
    )
    assert any(r.hybrid_status == "CONFIRMED" and r.matched_alias == "矯正治療" for r in results)
