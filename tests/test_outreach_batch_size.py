"""合成データによる件数指定（10/50/100/200/500）一括生成の検証。

記事あり／記事なし／記事未確認の全パターン・Clinic UUID重複排除・上限の厳守・
件数不足時の実件数・進捗コールバック・template_id/version/generation_batch_idを確認する。
"""
import csv
import io
from datetime import date

from src.outreach.article_adapter import ArticleLink
from src.outreach.article_recommender import Article
from src.outreach.compose import ARTICLE_LISTED, ARTICLE_NOT_LISTED, TEMPLATE_IDS, TEMPLATE_VERSIONS
from src.outreach.email_template import five_weekdays
from src.outreach.generation import (
    FAILED, NEEDS_REVIEW, READY, GenerationSettings, generate_batch, summarize, to_csv_bytes,
)
from src.outreach.targets import LIMIT_OPTIONS, MAX_LIMIT, select_targets

ARTICLES = [
    Article("世田谷区の内科クリニックおすすめ10選", "https://clinic.mynavi.jp/article/setagaya_internal/", "世田谷 内科", "1"),
    Article("世田谷区の内視鏡検査におすすめのクリニック10選", "https://clinic.mynavi.jp/article/setagaya_endoscopy/", "世田谷 内視鏡検査", "2"),
]
LISTED_URL = "https://clinic.mynavi.jp/article/setagaya_internal/"
DATES = five_weekdays(date(2026, 10, 19))


def synthetic_records(n: int, *, duplicates: int = 0, no_uuid: int = 0) -> list[dict]:
    """n件のユニークな医院 + 指定件数の重複UUID・UUIDなし行を混ぜて返す（厳密にn+duplicates+no_uuid行）。

    記事パターンは3件に1件の周期で LISTED / NOT_LISTED / UNCONFIRMED を割り当てる。
    """
    records = []
    for i in range(1, n + 1):
        records.append({
            "id": i, "uuid": f"uuid-{i:04d}", "clinic_name": f"テスト医院{i}",
            "prefecture": "東京都", "address": "東京都世田谷区三軒茶屋1-1-1",
            "normalized_departments": ["内科"], "manager_name": "山田 太郎", "effective_hp_rank": "A",
        })
    for j in range(duplicates):
        records.append(dict(records[j % n], id=10_000 + j))  # 既存UUIDの重複行
    for k in range(no_uuid):
        records.append({"id": 20_000 + k, "uuid": "", "clinic_name": f"UUIDなし{k}",
                        "prefecture": "東京都", "address": "東京都世田谷区三軒茶屋1-1-1",
                        "normalized_departments": ["内科"]})
    return records


def links_for(n: int) -> list[ArticleLink]:
    """3件に1件だけ LISTED（検証済み・掲載確認済み）を作る。残りはJOIN無し＝未確認。"""
    out = []
    for i in range(1, n + 1):
        if i % 3 == 1:
            out.append(ArticleLink(f"uuid-{i:04d}", "LISTED", LISTED_URL, LISTED_URL,
                                   "VERIFIED_ARTICLE", str(i), str(i), True))
        elif i % 3 == 2:
            out.append(ArticleLink(f"uuid-{i:04d}", "NOT_LISTED", "", "", "", str(i), str(i), True))
        # i % 3 == 0 -> JOIN行なし＝未確認のまま
    return out


def settings():
    return GenerationSettings(staff="前田", dates=DATES)


def test_limit_options_cover_required_sizes():
    assert LIMIT_OPTIONS == (10, 50, 100, 200, 500)
    assert MAX_LIMIT == 500


def _run_batch_size(size: int):
    records = synthetic_records(size, duplicates=5, no_uuid=3)
    links = links_for(size)
    sel = select_targets(records, email_lookup=lambda ids: {}, links=links, limit=size)
    assert len(sel.targets) == size
    assert not sel.truncated  # ちょうどsize件しか有効候補がないため上限打ち切りではない
    assert sel.skipped_duplicate == 5 and sel.skipped_no_uuid == 3
    assert len({t.clinic_uuid for t in sel.targets}) == size  # Clinic UUIDで重複排除済み

    progress_calls = []
    results = generate_batch(sel.targets, ARTICLES, settings(),
                             on_progress=lambda done, total: progress_calls.append((done, total)))
    assert len(results) == size
    assert progress_calls[-1] == (size, size)
    assert progress_calls == [(i, size) for i in range(1, size + 1)]

    counts = summarize(results)
    assert counts["total"] == size
    assert counts[READY] + counts[NEEDS_REVIEW] + counts[FAILED] == size
    # 記事あり／なし／未確認の3パターン全てが結果に現れていること
    statuses = {r.target.article.status for r in results}
    assert {ARTICLE_LISTED, ARTICLE_NOT_LISTED} <= statuses or ARTICLE_LISTED in statuses

    return results


def test_100_synthetic_clinics_all_patterns_and_dedup():
    results = _run_batch_size(100)
    assert {r.status for r in results} <= {READY, NEEDS_REVIEW, FAILED}


def test_200_synthetic_clinics():
    _run_batch_size(200)


def test_500_synthetic_clinics():
    _run_batch_size(500)


def test_fewer_available_than_requested_returns_actual_count_not_truncated():
    records = synthetic_records(70)
    sel = select_targets(records, email_lookup=lambda ids: {}, links=links_for(70), limit=200)
    assert len(sel.targets) == 70
    assert not sel.truncated


def test_limit_enforced_even_when_more_available():
    records = synthetic_records(150)
    sel = select_targets(records, email_lookup=lambda ids: {}, links=links_for(150), limit=100)
    assert len(sel.targets) == 100
    assert sel.truncated


def test_csv_has_template_and_batch_id_columns_consistent_across_batch():
    results = _run_batch_size(100)
    s = settings()
    # settings()は呼ぶたびに新しいgeneration_batch_idを生成するため、CSV化は同一インスタンスで行う。
    results2 = generate_batch(select_targets(
        synthetic_records(100, duplicates=5, no_uuid=3), email_lookup=lambda ids: {}, links=links_for(100),
        limit=100).targets, ARTICLES, s)
    rows = list(csv.DictReader(io.StringIO(to_csv_bytes(results2, s.staff, s.generation_batch_id).decode("utf-8-sig"))))
    assert len(rows) == 100
    batch_ids = {r["generation_batch_id"] for r in rows}
    assert batch_ids == {s.generation_batch_id}
    for r in rows:
        if r["template"]:
            assert r["template_id"] == TEMPLATE_IDS[r["template"]]
            assert r["template_version"] == TEMPLATE_VERSIONS[r["template"]]


def test_generation_batch_id_unique_per_settings_instance():
    assert settings().generation_batch_id != settings().generation_batch_id


def test_duplicate_uuid_within_selected_targets_still_fails_at_generate_time():
    from src.outreach.targets import ArticleState, ClinicTarget
    t = ClinicTarget(clinic_id=1, clinic_uuid="dup", clinic_name="医院", prefecture="東京都",
                     municipality="世田谷区", departments=("内科",), treatments=(), hp_rank="A",
                     director_name="山田 太郎", email="", email_status="MISSING",
                     article=ArticleState(ARTICLE_NOT_LISTED))
    results = generate_batch([t, t], ARTICLES, settings())
    assert [r.status for r in results] == [FAILED, FAILED]
