"""A. 速度Baseline（ライブ）。Golden Dataset を現在のコードで通常実行し、時間・リクエスト数だけを数える。

HTTP応答本文の保存・ディスク書き込みは行わない（計測値に保存時間を含めないため）。
使い方: python scripts/perf/baseline_live.py [出力名]
"""
import collections
import json
import sys
import time
from urllib.parse import urlsplit

from perf_common import (ROOT, PERF_DIR, NoNetSession, copy_db, golden_ids, run_golden_job, percentile, db_state)
from src.enrichment import safe_web, hp_analysis, researcher as researcher_mod
from src.master import store as store_mod

TIMEOUT_SEC = 10          # SafeFetcher の既定 timeout
per = collections.defaultdict(lambda: collections.defaultdict(float))
reqs = collections.defaultdict(list)
class _ThreadState(__import__("threading").local):
    cid = None


state = _ThreadState()   # 並列時も医院ごとに正しく記録・再生するため、処理中の医院IDはスレッドごとに持つ


class MeteredTransport:
    def __init__(self, inner):
        self.inner = inner

    def get(self, url, timeout, max_bytes):
        cid = state.cid
        t = time.monotonic()
        rec = {"url": url, "status": None, "error": None}
        try:
            resp = self.inner.get(url, timeout, max_bytes)
            rec["status"] = resp.status
            rec["bytes"] = len(resp.body)
            return resp
        except Exception as exc:
            rec["error"] = str(exc)[:80]
            raise
        finally:
            rec["sec"] = time.monotonic() - t
            rec["timeout"] = bool(rec["error"]) and (rec["sec"] >= TIMEOUT_SEC * 0.95 or "時間上限" in (rec["error"] or ""))
            per[cid]["http_sec"] += rec["sec"]
            reqs[cid].append(rec)


_orig_init = safe_web.SafeFetcher.__init__


def _init(self, *a, **k):
    _orig_init(self, *a, **k)
    self.transport = MeteredTransport(self.transport)
    real_sleep = self.sleep

    def sleep(sec):
        per[state.cid]["rate_limit_sleep_sec"] += sec
        real_sleep(sec)
    self.sleep = sleep


safe_web.SafeFetcher.__init__ = _init

_orig_post = hp_analysis.Page.__post_init__


def _post(self):
    t = time.monotonic()
    _orig_post(self)
    per[state.cid]["page_parse_sec"] += time.monotonic() - t
    per[state.cid]["page_objects"] += 1


hp_analysis.Page.__post_init__ = _post

_orig_analyze = researcher_mod.analyze


def _analyze(record, pages, results=()):
    t = time.monotonic()
    try:
        return _orig_analyze(record, pages, results)
    finally:
        per[state.cid]["scoring_sec"] += time.monotonic() - t
        per[state.cid]["pages_analyzed"] = len(pages)


researcher_mod.analyze = _analyze

_orig_run = researcher_mod.Researcher.run


def _run(self, kind, record, force=False):
    state.cid = record.get("id")
    t = time.monotonic()
    try:
        return _orig_run(self, kind, record, force)
    finally:
        per[state.cid]["research_sec"] += time.monotonic() - t


researcher_mod.Researcher.run = _run

_orig_save = store_mod.ClinicStore.save_research


def _save(self, cid, result, pages=None):
    t = time.monotonic()
    try:
        return _orig_save(self, cid, result, pages)
    finally:
        per[state.cid]["db_write_sec"] += time.monotonic() - t


