"""永続ジョブ＋1医院単位の確定。プロセス間ロックで二重実行を防ぐ。"""
from threading import Thread,Lock
import uuid
import json
from filelock import FileLock,Timeout
from src.master.store import now,dumps
from src.master.filters import where
from src.enrichment.search_provider import CachedSearch,BudgetReached,SearchError
from src.enrichment.researcher import Researcher,Stopped,empty_hp_result


def create_job(store,filters,kind="hp",limit=100,max_searches=100,force=False,max_pages=20):
    if kind not in {"hp","epark","media"}:
        raise ValueError("調査種類を確認してください。")
    limit = min(500,max(1,int(limit)))
    max_searches = min(1000,max(0,int(max_searches)))
    sql,args = where(filters)
    if not force:
        sql += {"hp":" AND hp_status='UNRESEARCHED'", "epark":" AND json_extract(effective_json,'$.epark_checked_at') IS NULL",
                "media":" AND json_extract(effective_json,'$.media_checked_at') IS NULL"}[kind]
    jid = uuid.uuid4().hex
    with store.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        ids = [r[0] for r in c.execute("SELECT id FROM clinics WHERE "+sql+" ORDER BY (uuid<>'') DESC,is_new DESC,id LIMIT ?",(*args,limit))]
        if not ids:
            raise ValueError("指定した条件の未調査医院がありません。条件を見直すか、強制再調査を選択してください。")
        c.execute("INSERT INTO research_jobs(id,kind,options_json,max_searches,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                  (jid,kind,dumps({"force":bool(force),"max_pages":max_pages}),max_searches,now(),now()))
        c.executemany("INSERT INTO research_job_items(job_id,clinic_id) VALUES(?,?)",[(jid,i) for i in ids])
    return jid


def job_status(store,jid):
    with store.connect() as c:
        r = c.execute("SELECT * FROM research_jobs WHERE id=?",(jid,)).fetchone()
        if not r:
            raise ValueError("調査履歴が見つかりません。")
        result = dict(r)
        result["counts"] = {r[0]:r[1] for r in c.execute("SELECT state,count(*) FROM research_job_items WHERE job_id=? GROUP BY state",(jid,))}
        result["results"] = {r[0]:r[1] for r in c.execute("SELECT result,count(*) FROM research_job_items WHERE job_id=? AND state='DONE' GROUP BY result",(jid,))}
        result["total"] = sum(result["counts"].values())
        return result


def recent_jobs(store):
    with store.connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM research_jobs WHERE status<>'RESET' ORDER BY created_at DESC,id DESC LIMIT 20")]


def pause_job(store,jid):
    with store.connect() as c:
        c.execute("UPDATE research_jobs SET status='PAUSED',updated_at=? WHERE id=? AND status<>'COMPLETED'",(now(),jid))


def reset_job(store,jid):
    """調査ジョブだけを履歴画面からリセットする。調査結果・HPページ・検索使用量は消さない。"""
    with store.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT status FROM research_jobs WHERE id=?",(jid,)).fetchone()
        if not row:
            raise ValueError("調査履歴が見つかりません。")
        if row[0] == "RUNNING":
            raise ValueError("実行中の調査はリセットできません。先に一時停止し、停止完了を待ってください。")
        c.execute("UPDATE research_jobs SET status='RESET',updated_at=? WHERE id=?",(now(),jid))


def job_limit(store,jid,limit):
    with store.connect() as c:
        c.execute("UPDATE research_jobs SET max_searches=?,updated_at=? WHERE id=?",(max(0,int(limit)),now(),jid))


def run_job(store,jid,provider,fetcher=None):
    try:
        with FileLock(str(store.path)+".research.lock",timeout=0):
            _run_locked(store,jid,provider,fetcher)
    except Timeout:
        return False
    return True


def _run_locked(store,jid,provider,fetcher):
    with store.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        job = dict(c.execute("SELECT * FROM research_jobs WHERE id=?",(jid,)).fetchone())
        if job["status"]=="COMPLETED":
            return
        # ロック取得できた時点で旧プロセスの実行はない。未完了行だけを回復。
        c.execute("UPDATE research_job_items SET state='PENDING' WHERE state='RUNNING'")
        c.execute("UPDATE research_jobs SET status='PAUSED' WHERE status='RUNNING'")
        c.execute("UPDATE research_jobs SET status='RUNNING',updated_at=? WHERE id=?",(now(),jid))
    search = CachedSearch(store,provider,jid)
    options = json.loads(job["options_json"])
    def stopped():
        return job_status(store,jid)["status"]!="RUNNING"
    researcher = Researcher(search,fetcher,max_pages=options.get("max_pages",20),should_stop=stopped)
    while not stopped():
        with store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT clinic_id FROM research_job_items WHERE job_id=? AND state='PENDING' ORDER BY clinic_id LIMIT 1",(jid,)).fetchone()
            if row is None:
                c.execute("UPDATE research_jobs SET status='COMPLETED',updated_at=? WHERE id=?",(now(),jid))
                return
            cid = row[0]
            c.execute("UPDATE research_job_items SET state='RUNNING' WHERE job_id=? AND clinic_id=?",(jid,cid))
        record = {}
        try:
            record = store.get(cid)
            # 手動値は解析入力としては使うが、自動シグナルの保存に手動値をコピーしない。
            with store.connect() as c:
                auto = c.execute("SELECT result_json FROM research_results WHERE clinic_id=?",(cid,)).fetchone()
                record["marketing_signals"] = json.loads(auto[0]).get("marketing_signals",[]) if auto else []
            result,pages = researcher.run(job["kind"],record,options.get("force",False))
            store.save_research(cid,result,pages)
            status,note = result.get("research_status","SUCCESS"),""
        except (BudgetReached,Stopped) as exc:
            with store.connect() as c:
                c.execute("UPDATE research_job_items SET state='PENDING',note=? WHERE job_id=? AND clinic_id=?",(str(exc),jid,cid))
                c.execute("UPDATE research_jobs SET status=?,updated_at=? WHERE id=?",("BUDGET" if isinstance(exc,BudgetReached) else "PAUSED",now(),jid))
            return
        except Exception as exc:
            # 接続の秘密や生Tracebackを画面/DBへ残さない。
            status = "ERROR"
            note = str(exc) if isinstance(exc,SearchError) else "この医院の調査でエラーが発生しました。再調査または手動確認を行ってください。"
            safe_result = {"research_status":"ERROR","research_error":note}
            if job["kind"]=="hp":
                safe_result.update(empty_hp_result("ERROR",record))
            else:
                safe_result[job["kind"]+"_checked_at"] = now()
            store.save_research(cid,safe_result,[] if job["kind"]=="hp" else None)
        with store.connect() as c:
            c.execute("UPDATE research_job_items SET state='DONE',result=?,note=? WHERE job_id=? AND clinic_id=?",(status,note,jid,cid))
            c.execute("UPDATE research_jobs SET updated_at=? WHERE id=?",(now(),jid))


class JobRunner:
    def __init__(self):
        self.thread = None
        self.lock = Lock()

    def start(self,store,jid,provider):
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError("調査中です。一時停止してから別の調査を開始してください。")
            self.thread = Thread(target=run_job,args=(store,jid,provider),daemon=True)
            self.thread.start()

    def running(self):
        return bool(self.thread and self.thread.is_alive())
