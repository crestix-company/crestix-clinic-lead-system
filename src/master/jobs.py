"""永続ジョブ＋1医院単位の確定。プロセス間ロックで二重実行を防ぐ。"""
from threading import Thread,Lock
from concurrent.futures import ThreadPoolExecutor
import json
from filelock import FileLock,Timeout
from src.master.store import now
from src.enrichment.search_provider import CachedSearch,BudgetReached,SearchError
from src.enrichment.researcher import Researcher,Stopped,empty_hp_result
from src.enrichment.hp_analysis import host
from src.enrichment.safe_web import _host_key

# HP調査を同時に進める医院数。最初は2並列固定（4並列は2並列の安全性確認後）。
# 同じサイトの医院は必ず同じ処理がclinic_id順に続けて調べるため、サイトごとの
# 取得間隔・Crawl-delay・robots.txt・アクセス制限の扱いは順次処理と同じになる。
PARALLEL_WORKERS = 2
_WRITE_LOCK = Lock()

ITEM_STATES = frozenset({"PENDING", "RUNNING", "DONE", "CANCELLED"})
TERMINAL_ITEM_STATES = frozenset({"DONE", "CANCELLED"})
ITEM_TRANSITIONS = frozenset({
    ("PENDING", "RUNNING"),
    ("PENDING", "CANCELLED"),
    ("RUNNING", "DONE"),
    ("RUNNING", "PENDING"),
})


def create_job(store,filters,kind="hp",limit=100,max_searches=100,force=False,max_pages=20):
    # Stage4-D Gate2: persistent WRITE goes through the Repository (same SQL, same atomicity --
    # see src.repository.sqlite_write_adapter.SqliteJobsWriteRepository.create_job_from_filters).
    from src.repository.write_backend import write_repositories_for
    return write_repositories_for(store).jobs.create_job_from_filters(filters,kind,limit,max_searches,force,max_pages)


def job_status(store,jid):
    from src.repository.write_backend import write_repositories_for
    return write_repositories_for(store).jobs.job_status(jid)


def recent_jobs(store):
    from src.repository.write_backend import write_repositories_for
    return write_repositories_for(store).jobs.recent_jobs()


def pause_job(store,jid):
    # Stage4-D Gate2: see create_job() above.
    from src.repository.write_backend import write_repositories_for
    write_repositories_for(store).jobs.pause_job(jid)


def reset_job(store,jid):
    """未処理itemを終了し、jobをリセットする。既存の調査結果等は消さない。"""
    # Stage4-D Gate2: see create_job() above.
    from src.repository.write_backend import write_repositories_for
    write_repositories_for(store).jobs.reset_job(jid)


def repair_reset_job_items(store,job_ids,dry_run=True):
    """指定したRESET jobのPENDING itemだけをCANCELLEDへ移す。

    dry-runも同じBEGIN IMMEDIATE transactionで対象を数え、必ずrollbackする。
    job IDの暗黙選択を避けるため、空の指定は受け付けない。
    """
    ids = tuple(dict.fromkeys(str(jid).strip() for jid in job_ids if str(jid).strip()))
    if not ids:
        raise ValueError("repair対象のjob IDを明示してください。")
    placeholders = ",".join("?" for _ in ids)
    eligible_sql = f"""SELECT COUNT(*)
        FROM research_job_items i
        JOIN research_jobs j ON j.id=i.job_id
        WHERE i.job_id IN ({placeholders})
          AND j.status='RESET' AND i.state='PENDING'"""
    with store.connect() as c:
        c.execute("BEGIN" if dry_run else "BEGIN IMMEDIATE")
        found = {r[0] for r in c.execute(
            f"SELECT id FROM research_jobs WHERE id IN ({placeholders})", ids
        )}
        missing = [jid for jid in ids if jid not in found]
        if missing:
            raise ValueError("調査履歴が見つかりません: " + ", ".join(missing))
        before = c.execute(eligible_sql,ids).fetchone()[0]
        changed = 0
        if not dry_run:
            changed = c.execute(f"""UPDATE research_job_items
                SET state='CANCELLED'
                WHERE state='PENDING' AND job_id IN ({placeholders})
                  AND EXISTS (
                    SELECT 1 FROM research_jobs j
                    WHERE j.id=research_job_items.job_id AND j.status='RESET'
                  )""",ids).rowcount
        after = c.execute(eligible_sql,ids).fetchone()[0]
        result = {"job_ids":list(ids),"dry_run":bool(dry_run),"before":before,
                  "changed":changed,"after":after}
        if dry_run:
            c.rollback()
        return result


