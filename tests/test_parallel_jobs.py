"""Performance Phase 2: 医院単位の並列調査（同じサイトの医院は同じ処理がclinic_id順に調べる）。"""
import json
import threading
import time
import pandas as pd
import pytest
from src.enrichment import researcher as researcher_mod
from src.enrichment.hp_analysis import Page, host
from src.enrichment.safe_web import WebError
from src.master import jobs
from src.master.filters import Filters
from src.master.google_maps import MAPS_RESULT_HEADERS
from src.master.jobs import create_job, run_job, job_status, pause_job
from src.master.samples import sample_records
from src.master.store import ClinicStore

ALL = Filters(active_only=False, hp_only=False)
# (医院名, 公式HP)。shared.example と www.shared.example は同じサイトとして扱う。
SITES = [("青葉内科", "https://shared.example/aoba/"), ("紅葉眼科", "https://other.example/"),
         ("若草皮膚科", "https://www.shared.example/wakakusa/"), ("白樺クリニック", "https://third.example/"),
         ("桜台内科", "https://shared.example/sakura/"), ("楓眼科", "https://fourth.example/")]


def _records():
    base = sample_records()[0]
    return [{**base, "clinic_id": f"p-{i}", "clinic_name": name, "phone": f"03-3333-{i:04d}",
             "address": f"東京都千代田区並列町{i}-1-1"} for i, (name, _) in enumerate(SITES)]


def _store(tmp_path, name="p.db"):
    store = ClinicStore(tmp_path / name)
    recs = _records()
    store.import_master(recs)
    ids = {r["clinic_name"]: r["id"] for r in store.query(ALL, limit=100)}
    rows = []
    for rec, (name, url) in zip(recs, SITES):
        row = {h: "" for h in MAPS_RESULT_HEADERS}
        row.update({"internal_clinic_id": str(ids[name]), "medical_institution_number": rec["clinic_id"],
                    "source_clinic_name": name, "source_phone": rec["phone"], "source_address": rec["address"],
                    "source_prefecture": rec["prefecture"], "maps_match_status": "MAPS_MATCHED_WEBSITE",
                    "maps_match_method": "phone", "maps_website_url": url})
        rows.append(row)
    store.import_google_maps(pd.DataFrame(rows))
    return store, ids


class FakeFetcher:
    """医院ごとの架空HP。取得ごとに(サイト, 医院, 開始, 終了, スレッド)を記録する。"""
    log, lock = [], threading.Lock()
    delay, fail, on_fetch, slow = 0.02, set(), None, {}

    def fetch(self, url, allowed_host=None):
        site = host(url).removeprefix("www.")
        start = time.monotonic()
        time.sleep(FakeFetcher.delay + FakeFetcher.slow.get(site, 0))
        name, rec = next(((n, r) for (n, u), r in zip(SITES, _records()) if url.startswith(u)), (None, None))
        with FakeFetcher.lock:
            FakeFetcher.log.append((site, name, start, time.monotonic(), threading.get_ident()))
        if FakeFetcher.on_fetch:
            FakeFetcher.on_fetch()
        if name is None or name in FakeFetcher.fail:
            raise WebError("取得できません。")
        return Page(url, f"<title>{name}</title><h1>{name}</h1><p>{rec['phone']} {rec['address']}</p>"
                         f"<a href='https://www.instagram.com/{site}_{name}/'>Instagram</a>")


@pytest.fixture(autouse=True)
def fake_fetcher(monkeypatch):
    FakeFetcher.log, FakeFetcher.fail, FakeFetcher.on_fetch, FakeFetcher.delay, FakeFetcher.slow = [], set(), None, 0.02, {}
    monkeypatch.setattr(researcher_mod, "SafeFetcher", FakeFetcher)


def _results(store, ids):
    out = {}
    with store.connect() as c:
        for name, cid in ids.items():
            raw = c.execute("SELECT result_json FROM research_results WHERE clinic_id=?", (cid,)).fetchone()
            r = json.loads(raw[0]) if raw else {}
            r.pop("hp_checked_at", None)
            out[name] = r
    return out


def _run(tmp_path, workers, monkeypatch, name):
    monkeypatch.setattr(jobs, "PARALLEL_WORKERS", workers)
    store, ids = _store(tmp_path, name)
    jid = create_job(store, ALL, force=True)
    run_job(store, jid, provider=None)
    return store, ids, jid


def test_parallel_results_identical_to_sequential(tmp_path, monkeypatch):
    s1, ids1, j1 = _run(tmp_path, 1, monkeypatch, "seq.db")
    seq_log = list(FakeFetcher.log)
    FakeFetcher.log = []
    s2, ids2, j2 = _run(tmp_path, 2, monkeypatch, "par.db")
    assert job_status(s1, j1)["results"] == job_status(s2, j2)["results"] == {"SUCCESS": 6}
    assert _results(s1, ids1) == _results(s2, ids2)
    # サイトごとの取得順（医院・URLの並び）は順次処理と同じ
    by_site = lambda log: {s: [n for site, n, *_ in log if site == s] for s in {x[0] for x in log}}
    assert by_site(seq_log) == by_site(FakeFetcher.log)


