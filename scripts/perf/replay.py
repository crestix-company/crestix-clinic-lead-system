"""B. Accuracy Replay Dataset（精度固定）。速度Baselineとは別実行。

  python scripts/perf/replay.py capture          # ライブ取得しながらHTTP応答を .perf-replay/replay/ に記録
  python scripts/perf/replay.py replay --write-expected   # 記録を再生し、期待値JSON(golden/expected.json)を作成
  python scripts/perf/replay.py replay           # 記録を再生し、期待値と完全一致するか確認（高速化の採用判定）
  python scripts/perf/replay.py replay --profile # 上記に加え、HTML解析回数・解析時間・判定時間を計測

記録するのは requested URL / HTTP status / final URL（location）/ content-type / 本文だけ。
Cookie・認証情報・送信ヘッダーは保存しない。記録はGit管理外（.perf-replay/）。
再生時は「今日」を記録日に固定し、取得間隔の待ち時間は省略する（判定結果には影響しない）。
"""
import collections
import datetime
import gzip
import json
import pickle
import sys
import time

from perf_common import (ROOT, PERF_DIR, EXPECTED, NoNetSession, copy_db, golden_ids, run_golden_job,
                         normalized_results, db_state)
from src.enrichment import safe_web, researcher as researcher_mod
from src.enrichment.safe_web import WebError, WebResponse
from src.scoring import age_estimator
from src.utils import date_utils
from src.master import store as store_mod

REPLAY_DIR = PERF_DIR / "replay"
SNAPSHOT = REPLAY_DIR / "db_snapshot.sqlite3"
RESPONSES = REPLAY_DIR / "responses.pkl.gz"
META = REPLAY_DIR / "meta.json"
KEEP_HEADERS = {"content-type", "location"}
class _ThreadState(__import__("threading").local):
    cid = None


state = _ThreadState()   # 並列時も医院ごとに正しく記録・再生するため、処理中の医院IDはスレッドごとに持つ
prof_lock = __import__("threading").Lock()
prof = {"bs_calls": collections.Counter(), "bs_sec": collections.Counter(), "clinic_sec": {}, "scoring_sec": 0.0, "page_post_init_sec": 0.0}


def _install_profiler():
    """BeautifulSoup生成回数・時間（呼び出し元関数別）、判定時間、医院別時間を計測する（判定には影響しない）。"""
    import bs4
    from src.enrichment import hp_analysis
    orig_bs = bs4.BeautifulSoup.__init__

    def bs_init(self, *a, **k):
        caller = sys._getframe(1)
        key = f"{caller.f_code.co_filename.rsplit('/src/', 1)[-1]}:{caller.f_code.co_name}"
        t = time.perf_counter()
        orig_bs(self, *a, **k)
        with prof_lock:
            prof["bs_calls"][key] += 1
            prof["bs_sec"][key] += time.perf_counter() - t
    bs4.BeautifulSoup.__init__ = bs_init
    orig_analyze = researcher_mod.analyze

    def analyze(record, pages, results=()):
        t = time.perf_counter()
        try:
            return orig_analyze(record, pages, results)
        finally:
            with prof_lock:
                prof["scoring_sec"] += time.perf_counter() - t
    researcher_mod.analyze = analyze
    orig_post = hp_analysis.Page.__post_init__

    def post(self):
        t = time.perf_counter()
        orig_post(self)
        with prof_lock:
            prof["page_post_init_sec"] += time.perf_counter() - t
    hp_analysis.Page.__post_init__ = post
    orig_run = researcher_mod.Researcher.run

    def run(self, kind, record, force=False):
        t = time.perf_counter()
        try:
            return orig_run(self, kind, record, force)
        finally:
            prof["clinic_sec"][record.get("id")] = time.perf_counter() - t
    researcher_mod.Researcher.run = run


def _profile_report():
    from perf_common import percentile
    secs = list(prof["clinic_sec"].values())
    return {"bs_parse_calls": sum(prof["bs_calls"].values()), "bs_parse_sec": round(sum(prof["bs_sec"].values()), 2),
            "scoring_sec": round(prof["scoring_sec"], 2), "page_post_init_sec": round(prof["page_post_init_sec"], 2),
            "clinic_total_sec": round(sum(secs), 2), "clinic_avg_sec": round(sum(secs) / len(secs), 3),
            "clinic_p50_sec": round(percentile(secs, 50), 3), "clinic_p95_sec": round(percentile(secs, 95), 3),
            "bs_calls_by_caller": dict(prof["bs_calls"].most_common()),
            "bs_sec_by_caller": {k: round(v, 2) for k, v in prof["bs_sec"].most_common()}}


def _track_clinic():
    orig = researcher_mod.Researcher.run

    def run(self, kind, record, force=False):
        state.cid = record.get("id")
        return orig(self, kind, record, force)
    researcher_mod.Researcher.run = run


def _install_transport(factory, no_sleep=False):
    orig = safe_web.SafeFetcher.__init__

    def init(self, *a, **k):
        orig(self, *a, **k)
        self.transport = factory(self.transport)
        if no_sleep:
            self.sleep = lambda sec: None
    safe_web.SafeFetcher.__init__ = init


class RecordingTransport:
    log = collections.defaultdict(list)

    def __init__(self, inner):
        self.inner = inner

    def get(self, url, timeout, max_bytes):
        key = (state.cid, url)
        try:
            resp = self.inner.get(url, timeout, max_bytes)
        except WebError as exc:
            RecordingTransport.log[key].append({"error": str(exc)})
            raise
        RecordingTransport.log[key].append({"status": resp.status,
                                            "headers": {k: v for k, v in resp.headers.items() if k in KEEP_HEADERS},
                                            "body": resp.body})
        return resp


