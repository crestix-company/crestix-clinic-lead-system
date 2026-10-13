import csv
import io
from datetime import date

import pytest

from src.outreach.article_adapter import ArticleLink
from src.outreach.article_recommender import Article
from src.outreach.compose import (
    ARTICLE_LISTED, ARTICLE_NOT_LISTED, ARTICLE_UNCONFIRMED, CALLED_LINE, TEMPLATE_LISTED,
    TEMPLATE_RECOMMEND, Recipient, format_schedule, render_listed,
)
from src.outreach.email_template import (
    DraftInput, DraftValidationError, SIGNATURE, five_weekdays, render_draft,
)
from src.outreach.generation import (
    CSV_COLUMNS, FAILED, NEEDS_REVIEW, READY, GenerationSettings, generate_batch, summarize, to_csv_bytes,
)
from src.outreach.targets import ArticleState, ClinicTarget, classify_article, select_targets

ARTICLES = [
    Article("世田谷区のペインクリニックおすすめ10選", "https://clinic.mynavi.jp/article/setagaya_pain-clinic/", "世田谷 ペインクリニック", "1"),
    Article("世田谷区の内科クリニックおすすめ10選", "https://clinic.mynavi.jp/article/setagaya_internal/", "世田谷 内科", "2"),
    Article("東京都の内科クリニックおすすめ10選", "https://clinic.mynavi.jp/article/tokyo_internal/", "東京 内科", "3"),
    Article("世田谷区の内視鏡検査におすすめのクリニック10選", "https://clinic.mynavi.jp/article/setagaya_endoscopy/", "世田谷 内視鏡検査", "4"),
    Article("札幌市の眼科クリニックおすすめ10選", "https://clinic.mynavi.jp/article/sapporo_ophthalmology/", "札幌 眼科", "5"),
]
URLS = {a.url for a in ARTICLES}
DATES = five_weekdays(date(2026, 10, 19))
LISTED_URL = "https://clinic.mynavi.jp/article/setagaya_pain-clinic/"


def target(uuid="uuid-001", article=ArticleState(ARTICLE_LISTED, LISTED_URL, LISTED_URL), **kw):
    values = dict(clinic_id=1, clinic_uuid=uuid, clinic_name="州会クリニック", prefecture="東京都",
                  municipality="世田谷区", departments=("内科",), treatments=(), hp_rank="A",
                  director_name="永井 一成", email="info@shukai.example.jp", email_status="FOUND", article=article)
    values.update(kw)
    return ClinicTarget(**values)


def settings(called=()):
    return GenerationSettings(staff="前田", dates=DATES, called_uuids=frozenset(called))


def run(t, called=()):
    return generate_batch([t], ARTICLES, settings(called))[0]


# --- 記事あり ---
def test_listed_uses_fixed_shukai_template_identical_to_v1():
    result = run(target(), called=("uuid-001",))
    assert result.status == READY and result.template == TEMPLATE_LISTED
    assert result.subject == "【お伝えしておりましたマイナビ記事について】株式会社Crestix 前田"
    legacy = render_draft(DraftInput(
        clinic_name="州会クリニック", recipient_role="院長", recipient_name="永井 一成", is_staff_name="前田",
        example_article_url=LISTED_URL, listing_url=LISTED_URL, listing_kind="VERIFIED_ARTICLE",
        listing_verified=True, called_today_and_told_to_email=True, director_attendance_requested=True,
        candidate_dates=DATES))
    assert result.body == legacy.body  # 共有済み固定テンプレートと一字一句一致


def test_listed_without_call_never_claims_phone_call():
    result = run(target())
    assert result.status == READY
    assert "お電話" not in result.body and "本日" not in result.body
    assert "掲載させていただいている" in result.body


def test_listed_url_missing_from_master_is_downgraded():
    bad = ArticleState(ARTICLE_LISTED, "https://clinic.mynavi.jp/article/not_in_master/", "")
    result = run(target(article=bad))
    assert result.status == NEEDS_REVIEW
    assert result.template == TEMPLATE_RECOMMEND
    assert "掲載いただいている" not in result.body and "not_in_master" not in result.body


# --- 記事なし ---
def test_not_listed_uses_recommend_template_without_listing_claim():
    result = run(target(article=ArticleState(ARTICLE_NOT_LISTED)))
    assert result.status == READY and result.template == TEMPLATE_RECOMMEND
    for phrase in ("掲載させていただいている", "掲載いただいている", "掲載データ", "お電話"):
        assert phrase not in result.body
    assert result.first.article.url == "https://clinic.mynavi.jp/article/setagaya_internal/"
    assert result.first.article.url in result.body
    assert 1 <= len(result.recommendations) <= 3


def test_treatment_match_is_first_recommendation():
    result = run(target(article=ArticleState(ARTICLE_NOT_LISTED), treatments=("内視鏡検査",)))
    assert result.first.article.url == "https://clinic.mynavi.jp/article/setagaya_endoscopy/"


def test_no_matching_article_fails_instead_of_inventing():
    result = run(target(article=ArticleState(ARTICLE_NOT_LISTED), departments=("皮膚科",)))
    assert result.status == FAILED and result.body == ""


def test_region_mismatch_needs_review():
    result = run(target(article=ArticleState(ARTICLE_NOT_LISTED), departments=("眼科",)))
    assert result.status == NEEDS_REVIEW
    assert "第一推奨記事が地域不一致" in result.reasons