def test_same_site_never_concurrent_and_in_clinic_order(tmp_path, monkeypatch):
    store, ids, jid = _run(tmp_path, 2, monkeypatch, "p.db")
    log = FakeFetcher.log
    shared = [x for x in log if x[0] == "shared.example"]
    assert [n for _, n, *_ in shared] == ["青葉内科", "若草皮膚科", "桜台内科"]   # clinic_id順
    assert len({t for *_, t in shared}) == 1                                    # 同じ処理が担当
    for a, b in zip(shared, shared[1:]):
        assert a[3] <= b[2]                                                      # 時間が重ならない
    # 別サイト同士は実際に並行して動いている
    overlaps = sum(1 for a in log for b in log if a[4] != b[4] and a[2] < b[3] and b[2] < a[3])
    assert overlaps > 0


def test_completed_only_after_all_items_done(tmp_path, monkeypatch):
    store, ids, jid = _run(tmp_path, 2, monkeypatch, "p.db")
    status = job_status(store, jid)
    assert status["status"] == "COMPLETED" and status["counts"] == {"DONE": 6}


def test_pause_keeps_every_item_and_resume_completes(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "PARALLEL_WORKERS", 2)
    store, ids = _store(tmp_path)
    jid = create_job(store, ALL, force=True)
    calls = []

    def pause_after_two():
        calls.append(1)
        if len(calls) == 2:
            pause_job(store, jid)
    FakeFetcher.on_fetch = pause_after_two
    run_job(store, jid, provider=None)
    status = job_status(store, jid)
    assert status["status"] == "PAUSED"
    assert "RUNNING" not in status["counts"]
    assert sum(status["counts"].values()) == 6 and status["counts"].get("PENDING", 0) >= 1
    FakeFetcher.on_fetch = None
    run_job(store, jid, provider=None)
    status = job_status(store, jid)
    assert status["status"] == "COMPLETED" and status["counts"] == {"DONE": 6}


def test_one_failing_site_does_not_stop_other_workers(tmp_path, monkeypatch):
    FakeFetcher.fail = {"紅葉眼科"}
    store, ids, jid = _run(tmp_path, 2, monkeypatch, "p.db")
    status = job_status(store, jid)
    assert status["status"] == "COMPLETED" and status["counts"] == {"DONE": 6}
    assert status["results"]["SUCCESS"] == 5 and sum(status["results"].values()) == 6


def test_explicit_fetcher_and_non_hp_jobs_stay_sequential(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "PARALLEL_WORKERS", 2)
    store, ids = _store(tmp_path)
    jid = create_job(store, ALL, force=True)
    run_job(store, jid, provider=None, fetcher=FakeFetcher())
    assert len({t for *_, t in FakeFetcher.log}) == 1
    assert job_status(store, jid)["status"] == "COMPLETED"


# ---- 別サイトへの到達（リダイレクト・検索結果）で並列の処理が同じサイトを共有しないこと ----
def test_cross_site_redirect_is_stopped_before_any_request_to_other_site():
    from src.enrichment.safe_web import SafeFetcher, WebResponse
    requested = []

    class Transport:
        def get(self, url, timeout, max_bytes):
            requested.append(host(url))
            if url.endswith("/robots.txt"):
                return WebResponse(404, {}, b"")
            return WebResponse(301, {"location": "https://shared.example/landing/"}, b"")
    fetcher = SafeFetcher(transport=Transport(), sleeper=lambda s: None)
    with pytest.raises(WebError, match="別ドメイン"):
        fetcher.fetch("https://other.example/")
    assert "shared.example" not in requested        # 別サイトへはrobots.txtも本体も取得しない
    assert set(requested) == {"other.example"}


def test_www_redirect_stays_in_same_lane():
    assert jobs._host_key("https://www.shared.example/x") == jobs._host_key("https://shared.example/")


def test_clinic_without_maps_url_runs_alone_after_parallel_lanes(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "PARALLEL_WORKERS", 2)
    store, ids = _store(tmp_path)
    extra = {**_records()[0], "clinic_id": "p-nomaps", "clinic_name": "検索内科", "phone": "03-3333-9999",
             "address": "東京都千代田区並列町99-1-1"}
    store.import_master([extra])
    SITES.append(("検索内科", "https://shared.example/kensaku/"))
    FakeFetcher.slow = {"shared.example": 0.2}   # 並列レーン（shared.example）が長く動いている状況
    try:
        class Provider:
            def search(self, query):
                return [{"url": "https://shared.example/kensaku/", "title": "検索内科", "content": extra["phone"]}]
        jid = create_job(store, ALL, force=True)
        run_job(store, jid, provider=Provider())
    finally:
        SITES.pop()
    log = FakeFetcher.log
    alone = [x for x in log if x[1] == "検索内科"]
    others = [x for x in log if x[1] != "検索内科"]
    assert alone and others
    # 検索で見つけた別サイト（並列レーンと同じ shared.example）は、並列の処理が全部終わってから単独で取得する
    assert min(a[2] for a in alone) >= max(o[3] for o in others)
    assert job_status(store, jid)["status"] == "COMPLETED"