def job_limit(store,jid,limit):
    # Stage4-D Gate2: see create_job() above.
    from src.repository.write_backend import write_repositories_for
    write_repositories_for(store).jobs.job_limit(jid,limit)


def run_job(store,jid,provider,fetcher=None,progress_callback=None):
    if getattr(store, "is_supabase_runtime", False):
        # Cross-PC exclusion is the PostgreSQL row-claim/lease contract (FOR UPDATE SKIP
        # LOCKED), not a machine-local lock file.
        _run_locked(store,jid,provider,fetcher,progress_callback)
        return True
    try:
        with FileLock(str(store.path)+".research.lock",timeout=0):
            _run_locked(store,jid,provider,fetcher,progress_callback)
    except Timeout:
        return False
    return True


def _run_locked(store,jid,provider,fetcher,progress_callback=None):
    # Stage4-D Gate2: persistent WRITE goes through the Repository.
    from src.repository.write_backend import write_repositories_for
    repo = write_repositories_for(store).jobs
    def progress():
        if progress_callback is not None and progress_callback() is False:
            raise Stopped("Auto Run controller leaseを失いました。")
    progress()
    # ロック取得できた時点で旧プロセスの実行はない。未完了行だけを回復。
    job = repo.recover_job_for_run(jid)
    if job is None:
        return
    options = job["options_json"] if isinstance(job["options_json"], dict) else json.loads(job["options_json"])
    def stopped():
        return job_status(store,jid)["status"]!="RUNNING"
    def new_researcher():
        return Researcher(CachedSearch(store,provider,jid),fetcher,max_pages=options.get("max_pages",20),should_stop=stopped)
    # 並列はHP調査で、通信部品を医院ごとに新しく作れる通常経路だけ（渡されたfetcherは共有できないため順次）。
    if job["kind"]!="hp" or fetcher is not None or PARALLEL_WORKERS<=1:
        researcher = new_researcher()
        while not stopped():
            progress()
            claimed = repo.claim_next_pending_item_with_token(jid)
            if claimed is None:
                repo.mark_job_status(jid, "COMPLETED")
                return
            cid,claim_token = claimed
            if not _research_one(store,jid,job,options,researcher,cid,claim_token):
                return
            progress()
        return
    while not stopped():
        lanes = _site_lanes(store,jid)
        if not lanes:
            repo.complete_job_if_no_remaining_items(jid)
            return
        queue,queue_lock = list(lanes),Lock()
        def worker():
            # 1つの処理は専用の通信部品（Researcher/SafeFetcher）を持ち、割り当てられたサイトの医院をclinic_id順に調べる。
            researcher = new_researcher()
            while not stopped():
                progress()
                with queue_lock:
                    if not queue:
                        return
                    lane = queue.pop(0)
                for cid in lane:
                    if stopped():
                        return
                    with _WRITE_LOCK:
                        claim_token = repo.claim_specific_item_with_token(jid, cid)
                    if claim_token and not _research_one(store,jid,job,options,researcher,cid,claim_token):
                        return
                    progress()
        with ThreadPoolExecutor(max_workers=min(PARALLEL_WORKERS,len(lanes))) as pool:
            futures = [pool.submit(worker) for _ in range(min(PARALLEL_WORKERS,len(lanes)))]
        for f in futures:
            f.result()  # 想定外の例外は順次処理と同様に呼び出し元へ伝える（全処理の終了後）


def _site_lanes(store,jid):
    """未調査の医院を、調査対象サイト（www有無は同一）ごとにclinic_id順でまとめる。

    MapsのウェブサイトURLがある医院は検索せず、そのサイト内だけを取得する（別サイトへの
    リダイレクトは取得前に停止）ため、サイトごとに並列にできる。URLがない医院は検索結果の
    別サイトを取得し得るため、並列の処理がすべて終わった後に単独・clinic_id順で調べる。
    """
    lanes,alone = {},[]
    from src.repository.write_backend import write_repositories_for
    rows = write_repositories_for(store).jobs.pending_site_candidates(jid)
    for cid,maps_status,maps_url in rows:
        key = _host_key(host(maps_url)) if maps_status=="MAPS_MATCHED_WEBSITE" and maps_url else ""
        if key:
            lanes.setdefault(key,[]).append(cid)
        else:
            alone.append(cid)
    return list(lanes.values()) if lanes else ([alone] if alone else [])


