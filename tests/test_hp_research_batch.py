"""HP再調査バッチ（Stage4 Canary）のfixtureテスト。
derive_results()はネットワーク不使用（合成Pageのみ）。select_candidates/upsert_resultは
テスト専用の一時sqlite3ファイルのみを使う（本番DB・既存Treatment sidecarには一切接続しない）。
"""
import json
import sqlite3

from src.enrichment.hp_analysis import Page
from src.master.hp_research_batch import (
    SCHEMA, connect_sidecar, derive_results, select_candidates, upsert_result, MAX_ATTEMPTS,
    fetch_clinic, process_one, validate_batch_url, InvalidBatchUrl,
)

RICH_HTML = """
<html><head>
<title>さくら眼科クリニック｜白内障・緑内障</title>
<meta name="description" content="さくら眼科クリニックは白内障・緑内障の日帰り手術に対応する眼科専門クリニックです。">
<meta property="og:title" content="さくら眼科クリニック">
<link rel="canonical" href="https://example-clinic.jp/">
<link rel="icon" href="/favicon.ico">
<script type="application/ld+json">{"@context":"https://schema.org"}</script>
</head><body>
<a href="https://line.me/R/ti/p/@example">LINE公式</a>
<a href="https://instagram.com/example_clinic">Instagram</a>
<a href="/reserve">Web予約</a>
<p>院長プロフィール：経歴紹介</p>
<img src="1.jpg"><img src="2.jpg"><img src="3.jpg">
<p>料金：手術費用 20万円（税込）</p>
<p>お問い合わせはこちら tel:03-1234-5678</p>
</body></html>
"""

SPARSE_HTML = """
<html><head><title>医院</title></head><body><p>診療時間のご案内のみ</p></body></html>
"""


def _pages(html, url="https://example-clinic.jp/"):
    return [Page(url, html)]


def test_derive_results_rich_site_produces_old_and_new_feats():
    record = {"clinic_name": "さくら眼科クリニック"}
    result = derive_results(_pages(RICH_HTML), record=record)
    feats = set(json.loads(result["feature_json"]))
    assert "LINE導線" in feats
    assert "has_ogp" in feats
    assert "has_structured_data" in feats
    assert result["treatment_status"] == "DONE"
    assert result["hp_abc_candidate"] in {"A", "B", "C", "D", "UNKNOWN"}


def test_derive_results_sparse_site_is_low_rank():
    result = derive_results(_pages(SPARSE_HTML), record={"clinic_name": "医院"})
    assert result["hp_abc_candidate"] in {"C", "D"}


def test_derive_results_treatment_never_touches_hp_abc_score():
    """同じHP特徴でもTreatmentカテゴリの有無でhp_abc_candidateが変わらないことを確認。"""
    r1 = derive_results(_pages(RICH_HTML), record={"clinic_name": "さくら眼科クリニック"})
    r2 = derive_results(_pages(RICH_HTML), record={"clinic_name": "さくら眼科クリニック", "departments": "内視鏡科"})
    assert r1["hp_abc_candidate"] == r2["hp_abc_candidate"]
    assert r1["hp_abc_score"] == r2["hp_abc_score"]


def test_validate_batch_url_rejects_text_and_non_http():
    for value in ["A Single-Blinded Prospective Study", "//paper title", "mailto:x@example.jp", "https://＃/"]:
        try:
            validate_batch_url(value)
        except InvalidBatchUrl:
            pass
        else:
            raise AssertionError(value)
    assert validate_batch_url("https://clinic.example.jp/path") == "https://clinic.example.jp/path"


def test_invalid_input_url_is_recorded_without_fetch():
    result = process_one(99, "A Single-Blinded Prospective Study", True)
    assert result["clinic_id"] == 99
    assert result["fetch_status"] == "INVALID_URL"
    assert result["treatment_status"] == "FETCH_FAILED"


def test_malformed_page_link_is_one_clinic_invalid_url_not_batch_exception():
    class Fetcher:
        def fetch(self, url):
            return Page(url, '<a href="//A Single-Blinded Prospective Study ：author">paper</a>')
    result = process_one(100, "https://clinic.example.jp/", True, fetcher=Fetcher())
    assert result["fetch_status"] == "INVALID_URL"
    assert "取得ページ内" in result["error_detail"]


def _sidecar_path(tmp_path):
    return tmp_path / "batch_sidecar.sqlite3"


def test_upsert_is_idempotent_and_tracks_attempts(tmp_path):
    with connect_sidecar(_sidecar_path(tmp_path)) as conn:
        result = {
            "clinic_id": 1, "hp_url": "https://example.jp/", "fetch_status": "OK", "final_url": "https://example.jp/",
            "treatment_status": "DONE", "treatment_categories": "[]", "hp_abc_candidate": "C", "hp_abc_score": "pos=2,neg=0",
            "candidate_rank_1": "", "candidate_rank_2": "", "ambiguity_reason": "", "feature_json": "[]",
            "researched_at": "2026-10-05T00:00:00+00:00", "engine_version": "test", "error_detail": "", "elapsed_seconds": 1.0,
        }
        upsert_result(conn, result)
        row = conn.execute("SELECT * FROM hp_research_batch_results WHERE clinic_id=1").fetchone()
        assert row["attempts"] == 1

        upsert_result(conn, result)  # 同じclinic_idを再処理してもUPSERTでattemptsが増えるだけ
        rows = conn.execute("SELECT * FROM hp_research_batch_results").fetchall()
        assert len(rows) == 1  # clinic_id重複行が増えない(idempotent)
        assert rows[0]["attempts"] == 2