store_mod.ClinicStore.save_research = _save


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else time.strftime("baseline_live_%Y%m%d_%H%M%S")
    prod = ROOT / "data/clinics.sqlite3"
    before = db_state(prod)
    ids = golden_ids()
    db = copy_db(prod, f"{name}.sqlite3")
    store, jid, total, job = run_golden_job(db, ids)
    rows = []
    with store.connect() as c:
        names = dict(c.execute("SELECT id,clinic_name FROM clinics").fetchall())
        results = dict(c.execute("SELECT clinic_id,result FROM research_job_items WHERE job_id=?", (jid,)).fetchall())
    for cid in ids:
        m = per[cid]
        rq = reqs[cid]
        urls = [r["url"] for r in rq]
        non_robots = [u for u in urls if not u.endswith("/robots.txt")]
        https_fallback = 0
        for i, r in enumerate(rq[:-1]):
            nxt = rq[i + 1]
            a, b = urlsplit(r["url"]), urlsplit(nxt["url"])
            if a.scheme == "https" and b.scheme == "http" and a.hostname == b.hostname and (r["error"] or (r["status"] or 0) >= 400):
                https_fallback += 1
        slowest = max(rq, key=lambda r: r["sec"]) if rq else {}
        total_sec = m["research_sec"] + m["db_write_sec"]
        other = total_sec - m["http_sec"] - m["rate_limit_sleep_sec"] - m["page_parse_sec"] - m["scoring_sec"] - m["db_write_sec"]
        rows.append({
            "clinic_id": cid, "clinic_name": names.get(cid), "result": results.get(cid),
            "total_sec": round(total_sec, 3), "http_sec": round(m["http_sec"], 3),
            "rate_limit_sleep_sec": round(m["rate_limit_sleep_sec"], 3), "page_parse_sec": round(m["page_parse_sec"], 3),
            "scoring_sec": round(m["scoring_sec"], 3), "db_write_sec": round(m["db_write_sec"], 3), "other_sec": round(other, 3),
            "requests": len(rq), "robots_requests": len(urls) - len(non_robots), "unique_urls": len(set(urls)),
            "duplicate_requests": len(urls) - len(set(urls)),
            "duplicate_page_requests": len(non_robots) - len(set(non_robots)),
            "redirects": sum(1 for r in rq if r["status"] in {301, 302, 303, 307, 308}),
            "errors": sum(1 for r in rq if r["error"] or (r["status"] and r["status"] >= 400)),
            "timeouts": sum(1 for r in rq if r["timeout"]), "https_to_http_retries": https_fallback,
            "pages_analyzed": int(m["pages_analyzed"]), "page_objects": int(m["page_objects"]),
            "slowest_url": slowest.get("url"), "slowest_url_sec": round(slowest.get("sec", 0), 3),
            "duplicate_urls": sorted({u for u in non_robots if non_robots.count(u) > 1}),
        })
    secs = [r["total_sec"] for r in rows]
    summary = {
        "name": name, "golden_clinics": len(ids), "job_status": job["status"], "results": job["results"],
        "search_count": job["search_count"], "tavily_posts": NoNetSession.posts,
        "total_duration_sec": round(total, 1), "mean_sec": round(sum(secs) / len(secs), 2),
        "p50_sec": round(percentile(secs, 50), 2), "p95_sec": round(percentile(secs, 95), 2),
        "min_sec": round(min(secs), 2), "max_sec": round(max(secs), 2),
        "requests": sum(r["requests"] for r in rows), "unique_urls": sum(r["unique_urls"] for r in rows),
        "robots_requests": sum(r["robots_requests"] for r in rows),
        "duplicate_requests": sum(r["duplicate_requests"] for r in rows),
        "duplicate_page_requests": sum(r["duplicate_page_requests"] for r in rows),
        "redirects": sum(r["redirects"] for r in rows), "errors": sum(r["errors"] for r in rows),
        "timeouts": sum(r["timeouts"] for r in rows), "https_to_http_retries": sum(r["https_to_http_retries"] for r in rows),
        "breakdown_sec": {k: round(sum(r[k] for r in rows), 1) for k in
                          ["http_sec", "rate_limit_sleep_sec", "page_parse_sec", "scoring_sec", "db_write_sec", "other_sec"]},
        "page_objects": sum(r["page_objects"] for r in rows), "pages_analyzed": sum(r["pages_analyzed"] for r in rows),
        "prod_db_unchanged": db_state(prod) == before,
    }
    out = PERF_DIR / f"{name}.json"
    out.write_text(json.dumps({"summary": summary, "clinics": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