def _research_one(store,jid,job,options,researcher,cid,claim_token=None):
    """1医院の調査と確定。続行してよければTrue、一時停止・上限で止める場合はFalse。"""
    # Stage4-D Gate2: persistent WRITE goes through the Repository.
    from src.repository.write_backend import write_repositories_for
    repositories = write_repositories_for(store)
    jobs_repo = repositories.jobs
    research_repo = repositories.research
    record = {}
    try:
        record = store.get(cid)
        # 手動値は解析入力としては使うが、自動シグナルの保存に手動値をコピーしない。
        auto = research_repo.get_saved_research(cid)
        record["marketing_signals"] = auto.get("marketing_signals", [])
        result,pages = researcher.run(job["kind"],record,options.get("force",False))
        if claim_token and not jobs_repo.item_claim_is_current(jid, cid, claim_token):
            return True
        with _WRITE_LOCK:
            research_repo.save_research(cid,result,pages)
        status,note = result.get("research_status","SUCCESS"),""
        if job["kind"] == "hp" and repositories.hp is not None:
            with _WRITE_LOCK:
                repositories.hp.upsert_result(_hp_ledger_payload(cid, result, status, note))
    except (BudgetReached,Stopped) as exc:
        with _WRITE_LOCK:
            if claim_token:
                jobs_repo.requeue_claimed_item(
                    jid,cid,claim_token,str(exc),"BUDGET" if isinstance(exc,BudgetReached) else "PAUSED"
                )
            else:
                jobs_repo.requeue_item_for_budget_or_pause(
                    jid,cid,str(exc),"BUDGET" if isinstance(exc,BudgetReached) else "PAUSED"
                )
        return False
    except Exception as exc:
        if claim_token and not jobs_repo.item_claim_is_current(jid, cid, claim_token):
            return True
        # 接続の秘密や生Tracebackを画面/DBへ残さない。
        status = "ERROR"
        note = str(exc) if isinstance(exc,SearchError) else "この医院の調査でエラーが発生しました。再調査または手動確認を行ってください。"
        safe_result = {"research_status":"ERROR","research_error":note}
        if job["kind"]=="hp":
            safe_result.update(empty_hp_result("ERROR",record))
        else:
            safe_result[job["kind"]+"_checked_at"] = now()
        with _WRITE_LOCK:
            research_repo.save_research(cid,safe_result,[] if job["kind"]=="hp" else None)
        if job["kind"] == "hp" and repositories.hp is not None:
            with _WRITE_LOCK:
                repositories.hp.upsert_result(_hp_ledger_payload(cid, safe_result, status, note))
    with _WRITE_LOCK:
        if claim_token:
            jobs_repo.finish_claimed_item(jid,cid,claim_token,status,note)
        else:
            jobs_repo.finish_item(jid,cid,status,note)
    return True


def _hp_ledger_payload(clinic_id, result, status, note=""):
    """Translate one terminal runtime HP attempt to the canonical HP research ledger.

    A ledger row means "attempted", not "successful". Only a verified SUCCESS with a final
    URL gets fetch_status=OK; terminal REVIEW/ERROR/NOT_FOUND attempts are retained as such.
    This deliberately leaves HP rank and Treatment projection fields untouched.
    """
    from datetime import datetime, timezone
    from src.master.hp_site_type import portal_name_for_url

    hp_url = str(result.get("hp_url") or "").strip()
    final_url = str(result.get("final_url") or hp_url).strip()
    usable = (
        status == "SUCCESS"
        and result.get("hp_status") == "VERIFIED"
        and result.get("hp_verified") is True
        and bool(hp_url)
    )
    fetch_status = "OK" if usable else str(status or result.get("research_status") or "ERROR").upper()
    categories = result.get("treatment_categories") or []
    if isinstance(categories, str):
        try:
            categories = json.loads(categories)
        except (TypeError, ValueError):
            categories = []
    if not isinstance(categories, list):
        categories = []
    return {
        "clinic_id": clinic_id,
        "hp_url": hp_url,
        "fetch_status": fetch_status,
        "final_url": final_url,
        "treatment_status": "DONE" if usable else "FETCH_FAILED",
        "treatment_categories": json.dumps(categories, ensure_ascii=False),
        "hp_abc_candidate": "",
        "hp_abc_score": "",
        "candidate_rank_1": "",
        "candidate_rank_2": "",
        "ambiguity_reason": "",
        "feature_json": "[]",
        "researched_at": result.get("hp_checked_at") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engine_version": "research_jobs_hp_v1",
        "error_detail": str(note or result.get("research_error") or ""),
        "elapsed_seconds": 0,
        "attempts": 1,
        "portal_name": portal_name_for_url(final_url or hp_url),
    }


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