def test_upsert_caps_retries_at_max_attempts(tmp_path):
    with connect_sidecar(_sidecar_path(tmp_path)) as conn:
        result = {
            "clinic_id": 2, "hp_url": "https://bad.example/", "fetch_status": "ERROR", "final_url": "",
            "treatment_status": "FETCH_FAILED", "treatment_categories": "[]", "hp_abc_candidate": "", "hp_abc_score": "",
            "candidate_rank_1": "", "candidate_rank_2": "", "ambiguity_reason": "", "feature_json": "[]",
            "researched_at": "2026-10-05T00:00:00+00:00", "engine_version": "test", "error_detail": "timeout", "elapsed_seconds": 5.0,
        }
        for _ in range(MAX_ATTEMPTS):
            upsert_result(conn, result)
        row = conn.execute("SELECT attempts FROM hp_research_batch_results WHERE clinic_id=2").fetchone()
        assert row["attempts"] == MAX_ATTEMPTS


def test_select_candidates_excludes_already_succeeded_and_exhausted(tmp_path):
    prod_db = tmp_path / "clinics.sqlite3"
    con = sqlite3.connect(prod_db)
    con.execute("CREATE TABLE clinics(id INTEGER PRIMARY KEY, hp_url TEXT, maps_website_url TEXT)")
    con.executemany(
        "INSERT INTO clinics VALUES(?,?,?)",
        [
            (1, "https://a.example/", ""),   # hp_urlあり、未処理 -> 候補になる
            (2, "https://b.example/", ""),   # 後でOK済みにする -> 除外される
            (3, "https://c.example/", ""),   # 後でMAX_ATTEMPTS失敗済みにする -> 除外される
            (4, "", "https://maps-d.example/"),  # hp_urlなし(maps由来) -> tier4で含まれる
            (5, "", ""),                      # URLなし -> 対象外
        ],
    )
    con.commit()
    con.close()

    with connect_sidecar(_sidecar_path(tmp_path)) as sidecar_conn:
        base = {
            "hp_url": "", "final_url": "", "treatment_status": "", "treatment_categories": "[]",
            "hp_abc_candidate": "", "hp_abc_score": "", "candidate_rank_1": "", "candidate_rank_2": "",
            "ambiguity_reason": "", "feature_json": "[]", "researched_at": "2026-10-05T00:00:00+00:00",
            "engine_version": "test", "elapsed_seconds": 0.0,
        }
        upsert_result(sidecar_conn, {**base, "clinic_id": 2, "fetch_status": "OK", "error_detail": ""})
        for _ in range(3):
            upsert_result(sidecar_conn, {**base, "clinic_id": 3, "fetch_status": "ERROR", "error_detail": "x"})

        candidates = select_candidates(prod_db, sidecar_conn, limit=10)
    ids = [c["clinic_id"] for c in candidates]
    assert 2 not in ids  # 成功済みは二重処理しない
    assert 3 not in ids  # MAX_ATTEMPTS失敗済みは無限retryしない
    assert 5 not in ids  # URLが無い医院は対象外
    assert 1 in ids and 4 in ids
    # tier1(hp_urlあり)がtier4(hp_urlなし/maps由来)より先に来る
    assert ids.index(1) < ids.index(4)


def test_select_candidates_prioritizes_treatment_not_researched(tmp_path):
    prod_db = tmp_path / "clinics.sqlite3"
    con = sqlite3.connect(prod_db)
    con.execute("CREATE TABLE clinics(id INTEGER PRIMARY KEY, hp_url TEXT, maps_website_url TEXT)")
    con.executemany("INSERT INTO clinics VALUES(?,?,?)", [(10, "https://x.example/", ""), (11, "https://y.example/", "")])
    con.commit()
    con.close()

    treatment_sidecar = tmp_path / "treatment_research_final.sqlite3"
    tcon = sqlite3.connect(treatment_sidecar)
    tcon.execute("CREATE TABLE clinic_research_status(clinic_id INTEGER PRIMARY KEY, research_status TEXT)")
    tcon.execute("INSERT INTO clinic_research_status VALUES(10, 'DONE')")  # 10は調査済み、11は未調査
    tcon.commit()
    tcon.close()

    with connect_sidecar(_sidecar_path(tmp_path)) as sidecar_conn:
        candidates = select_candidates(prod_db, sidecar_conn, limit=10, treatment_sidecar_path=treatment_sidecar)
    ids = [c["clinic_id"] for c in candidates]
    assert ids.index(11) < ids.index(10)  # Treatment未調査(11)が調査済み(10)より優先される
