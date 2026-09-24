"""Performance Phase 共通処理（計測・記録・再生用。本番コードの判定ロジックは変更しない）。

- 本番DBは直接使わず、.perf-replay/ 配下の複製DBでHP調査ジョブを実行する。
- 計測は本番クラスの外側を包むだけ（monkeypatch）。判定結果に影響する処理は変更しない。
- .perf-replay/ はGit管理外。実医院のHTML・HTTP応答本文はコミットしない。
"""
import json
import os
import shutil
import sqlite3
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.enrichment import safe_web, researcher as researcher_mod  # noqa: E402
from src.enrichment.search_provider import TavilySearchProvider  # noqa: E402
from src.master.jobs import run_job, job_status  # noqa: E402
from src.master.store import ClinicStore  # noqa: E402
from src.master.filters import AD_COUNT_SQL  # noqa: E402

PERF_DIR = ROOT / ".perf-replay"
GOLDEN = ROOT / "scripts/perf/golden/golden_clinics.json"
EXPECTED = ROOT / "scripts/perf/golden/expected.json"
VOLATILE_KEYS = {"hp_checked_at"}   # 実行時刻。判定結果ではないため比較対象外


def golden_ids():
    ids = [c["clinic_id"] for c in json.loads(GOLDEN.read_text(encoding="utf-8"))["clinics"]]
    limit = os.environ.get("PERF_LIMIT")   # 動作確認用（正式計測では使わない）
    return ids[:int(limit)] if limit else ids


def copy_db(source, name):
    PERF_DIR.mkdir(exist_ok=True)
    dest = PERF_DIR / name
    for suffix in ("", "-wal", "-shm", ".research.lock"):
        Path(str(dest) + suffix).unlink(missing_ok=True)
    shutil.copy2(source, dest)
    return dest


class NoNetSession:
    """Tavily通信を遮断して回数だけ数える（Maps HPありの医院では0回が正）。"""
    posts = 0

    def post(self, *args, **kwargs):
        NoNetSession.posts += 1
        raise AssertionError("Tavily communication is not allowed")


def run_golden_job(db_path, ids):
    store = ClinicStore(db_path)
    jid = uuid.uuid4().hex
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    with store.connect() as c:
        c.execute("INSERT INTO research_jobs(id,kind,options_json,max_searches,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                  (jid, "hp", json.dumps({"force": True, "max_pages": 20}), 100, now, now))
        c.executemany("INSERT INTO research_job_items(job_id,clinic_id) VALUES(?,?)", [(jid, i) for i in ids])
    NoNetSession.posts = 0
    started = time.monotonic()
    run_job(store, jid, TavilySearchProvider("perf-dummy-key", NoNetSession()))
    return store, jid, time.monotonic() - started, job_status(store, jid)


def normalized_results(store, jid, ids):
    """Golden比較用。手動修正を反映した effective 値（store.get）と、ジョブの結果区分を保存する。"""
    out = {}
    with store.connect() as c:
        items = {r[0]: r[1] for r in c.execute("SELECT clinic_id,result FROM research_job_items WHERE job_id=?", (jid,))}
        ad_counts = dict(c.execute(f"SELECT id,{AD_COUNT_SQL} FROM clinics WHERE id IN ({','.join('?' for _ in ids)})", ids).fetchall())
    for cid in ids:
        r = store.get(cid)
        with store.connect() as c:
            raw = c.execute("SELECT result_json FROM research_results WHERE clinic_id=?", (cid,)).fetchone()
        result = {k: v for k, v in (json.loads(raw[0]) if raw else {}).items() if k not in VOLATILE_KEYS}
        out[str(cid)] = {
            "summary": {
                "job_result": items.get(cid),
                "research_status": r.get("research_status"),
                "hp_status": r.get("hp_status"),
                "hp_url": r.get("hp_url"),
                "maps_website_url": r.get("maps_website_url"),
                "hp_verified": r.get("hp_verified"),
                "hp_match_score": r.get("hp_match_score"),
                "treatment_categories": sorted(r.get("treatment_categories") or []),
                "marketing_signals": sorted(s["name"] for s in r.get("marketing_signals") or []),
                "ad_signal_count": ad_counts.get(cid),
                "hp_production_companies": sorted(r.get("hp_production_companies") or []),
                "doctor_name": r.get("doctor_name"),
                "age_probability_under_59": r.get("age_probability_under_59"),
                "graduation_year": r.get("graduation_year"),
                "hp_rank": r.get("hp_rank"),
                "hp_score": r.get("hp_score"),
                "manual_fields": sorted(r.get("manual_fields") or []),
            },
            "research_result": result,
        }
    return out


def percentile(values, p):
    s = sorted(values)
    if not s:
        return None
    k = (len(s) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def db_state(db_path):
    """本番DB保護確認用の指紋（UUID・Maps・手動修正・schema）。"""
    c = sqlite3.connect(db_path)
    return {
        "schema": c.execute("SELECT group_concat(sql,';') FROM (SELECT sql FROM sqlite_master ORDER BY name)").fetchone()[0],
        "uuid_maps": c.execute("SELECT group_concat(id||'|'||uuid||'|'||maps_presence_status||'|'||maps_profile_url||'|'||maps_website_url,'\n') FROM (SELECT * FROM clinics ORDER BY id)").fetchone()[0],
        "manual": c.execute("SELECT group_concat(clinic_id||'|'||field||'|'||value_json,'\n') FROM (SELECT * FROM manual_overrides ORDER BY clinic_id,field)").fetchone()[0],
    }
