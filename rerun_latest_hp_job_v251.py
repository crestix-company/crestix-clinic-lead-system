import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from src.master.store import ClinicStore, now, dumps
from src.master.jobs import run_job, job_status
from src.enrichment.search_provider import TavilySearchProvider

DB=Path('data')/'clinics.sqlite3'
store=ClinicStore(DB)

with store.connect() as c:
    prev=c.execute("""
        SELECT id,created_at,status FROM research_jobs
        WHERE kind='hp'
        ORDER BY rowid DESC LIMIT 1
    """).fetchone()
    if not prev:
        print('再調査元のHPジョブが見つかりません。')
        raise SystemExit(1)
    ids=[r[0] for r in c.execute(
        'SELECT clinic_id FROM research_job_items WHERE job_id=? ORDER BY rowid',(prev['id'],)
    )]

if not ids:
    print('再調査元ジョブに医院がありません。')
    raise SystemExit(1)

jid=uuid.uuid4().hex
with store.connect() as c:
    c.execute('BEGIN IMMEDIATE')
    c.execute(
        'INSERT INTO research_jobs(id,kind,options_json,max_searches,created_at,updated_at) VALUES(?,?,?,?,?,?)',
        (jid,'hp',dumps({'force':True,'max_pages':20}),0,now(),now())
    )
    c.executemany(
        'INSERT INTO research_job_items(job_id,clinic_id) VALUES(?,?)',
        [(jid,cid) for cid in ids]
    )

print('v25.1 再調査を開始します。')
print('元ジョブ:',prev['id'])
print('新ジョブ:',jid)
print('対象:',len(ids),'件')
print('Tavily検索上限: 0回 / Maps HPを直接調査')

provider=TavilySearchProvider('')
result_holder={'ok':None,'error':None}

def worker():
    try:
        result_holder['ok']=run_job(store,jid,provider)
    except Exception as exc:
        result_holder['error']=repr(exc)

thread=threading.Thread(target=worker,daemon=True)
thread.start()
last=None
while thread.is_alive():
    try:
        s=job_status(store,jid)
        done=s['counts'].get('DONE',0)
        running=s['counts'].get('RUNNING',0)
        pending=s['counts'].get('PENDING',0)
        snapshot=(done,running,pending,s['status'])
        if snapshot!=last:
            print(f'進捗 {done}/{s["total"]}件 | 状態={s["status"]}')
            last=snapshot
    except Exception:
        pass
    time.sleep(2)
thread.join()

if result_holder['error']:
    print('再調査処理で例外:',result_holder['error'])
    raise SystemExit(1)
if result_holder['ok'] is False:
    print('別の調査プロセスが実行中です。アプリを停止してから再実行してください。')
    raise SystemExit(1)

s=job_status(store,jid)
print('完了:',s['status'])
print('件数:',s['total'])
print('結果:',s['results'])
print('Tavily検索:',s.get('search_count',0),'回')
print('次に export_research_v251_results.py を実行してください。')