class ReplayTransport:
    data = {}
    cursor = collections.Counter()
    misses = []
    lock = __import__("threading").Lock()

    def __init__(self, inner):
        pass

    def get(self, url, timeout, max_bytes):
        key = (state.cid, url)
        entries = ReplayTransport.data.get(key)
        with ReplayTransport.lock:
            if not entries:
                ReplayTransport.misses.append(key)
            else:
                i = min(ReplayTransport.cursor[key], len(entries) - 1)
                ReplayTransport.cursor[key] += 1
        if not entries:
            raise WebError(f"REPLAY_MISS {url}")
        e = entries[i]
        if "error" in e:
            raise WebError(e["error"])
        return WebResponse(e["status"], dict(e["headers"]), e["body"])


def _freeze_today(day):
    frozen = datetime.date.fromisoformat(day)
    for mod in (date_utils, age_estimator, store_mod):
        if hasattr(mod, "today_japan"):
            mod.today_japan = lambda: frozen


def capture():
    REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    prod = ROOT / "data/clinics.sqlite3"
    before = db_state(prod)
    copy_db(prod, "replay/db_snapshot.sqlite3")
    day = date_utils.today_japan().isoformat()
    _track_clinic()
    _install_transport(RecordingTransport)
    ids = golden_ids()
    db = copy_db(SNAPSHOT, "replay/capture_run.sqlite3")
    store, jid, total, job = run_golden_job(db, ids)
    with gzip.open(RESPONSES, "wb") as f:
        pickle.dump(dict(RecordingTransport.log), f)
    (REPLAY_DIR / "capture_outputs.json").write_text(
        json.dumps(normalized_results(store, jid, ids), ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    META.write_text(json.dumps({"captured_on": day, "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "clinics": len(ids),
                                "requests": sum(len(v) for v in RecordingTransport.log.values()),
                                "job": {"status": job["status"], "results": job["results"], "search_count": job["search_count"]},
                                "tavily_posts": NoNetSession.posts}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"captured": len(ids), "job": job["results"], "tavily_posts": NoNetSession.posts,
                      "prod_db_unchanged": db_state(prod) == before}, ensure_ascii=False))


def diff(expected, actual):
    """医院・項目単位の差分。配列は順序のみの違いを同一とみなす（欠落・増減は差分）。"""
    def norm(v):
        if isinstance(v, list):
            return sorted((norm(x) for x in v), key=lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True))
        if isinstance(v, dict):
            return {k: norm(x) for k, x in v.items()}
        return v
    out = []
    for cid in sorted(set(expected) | set(actual), key=int):
        e, a = expected.get(cid), actual.get(cid)
        if e is None or a is None:
            out.append((cid, "<clinic>", e is not None, a is not None))
            continue
        for part in ("summary", "research_result"):
            for k in sorted(set(e[part]) | set(a[part])):
                if norm(e[part].get(k)) != norm(a[part].get(k)):
                    out.append((cid, f"{part}.{k}", e[part].get(k), a[part].get(k)))
    return out


def replay(write_expected=False, profile=False):
    if profile:
        _install_profiler()
    meta = json.loads(META.read_text(encoding="utf-8"))
    with gzip.open(RESPONSES, "rb") as f:
        ReplayTransport.data = pickle.load(f)
    _freeze_today(meta["captured_on"])
    _track_clinic()
    _install_transport(ReplayTransport, no_sleep=True)
    ids = golden_ids()
    db = copy_db(SNAPSHOT, "replay/replay_run.sqlite3")
    store, jid, total, job = run_golden_job(db, ids)
    actual = normalized_results(store, jid, ids)
    report = {"replay_sec": round(total, 1), "job": job["results"], "tavily_posts": NoNetSession.posts,
              "replay_misses": len(ReplayTransport.misses)}
    if write_expected:
        capture_out = json.loads((REPLAY_DIR / "capture_outputs.json").read_text(encoding="utf-8"))
        fidelity = diff(capture_out, actual)
        report["capture_vs_replay_diffs"] = len(fidelity)
        report["capture_vs_replay_examples"] = [list(map(str, d))[:4] for d in fidelity[:10]]
        EXPECTED.write_text(json.dumps({"captured_on": meta["captured_on"], "clinics": actual},
                                       ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    else:
        expected = json.loads(EXPECTED.read_text(encoding="utf-8"))["clinics"]
        d = diff(expected, actual)
        e_status = collections.Counter(v["summary"]["job_result"] for v in expected.values())
        a_status = collections.Counter(v["summary"]["job_result"] for v in actual.values())
        report.update({"accuracy_diffs": len(d), "diff_examples": [list(map(str, x))[:4] for x in d[:20]],
                       "expected_status": dict(e_status), "actual_status": dict(a_status),
                       "accuracy_regression_zero": not d and not ReplayTransport.misses})
    report["requests_replayed"] = sum(ReplayTransport.cursor.values())
    report["request_pattern"] = sorted(f"{k[0]}|{k[1]}|{n}" for k, n in ReplayTransport.cursor.items())
    if profile:
        report["profile"] = _profile_report()
    pattern = report.pop("request_pattern")
    (PERF_DIR / "replay" / "last_request_pattern.json").write_text(json.dumps(pattern, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return report


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "capture":
        capture()
    elif cmd == "replay":
        replay("--write-expected" in sys.argv, "--profile" in sys.argv)
    else:
        print(__doc__)