# --- 記事未確認 ---
def test_unconfirmed_is_safe_and_needs_review():
    result = run(target(article=ArticleState(ARTICLE_UNCONFIRMED, reason="記事マスターとの紐付けなし")),
                 called=("uuid-001",))
    assert result.status == NEEDS_REVIEW
    assert result.template == TEMPLATE_RECOMMEND
    assert "掲載いただいている" not in result.body
    assert CALLED_LINE in result.body  # 電話事実が確認済みの医院のみ


# --- 共通 ---
def test_signature_and_asterisk_rules_in_every_body():
    for state in (ArticleState(ARTICLE_LISTED, LISTED_URL, LISTED_URL), ArticleState(ARTICLE_NOT_LISTED),
                  ArticleState(ARTICLE_UNCONFIRMED)):
        body = run(target(article=state)).body
        assert body.endswith(SIGNATURE)
        assert body.count("******************************************") == 2


def test_missing_email_and_director_need_review():
    result = run(target(email="", email_status="MISSING", director_name=""))
    assert result.status == NEEDS_REVIEW
    assert "院長先生" in result.body
    assert {"宛先メール未取得", "院長名未確認（院長先生宛てで生成）"} <= set(result.reasons)


def test_duplicate_uuid_fails():
    results = generate_batch([target(), target(clinic_id=2)], ARTICLES, settings())
    assert [r.status for r in results] == [FAILED, FAILED]


def test_schedule_inserted_and_validated():
    body = run(target()).body
    assert "10月19日（月）9:00～21:00" in body and "10月23日（金）9:00～21:00" in body
    with pytest.raises(DraftValidationError):
        format_schedule((date(2026, 10, 24),), "9:00", "21:00")  # 土曜
    assert run(target()).status == READY
    assert generate_batch([target()], ARTICLES, GenerationSettings(staff="", dates=DATES))[0].status == FAILED


def test_recipient_newline_rejected():
    with pytest.raises(DraftValidationError):
        render_listed(Recipient("医院\n名"), staff="前田", example_url=LISTED_URL, listed_url=LISTED_URL,
                      schedule="x", called=False)


def test_csv_contains_identity_article_mail_and_status():
    results = generate_batch([target(), target("uuid-002", article=ArticleState(ARTICLE_NOT_LISTED))],
                             ARTICLES, settings())
    rows = list(csv.DictReader(io.StringIO(to_csv_bytes(results, "前田").decode("utf-8-sig"))))
    assert list(rows[0]) == CSV_COLUMNS
    assert rows[0]["clinic_uuid"] == "uuid-001" and rows[0]["status"] == READY
    assert rows[1]["recommended_url"] in URLS and rows[1]["article_status"] == "記事なし"
    assert summarize(results) == {"total": 2, READY: 2, NEEDS_REVIEW: 0, FAILED: 0}


# --- 判定・抽出 ---
def link(uuid, status="LISTED", verified=True):
    return ArticleLink(uuid, status, LISTED_URL, LISTED_URL, "VERIFIED_ARTICLE", "1", "1", verified)


def test_classify_article_states():
    assert classify_article("u1", [link("u1")]).status == ARTICLE_LISTED
    assert classify_article("u1", [link("u1", "NOT_LISTED")]).status == ARTICLE_NOT_LISTED
    assert classify_article("u1", [link("u1", verified=False)]).status == ARTICLE_UNCONFIRMED
    assert classify_article("u1", []).status == ARTICLE_UNCONFIRMED
    assert classify_article("u1", [], join_is_exhaustive=True).status == ARTICLE_NOT_LISTED
    assert classify_article("u1", [link("u1"), link("u1")]).status == ARTICLE_UNCONFIRMED


def record(i, uuid=None, **kw):
    base = {"id": i, "uuid": f"uuid-{i:03d}" if uuid is None else uuid, "clinic_name": f"テスト医院{i}",
            "prefecture": "東京都", "address": "東京都世田谷区三軒茶屋1-1-1", "normalized_departments": ["内科"],
            "manager_name": "山田 太郎", "effective_hp_rank": "A"}
    base.update(kw)
    return base


def test_select_targets_dedupes_skips_and_caps():
    records = [record(1), record(2, uuid="uuid-001"), record(3, uuid="")] + [record(i) for i in range(4, 70)]
    sel = select_targets(records, email_lookup=lambda ids: {1: "a@example.jp"}, links=[link("uuid-001")])
    assert len(sel.targets) == 50 and sel.truncated
    assert sel.skipped_duplicate == 1 and sel.skipped_no_uuid == 1
    assert len({t.clinic_uuid for t in sel.targets}) == 50
    first = sel.targets[0]
    assert first.municipality == "世田谷区" and first.email_status == "FOUND"
    assert first.article.status == ARTICLE_LISTED


def test_select_targets_email_and_article_filters():
    records = [record(i) for i in range(1, 6)]
    sel = select_targets(records, email_lookup=lambda ids: {2: "x@example.jp", 3: "bad"}, links=[link("uuid-004")],
                         email_filter="取得済み")
    assert [t.clinic_id for t in sel.targets] == [2]
    sel = select_targets(records, email_lookup=lambda ids: {}, links=[link("uuid-004")],
                         article_filter=(ARTICLE_LISTED,))
    assert [t.clinic_id for t in sel.targets] == [4]


def test_sidebar_has_single_mail_send_text_menu():
    import app_v2
    assert "メール送付文章" in app_v2.NAV
    assert "営業メール" not in app_v2.NAV
    assert app_v2.NAV.count("メール送付文章") == 1
    assert app_v2.NAV[:3] == ["かんたん操作", "営業対象・出力", "Email調査"] and app_v2.NAV[-1] == "詳細設定"
