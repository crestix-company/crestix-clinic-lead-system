"""Synthetic 50-clinic Canary: exercises the full select -> generate -> audit path offline."""
from datetime import date

from src.outreach.article_adapter import ArticleLink
from src.outreach.article_recommender import Article
from src.outreach.canary import audit
from src.outreach.email_template import five_weekdays
from src.outreach.generation import GenerationSettings, OutreachResult, generate_batch
from src.outreach.targets import select_targets

CITIES = ["世田谷区", "渋谷区", "新宿区", "杉並区", "練馬区"]
SLUG = {"世田谷区": "setagaya", "渋谷区": "shibuya", "新宿区": "shinjuku", "杉並区": "suginami", "練馬区": "nerima"}
DEPTS = {"内科": "internal", "皮膚科": "dermatology", "眼科": "ophthalmology", "整形外科": "orthopedic"}
ARTICLES = [Article(f"{city}の{dept}クリニックおすすめ10選", f"https://clinic.mynavi.jp/article/{SLUG[city]}_{slug}/",
                    f"{city[:-1]} {dept}", str(n))
            for n, (city, (dept, slug)) in enumerate(((c, d) for c in CITIES for d in DEPTS.items()), 1)]
ARTICLES.append(Article("東京都の内視鏡検査におすすめのクリニック10選",
                        "https://clinic.mynavi.jp/article/tokyo_endoscopy/", "東京 内視鏡検査", "99"))


def records():
    out = []
    for i in range(1, 56):
        city = CITIES[i % 5]
        dept = list(DEPTS)[i % 4]
        out.append({"id": i, "uuid": f"0000{i:04d}-aaaa-bbbb-cccc-dddddddddddd" if i != 55 else "",
                    "clinic_name": f"カナリア{i}クリニック", "prefecture": "東京都", "address": f"東京都{city}1-{i}",
                    "normalized_departments": [dept], "manager_name": "" if i % 9 == 0 else f"医師 {i}",
                    "treatment_categories": ["内視鏡検査"] if i % 11 == 0 else [], "effective_hp_rank": "A"})
    out.append(dict(out[0], id=99))  # UUID重複
    return out


def test_synthetic_canary_50_passes_all_checks():
    recs = records()
    links = []
    for r in recs[:15]:  # 記事あり
        city, dept = CITIES[r["id"] % 5], r["normalized_departments"][0]
        url = f"https://clinic.mynavi.jp/article/{SLUG[city]}_{DEPTS[dept]}/"
        links.append(ArticleLink(r["uuid"], "LISTED", url, url, "VERIFIED_ARTICLE", str(r["id"]), str(r["id"]), True))
    for r in recs[15:30]:  # 記事なし
        links.append(ArticleLink(r["uuid"], "NOT_LISTED", "", "", "", str(r["id"]), "", True))
    emails = {r["id"]: f"clinic{r['id']}@example.jp" for r in recs if r["id"] % 7}
    sel = select_targets(recs, email_lookup=lambda ids: emails, links=links)
    assert len(sel.targets) == 50 and sel.skipped_no_uuid == 0
    settings = GenerationSettings(staff="前田", dates=five_weekdays(date(2026, 10, 19)),
                                  called_uuids=frozenset(t.clinic_uuid for t in sel.targets[::4]))
    results = generate_batch(sel.targets, ARTICLES, settings)
    report = audit(results, master_urls={a.url for a in ARTICLES},
                   source_uuids={r["id"]: r["uuid"] for r in recs}, emails=emails, settings=settings)
    assert report["passed"], report["issues"][:5]
    assert report["total"] == 50
    assert set(report["article_status_counts"]) == {"LISTED", "NOT_LISTED", "UNCONFIRMED"}
    assert report["status_counts"].get("NEEDS_REVIEW", 0) >= 20  # 未確認20件は全てNEEDS_REVIEW


def test_canary_audit_detects_fabricated_url_and_false_phone_claim():
    recs = records()[:1]
    sel = select_targets(recs, email_lookup=lambda ids: {1: "a@example.jp"},
                         links=[ArticleLink(recs[0]["uuid"], "NOT_LISTED", "", "", "", "1", "", True)])
    settings = GenerationSettings(staff="前田", dates=five_weekdays(date(2026, 10, 19)))
    good = generate_batch(sel.targets, ARTICLES, settings)[0]
    forged = OutreachResult(**{**good.__dict__, "body": good.body.replace(
        good.first.article.url, "https://clinic.mynavi.jp/article/fake/") + "\n本日お電話しました"})
    report = audit([forged], master_urls={a.url for a in ARTICLES}, source_uuids={1: recs[0]["uuid"]},
                   emails={1: "a@example.jp"}, settings=settings)
    assert {"article_url_in_master", "fact_phone_claim"} <= set(report["issue_counts"])
