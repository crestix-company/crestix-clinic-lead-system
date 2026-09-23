from pathlib import Path
import json
import threading
import time
import uuid
from collections import Counter

ROOT = Path.cwd()
DB = ROOT / "data" / "clinics.sqlite3"
STATE = ROOT / "data" / "v254_full_run_state_v2.json"

if not DB.exists():
    print("エラー：clinic-list-filter-complete フォルダで実行してください。")
    raise SystemExit(1)

from src.master.store import ClinicStore, now, dumps
from src.master.jobs import run_job, job_status
from src.enrichment.search_provider import TavilySearchProvider

store = ClinicStore(DB)
provider = TavilySearchProvider("")

with store.connect() as c:
    sample_job = c.execute("""
        SELECT j.id
        FROM research_jobs j
        JOIN research_job_items i ON i.job_id=j.id
        WHERE j.kind='hp'
        GROUP BY j.id
        HAVING COUNT(*)=50
        ORDER BY j.rowid DESC
        LIMIT 1
    """).fetchone()

sample_ids = set()
if sample_job:
    with store.connect() as c:
        sample_ids = {
            int(r[0]) for r in c.execute(
                "SELECT clinic_id FROM research_job_items WHERE job_id=?",
                (sample_job["id"],)
            )
        }

with store.connect() as c:
    all_ids = [
        int(r[0]) for r in c.execute("""
            SELECT id
            FROM clinics
            WHERE merged_into IS NULL
              AND merge_hold=0
              AND active=1
              AND prefecture='東京都'
              AND maps_presence_status='MAPS_MATCHED_WEBSITE'
              AND maps_website_url<>''
            ORDER BY id
        """)
    ]

state = {"logic": "v25.4-v2", "done_ids": [], "started_at": now()}
if STATE.exists():
    try:
        loaded = json.loads(STATE.read_text(encoding="utf-8"))
        if loaded.get("logic") == "v25.4-v2":
            state = loaded
    except Exception:
        pass

done_ids = {int(x) for x in state.get("done_ids", [])}
done_ids |= sample_ids
remaining = [cid for cid in all_ids if cid not in done_ids]

print("=== v25.4 本番HP調査（進捗修正版） ===")
print("東京都 Google Maps HP取得済み:", len(all_ids), "件")
print("v25.4確認済み50件:", len(sample_ids & set(all_ids)), "件")
print("本番で既に処理済み:", len((done_ids - sample_ids) & set(all_ids)), "件")
print("今回の残り:", len(remaining), "件")
print("Tavily検索: 0回")
print("100件ずつ処理し、各バッチ完了ごとに再開情報を保存します。")
print()

if not remaining:
    print("残りはありません。")
    raise SystemExit(0)

def save_state():
    STATE.parent.mkdir(parents=True, exist_ok=True)
    state["done_ids"] = sorted(done_ids - sample_ids)
    state["updated_at"] = now()
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE)

def run_batch(ids, batch_no, total_batches):
    jid = uuid.uuid4().hex
    with store.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            "INSERT INTO research_jobs(id,kind,options_json,max_searches,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (jid, "hp", dumps({"force": True, "max_pages": 20}), 0, now(), now())
        )
        c.executemany(
            "INSERT INTO research_job_items(job_id,clinic_id) VALUES(?,?)",
            [(jid, cid) for cid in ids]
        )

    print(f"--- バッチ {batch_no}/{total_batches}：{len(ids)}件 ---")
    holder = {"ok": None, "error": None}

    def worker():
        try:
            holder["ok"] = run_job(store, jid, provider)
        except Exception as exc:
            holder["error"] = repr(exc)

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    last_done = -1

    while th.is_alive():
        try:
            s = job_status(store, jid)
            done = s["counts"].get("DONE", 0)
            if done != last_done:
                print(f"進捗 {done}/{s['total']}件 | 状態={s['status']}")
                last_done = done
        except Exception:
            pass
        time.sleep(3)

    th.join()

    if holder["error"]:
        print("バッチ実行例外:", holder["error"])
        return False, jid
    if holder["ok"] is False:
        print("別の調査が実行中です。アプリを停止してから再実行してください。")
        return False, jid

    s = job_status(store, jid)
    print("完了:", s["status"])
    print("結果:", s["results"])
    print("Tavily検索:", s.get("search_count", 0), "回")
    return s["status"] == "COMPLETED", jid

batch_size = 100
batches = [remaining[i:i+batch_size] for i in range(0, len(remaining), batch_size)]
aggregate = Counter()

for idx, ids in enumerate(batches, 1):
    ok, jid = run_batch(ids, idx, len(batches))
    if not ok:
        save_state()
        print("ここで停止しました。問題解消後、同じコマンドを再実行してください。")
        raise SystemExit(1)

    done_ids.update(ids)
    save_state()

    try:
        s = job_status(store, jid)
        aggregate.update(s["results"])
    except Exception:
        pass

    print("v25.4処理済み:", len(done_ids & set(all_ids)), "/", len(all_ids), "件")
    print()

print("=== v25.4 本番調査 完了 ===")
print("対象:", len(all_ids), "件")
print("v25.4で処理済み:", len(done_ids & set(all_ids)), "件")
print("今回のバッチ集計:", dict(aggregate))
print("Tavily検索: 0回")
print("結果は data/clinics.sqlite3 に保存済みです。")
