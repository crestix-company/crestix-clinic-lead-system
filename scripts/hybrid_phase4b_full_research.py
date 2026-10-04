"""Resumable Phase 4-B structured official-HP Research.

Production inputs are opened read-only. Every fetched page and link is written
to an isolated Phase 4-B cache; existing Phase 4-A cache is read-only.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from scripts import hybrid_phase4a_pilot as p4a
from src.enrichment.hp_analysis import Page, host
from src.enrichment.hybrid_treatment import evaluate_hybrid_treatments

SEED = "hybrid-treatment-phase4b-full-20261002-v1"
PHASE3 = Path("artifacts/hybrid_phase3/strict_vs_hybrid.csv")
PILOT_DB = Path("data/hybrid_phase4_pilot/structured_cache.sqlite3")
OUT = Path("artifacts/hybrid_phase4b")
CACHE = Path("data/hybrid_phase4b/structured_cache.sqlite3")
SAFE_RETRY_TARGETS = Path("artifacts/hybrid_phase4b_retry/robots_safe_retry_targets.csv")
POSITIVE = {"CONFIRMED", "MENTIONED", "REVIEW"}
STATUS_ORDER = ("CONFIRMED", "MENTIONED", "REVIEW", "NOT_CONFIRMED", "CANDIDATE_ONLY")
ROBOTS_SAFE_ERROR = "robots.txtを安全に確認できないため取得を見送りました。"


def load_phase3_groups(path: Path) -> dict[int, str]:
    groups: dict[int, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r["strict_status"] != "ZERO":
                continue
            if r["replay_coverage"] == "NO_REPLAY_DATA":
                group = "NO_REPLAY_DATA"
            elif r["hybrid_status"] == "NO_SIGNAL":
                group = "NO_SIGNAL"
            elif r["hybrid_status"] == "CANDIDATE_ONLY":
                group = "CANDIDATE_ONLY"
            elif r["hybrid_status"] == "NOT_CONFIRMED":
                group = "NOT_CONFIRMED"
            else:
                continue  # Phase-3 positive signals are already in baseline.
            groups[int(r["clinic_id"])] = group
    expected = {"NO_SIGNAL": 2900, "NO_REPLAY_DATA": 769,
                "CANDIDATE_ONLY": 107, "NOT_CONFIRMED": 14}
    actual = {g: sum(v == g for v in groups.values()) for g in expected}
    if actual != expected:
        raise AssertionError(f"Phase-3 zero-row strata changed: {actual} != {expected}")
    return groups


def init_cache(path: Path, target_hash: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS run_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS clinic_fetch(
        clinic_id INTEGER PRIMARY KEY,sampling_group TEXT NOT NULL,initial_url TEXT NOT NULL,
        final_url TEXT NOT NULL DEFAULT '',fetch_status TEXT NOT NULL,identity_verified INTEGER NOT NULL,
        identity_reasons_json TEXT NOT NULL DEFAULT '[]',pages_fetched INTEGER NOT NULL DEFAULT 0,
        http_request_count INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',completed_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS page_cache(
        clinic_id INTEGER NOT NULL,page_url TEXT NOT NULL,final_url TEXT NOT NULL,page_title TEXT NOT NULL,
        raw_html TEXT NOT NULL,normalized_html TEXT NOT NULL,h1_json TEXT NOT NULL,h2_json TEXT NOT NULL,
        anchor_labels_json TEXT NOT NULL,anchor_hrefs_json TEXT NOT NULL,main_text TEXT NOT NULL,
        page_type TEXT NOT NULL,fetched_at TEXT NOT NULL,fetch_status TEXT NOT NULL,content_hash TEXT NOT NULL,
        PRIMARY KEY(clinic_id,page_url));
      CREATE TABLE IF NOT EXISTS link_graph(
        clinic_id INTEGER NOT NULL,source_page TEXT NOT NULL,anchor_label TEXT NOT NULL,
        destination_page TEXT NOT NULL,destination_fetched INTEGER NOT NULL,
        PRIMARY KEY(clinic_id,source_page,anchor_label,destination_page));
      CREATE TABLE IF NOT EXISTS fetch_attempt_history(
        history_id INTEGER PRIMARY KEY,clinic_id INTEGER NOT NULL,sampling_group TEXT NOT NULL,
        initial_url TEXT NOT NULL,fetch_status TEXT NOT NULL,pages_fetched INTEGER NOT NULL,
        http_request_count INTEGER NOT NULL,error TEXT NOT NULL,completed_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS clinic_state(
        clinic_id INTEGER PRIMARY KEY,state TEXT NOT NULL,updated_at TEXT NOT NULL,error TEXT NOT NULL DEFAULT '');
    """)
    meta = dict(db.execute("SELECT key,value FROM run_meta"))
    expected = {"seed": SEED, "target_sha256": target_hash,
                "max_pages_per_clinic": "5", "rule_version": "hybrid-treatment-v1"}
    if meta and any(meta.get(k) != v for k, v in expected.items()):
        raise RuntimeError("Phase-4B cache/config mismatch; refusing to reuse or overwrite")
    if not meta:
        db.executemany("INSERT INTO run_meta VALUES(?,?)", expected.items())
    db.commit()
    return db


def retry_robot_rows(db: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return only the transient robots-safety failures approved for retry."""
    return db.execute(
        "SELECT * FROM clinic_fetch WHERE fetch_status='FETCH_FAILED' AND error=? ORDER BY clinic_id",
        (ROBOTS_SAFE_ERROR,),
    ).fetchall()


def load_retry_target_ids(path: Path) -> set[int]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows or "clinic_id" not in rows[0]:
        raise ValueError("retry target CSV must contain clinic_id")
    ids = [int(r["clinic_id"]) for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("retry target CSV contains duplicate clinic_id")
    return set(ids)


def retry_robots_plan(cache_path: Path) -> dict:
    """Inspect the retry population without mutating the Phase-4B cache."""
    with sqlite3.connect(f"file:{cache_path.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        rows = retry_robot_rows(db)
        group_counts = {
            r["sampling_group"]: int(r["n"])
            for r in db.execute(
                """SELECT sampling_group,count(*) AS n FROM clinic_fetch
                   WHERE fetch_status='FETCH_FAILED' AND error=?
                   GROUP BY sampling_group ORDER BY sampling_group""",
                (ROBOTS_SAFE_ERROR,),
            )
        }
        other_failures = int(db.execute(
            """SELECT count(*) FROM clinic_fetch
               WHERE fetch_status IN ('FETCH_FAILED','ERROR')
                 AND NOT (fetch_status='FETCH_FAILED' AND error=?)""",
            (ROBOTS_SAFE_ERROR,),
        ).fetchone()[0])
    return {
        "mode": "DRY_RUN_READ_ONLY",
        "retry_reason": ROBOTS_SAFE_ERROR,
        "retry_target_count": len(rows),
        "retry_target_group_counts": group_counts,
        "excluded_other_failure_count": other_failures,
        "first_clinic_id": int(rows[0]["clinic_id"]) if rows else None,
        "last_clinic_id": int(rows[-1]["clinic_id"]) if rows else None,
        "cache_changed": False,
        "http_started": False,
    }


def fetch_targets(targets: list[dict], cache_path: Path, target_hash: str,
                  clinic_db: Path, mhlw_db: Path, retry_target_ids: set[int] | None) -> tuple[int, int]:
    db = init_cache(cache_path, target_hash)
    if retry_target_ids is not None:
        failed = [r for r in retry_robot_rows(db) if int(r["clinic_id"]) in retry_target_ids]
        with db:
            for r in failed:
                db.execute("INSERT INTO fetch_attempt_history(clinic_id,sampling_group,initial_url,fetch_status,pages_fetched,http_request_count,error,completed_at) VALUES(?,?,?,?,?,?,?,?)",
                           tuple(r[k] for k in ("clinic_id","sampling_group","initial_url","fetch_status","pages_fetched","http_request_count","error","completed_at")))
                db.execute("DELETE FROM clinic_fetch WHERE clinic_id=?", (r["clinic_id"],))
                db.execute("DELETE FROM clinic_state WHERE clinic_id=?", (r["clinic_id"],))
                db.execute("DELETE FROM page_cache WHERE clinic_id=?", (r["clinic_id"],))
                db.execute("DELETE FROM link_graph WHERE clinic_id=?", (r["clinic_id"],))
    with p4a.open_ro(clinic_db) as cdb:
        clinics = {int(r["id"]): dict(r) for r in cdb.execute("SELECT * FROM clinics")}
        old = {int(r["clinic_id"]): p4a.read_json(r["result_json"], {})
               for r in cdb.execute("SELECT clinic_id,result_json FROM research_results")}
    transport = p4a.CountingTransport()
    fetcher = p4a.SafeFetcher(transport=transport, timeout=12, max_bytes=1_500_000, interval=.6)
    total = int(db.execute("SELECT COALESCE(sum(http_request_count),0) FROM fetch_attempt_history").fetchone()[0])
    completed = {int(r[0]) for r in db.execute("SELECT clinic_id FROM clinic_fetch")}
    for i, rec in enumerate(targets, 1):
        cid = int(rec["clinic_id"])
        if cid in completed:
            continue
        now = datetime.now(timezone.utc).isoformat()
        with db:
            db.execute("INSERT OR REPLACE INTO clinic_state VALUES(?,?,?,?)", (cid,"RUNNING",now,""))
        before = transport.request_count
        pages, status, verified, reasons, error = [], "FETCH_FAILED", False, [], ""
        try:
            first = fetcher.fetch(rec["selected_identity_url"])
            pages = [first]
            check = p4a.identity(clinics[cid], first)
            verified, reasons = bool(check.get("verified")), check.get("reasons", [])
            if verified:
                pages, errs = p4a.crawl(first, fetcher, max_pages=5)
                status = "OK"
                if errs:
                    error = " | ".join(x["reason"] for x in errs[:3])
            else:
                status = "IDENTITY_NOT_VERIFIED"
        except p4a.WebError as exc:
            error, status = str(exc), "FETCH_FAILED"
        except Exception as exc:  # checkpoint unexpected errors, never retry-loop
            error, status = f"{type(exc).__name__}: {exc}", "ERROR"
        requests = transport.request_count - before
        with db:
            p4a._store_clinic(db, rec, pages, status, verified, reasons, requests, error)
            db.execute("UPDATE clinic_fetch SET final_url=? WHERE clinic_id=?", (pages[0].url if pages else "",cid))
            db.execute("UPDATE clinic_state SET state=?,updated_at=?,error=? WHERE clinic_id=?",
                       (status,datetime.now(timezone.utc).isoformat(),error,cid))
        total += requests
        completed.add(cid)
        if i % 25 == 0 or i == len(targets):
            print(f"phase4b {i}/{len(targets)} clinic_id={cid} {status} pages={len(pages)} requests={total}", flush=True)
    db.close()
    return total, len(completed)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clinic-db", type=Path, default=p4a.PRODUCTION_CLINIC_DB)
    ap.add_argument("--research-db", type=Path, default=p4a.PRODUCTION_RESEARCH_DB)
    ap.add_argument("--mhlw-db", type=Path, default=p4a.MHLW_DB)
    ap.add_argument("--phase3-csv", type=Path, default=PHASE3)
    ap.add_argument("--pilot-db", type=Path, default=PILOT_DB)
    ap.add_argument("--cache-db", type=Path, default=CACHE)
    ap.add_argument("--output-dir", type=Path, default=OUT)
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--retry-robots", action="store_true",
                    help="disabled legacy mode; use --retry-targets-csv")
    ap.add_argument("--retry-targets-csv", type=Path,
                    help="retry only clinic_id values in this safe target CSV")
    ap.add_argument("--retry-robots-dry-run", action="store_true",
                    help="read-only retry target report; performs no HTTP requests or writes")
    args = ap.parse_args()
    if args.retry_robots_dry_run:
        print(json.dumps(retry_robots_plan(args.cache_db), ensure_ascii=False, indent=2))
        return
    if args.retry_robots:
        ap.error("--retry-robots is disabled; use --retry-targets-csv with an audited safe target file")
    if args.retry_targets_csv and not args.fetch:
        ap.error("--retry-targets-csv requires --fetch")
    if args.fetch and not args.retry_targets_csv:
        ap.error("Phase 4-B cache exists; --fetch requires --retry-targets-csv")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.cache_db.parent.mkdir(parents=True, exist_ok=True)
    groups = load_phase3_groups(args.phase3_csv)
    with sqlite3.connect(f"file:{args.pilot_db.resolve()}?mode=ro", uri=True) as pdb:
        pdb.execute("PRAGMA query_only=ON")
        pilot_ids = {int(r[0]) for r in pdb.execute("SELECT clinic_id FROM clinic_fetch")}
    all_records, _ = p4a._load_records(args.clinic_db,args.research_db,args.mhlw_db,groups)
    targets = [r for r in all_records if r["clinic_id"] not in pilot_ids]
    # The 14 Phase-3 NOT_CONFIRMED clinics are retained as lowest-priority targets.
    if len(pilot_ids) != 500 or len(targets) != 3290:
        raise AssertionError(f"Target recalculation mismatch: pilot={len(pilot_ids)}, remaining={len(targets)}")
    group_counts = {g: sum(r["sampling_group"] == g for r in targets) for g in
                    ("NO_SIGNAL","NO_REPLAY_DATA","CANDIDATE_ONLY","NOT_CONFIRMED")}
    rows = []
    for r in targets:
        rows.append({"clinic_id":r["clinic_id"],"clinic_name":r["clinic_name"],"sampling_group":r["sampling_group"],
                     "prefecture":r["prefecture"],"formal_departments":" / ".join(r["formal_departments"]),
                     "candidate_categories":" / ".join(r["candidate_categories"]),"hp_domain":host(r["selected_identity_url"]),
                     "selected_identity_url":r["selected_identity_url"],"url_source":r["url_source"],"seed":SEED})
    p4a.write_csv(args.output_dir/"population.csv",rows,list(rows[0]))
    target_hash = p4a.sha256(args.output_dir/"population.csv")
    hashes_before = {"clinics":p4a.sha256(args.clinic_db),"research":p4a.sha256(args.research_db),"mhlw":p4a.sha256(args.mhlw_db)}
    retry_target_ids = None
    if args.retry_targets_csv:
        retry_target_ids = load_retry_target_ids(args.retry_targets_csv)
        safe_ids = load_retry_target_ids(SAFE_RETRY_TARGETS)
        if not retry_target_ids <= safe_ids:
            raise SystemExit(f"retry CSV contains {len(retry_target_ids-safe_ids)} clinic_id(s) outside audited safe targets")
    if args.fetch:
        fetch_targets(targets,args.cache_db,target_hash,args.clinic_db,args.mhlw_db,retry_target_ids)
    if not args.cache_db.exists():
        raise SystemExit("No Phase-4B cache yet. Run again with --fetch to start the resumable crawl.")
    with sqlite3.connect(f"file:{args.cache_db.resolve()}?mode=ro",uri=True) as db:
        db.row_factory=sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        integrity=db.execute("PRAGMA integrity_check").fetchone()[0]
        fetch={int(r["clinic_id"]):dict(r) for r in db.execute("SELECT * FROM clinic_fetch")}
        pages_by={cid:[Page(r["page_url"],r["raw_html"]) for r in db.execute("SELECT page_url,raw_html FROM page_cache WHERE clinic_id=? ORDER BY page_url",(cid,))] for cid in fetch}
        page_count=db.execute("SELECT count(*) FROM page_cache").fetchone()[0]
        graph_count=db.execute("SELECT count(*) FROM link_graph").fetchone()[0]
        history={g:int(db.execute("SELECT COALESCE(sum(http_request_count),0) FROM fetch_attempt_history WHERE sampling_group=?",(g,)).fetchone()[0]) for g in group_counts}
        history_total=int(db.execute("SELECT COALESCE(sum(http_request_count),0) FROM fetch_attempt_history").fetchone()[0])
    replay=[]
    for rec in targets:
        cid=int(rec["clinic_id"]); fr=fetch.get(cid)
        if not fr or fr["fetch_status"]!="OK" or not fr["identity_verified"]: continue
        # A malformed full-width-host href is not a valid official-site link.
        # Drop only that href attribute in the in-memory replay copy; preserve
        # source cache bytes and all visible text/evidence.
        safe_pages=[]
        for page in pages_by[cid]:
            soup=BeautifulSoup(page.html,"html.parser")
            for anchor in soup.select("a[href]"):
                try: urljoin(page.url,anchor.get("href",""))
                except ValueError: anchor.attrs.pop("href",None)
            safe_pages.append(Page(page.url,str(soup)))
        results,_=evaluate_hybrid_treatments(clinic_id=cid,clinic_name=rec["clinic_name"],departments=rec["formal_departments"],
            pages=safe_pages,checked_at=datetime.now(timezone.utc).isoformat(),identity_verified=True)
        for obj in results:
            row=obj.to_dict(); row.update({"clinic_name":rec["clinic_name"],"sampling_group":rec["sampling_group"],
                 "formal_departments":" / ".join(rec["formal_departments"]),"page_count":len(pages_by[cid]),"fetch_status":"OK"})
            replay.append(row)
    best={}
    for row in replay:
        key=(int(row["clinic_id"]),row["treatment_category"])
        if key not in best or (STATUS_ORDER.index(row["hybrid_status"]),row["confidence"]) < (STATUS_ORDER.index(best[key]["hybrid_status"]),best[key]["confidence"]): best[key]=row
    grouped={}
    for (cid,_),r in best.items(): grouped.setdefault(cid,[]).append(r)
    clinic_rows=[]
    for rec in targets:
        cid=int(rec["clinic_id"]); fr=fetch.get(cid); rr=grouped.get(cid,[])
        if not fr: status="PENDING"
        elif fr["fetch_status"]=="ERROR": status="ERROR"
        elif fr["fetch_status"]!="OK": status=fr["fetch_status"]
        elif any(r["hybrid_status"]=="CONFIRMED" for r in rr): status="CONFIRMED"
        elif any(r["hybrid_status"]=="MENTIONED" for r in rr): status="MENTIONED"
        elif any(r["hybrid_status"]=="REVIEW" for r in rr): status="REVIEW"
        elif any(r["hybrid_status"]=="CANDIDATE_ONLY" for r in rr): status="CANDIDATE_ONLY"
        elif any(r["hybrid_status"]=="NOT_CONFIRMED" for r in rr): status="NOT_CONFIRMED"
        else: status="NO_SIGNAL"
        clinic_rows.append({"clinic_id":cid,"clinic_name":rec["clinic_name"],"sampling_group":rec["sampling_group"],"outcome":status,
          "formal_departments":" / ".join(rec["formal_departments"]),"fetch_status":fr["fetch_status"] if fr else "PENDING",
          "identity_verified":fr["identity_verified"] if fr else 0,"pages_fetched":fr["pages_fetched"] if fr else 0,
          "treatments":" / ".join(sorted({r["treatment_category"] for r in rr if r["hybrid_status"] in POSITIVE}))})
    p4a.write_csv(args.output_dir/"clinic_results.csv",clinic_rows,list(clinic_rows[0]))
    treatment_rows=list(best.values())
    p4a.write_csv(args.output_dir/"treatment_results.csv",treatment_rows,list(treatment_rows[0]) if treatment_rows else ["clinic_id"])
    source_rows=[]
    for src in ("CLINIC_NAME","HOME_MENU","INTRO_MENU","DEDICATED_PAGE","OFFICIAL_HP_TEXT"):
        subset=[r for r in treatment_rows if r["signal_source"]==src and r["hybrid_status"] in POSITIVE]
        source_rows.append({"signal_source":src,"treatment_rows":len(subset),"unique_clinics":len({r["clinic_id"] for r in subset})})
    p4a.write_csv(args.output_dir/"signal_source_summary.csv",source_rows,["signal_source","treatment_rows","unique_clinics"])
    cats={}
    for r in treatment_rows:
        if r["hybrid_status"] not in POSITIVE: continue
        cats.setdefault(r["treatment_category"],[]).append(r)
    cat_rows=[{"category":c,**{s:sum(r["hybrid_status"]==s for r in rr) for s in ("CONFIRMED","MENTIONED","REVIEW","NOT_CONFIRMED")},
               "unique_clinics":len({r["clinic_id"] for r in rr}),"false_positive_candidates":0} for c,rr in sorted(cats.items())]
    p4a.write_csv(args.output_dir/"treatment_category_summary.csv",cat_rows,list(cat_rows[0]) if cat_rows else ["category"])
    # Emit deterministic stratified audit selection; adjudication is deliberately left blank.
    audit_pool=[r for r in treatment_rows if r["hybrid_status"] in POSITIVE]
    audit_sample=[]
    quotas={"CONFIRMED":150,"MENTIONED":100,"REVIEW":50}
    for status,quota in quotas.items():
        pool=[r for r in audit_pool if r["hybrid_status"]==status]
        # Deterministic greedy stratification across source and treatment category.
        chosen=[]; source_n={}; category_n={}
        while pool and len(chosen)<quota:
            pool.sort(key=lambda r:(source_n.get(r["signal_source"],0)+category_n.get(r["treatment_category"],0),
                hashlib.sha256(f"{SEED}|audit|{status}|{r['clinic_id']}|{r['treatment_category']}".encode()).hexdigest()))
            row=pool.pop(0); chosen.append(row)
            source_n[row["signal_source"]]=source_n.get(row["signal_source"],0)+1
            category_n[row["treatment_category"]]=category_n.get(row["treatment_category"],0)+1
        audit_sample.extend({**r,"audit_label":"","audit_comment":""} for r in chosen)
    p4a.write_csv(args.output_dir/"human_audit_sample.csv",audit_sample,list(audit_sample[0]) if audit_sample else ["clinic_id","audit_label"])
    # Preserve the exact Phase-4A failure signatures and search this replay for
    # matching treatment/category/context combinations before Safety Refinement.
    fp_seeds=[
      {"clinic_id":4317,"clinic_name":"（Phase4-A record）","treatment_category":"ニキビ跡施術","signal_source":"HOME_MENU","matched_alias":"","pattern":"blog article promoted as a homepage treatment card","safety_bucket":"MENU_CONTEXT"},
      {"clinic_id":6681,"clinic_name":"（Phase4-A record）","treatment_category":"歯列矯正","signal_source":"OFFICIAL_HP_TEXT","matched_alias":"矯正治療","pattern":"dermatology/toenail correction alias collision","safety_bucket":"ALIAS_TOO_BROAD"},
      {"clinic_id":6360,"clinic_name":"（Phase4-A record）","treatment_category":"体外受精（IVF）","signal_source":"OFFICIAL_HP_TEXT","matched_alias":"体外受精","pattern":"clinic contrasts its own service with IVF; negated offer","safety_bucket":"NEGATIVE_CONTEXT"},
    ]
    p4a.write_csv(args.output_dir/"phase4a_false_positive_seeds.csv",fp_seeds,list(fp_seeds[0]))
    p4a.write_csv(args.output_dir/"false_positive_patterns.csv",[
      {"safety_bucket":b,"pattern":b,"matched_rows":0,"unique_clinics":0,"audit_status":"PENDING_HUMAN_AUDIT"}
      for b in ("ALIAS_TOO_BROAD","CROSS_DEPARTMENT","MENU_CONTEXT","OTHER_PROVIDER","ARTICLE_CONTEXT","GENERAL_INFO","NEGATIVE_CONTEXT","HTML_NOISE","CLINIC_IDENTITY","OTHER")],
      ["safety_bucket","pattern","matched_rows","unique_clinics","audit_status"])
    p4a.write_csv(args.output_dir/"mentioned_invalid_patterns.csv",[
      {"clinic_id":13320,"treatment_category":"ED治療","cause":"HTML_ARTEFACT","details":"Phase 4-A matched alias existed only in an HTML comment/disabled carousel; inspect same pattern in Phase 4-B raw HTML."}],
      ["clinic_id","treatment_category","cause","details"])
    outcomes={s:sum(r["outcome"]==s for r in clinic_rows) for s in ("CONFIRMED","MENTIONED","REVIEW","CANDIDATE_ONLY","NOT_CONFIRMED","NO_SIGNAL","FETCH_FAILED","IDENTITY_NOT_VERIFIED","ERROR","PENDING")}
    statuses={s:sum(r.get("fetch_status")==s for r in fetch.values()) for s in ("OK","FETCH_FAILED","IDENTITY_NOT_VERIFIED","ERROR")}
    hashes_after={"clinics":p4a.sha256(args.clinic_db),"research":p4a.sha256(args.research_db),"mhlw":p4a.sha256(args.mhlw_db)}
    signal_ids={int(r["clinic_id"]) for r in treatment_rows if r["hybrid_status"] in POSITIVE}
    current_ids=set()
    # Baseline clinic universe comes from Phase-3 statuses; strict-positive rows are pre-existing safe signal.
    with PHASE3.open(encoding="utf-8-sig",newline="") as f:
        for r in csv.DictReader(f):
            if r["strict_status"] in {"CONFIRMED","REVIEW"} or (r["strict_status"]=="ZERO" and r["hybrid_status"] in {"CONFIRMED","REVIEW"}): current_ids.add(int(r["clinic_id"]))
    new_ids=signal_ids-current_ids
    new_by={s:{int(r["clinic_id"]) for r in treatment_rows if r["hybrid_status"]==s}-current_ids for s in POSITIVE}
    current=2206
    strong=current+len(new_by["CONFIRMED"]|new_by["MENTIONED"])
    sales=current+len(new_ids)
    summary={"seed":SEED,"rule_version":"hybrid-treatment-v1","target_clinics":len(targets),"target_group_counts":group_counts,
      "phase4a_completed_excluded":len(pilot_ids),"fetch_statuses":statuses,"clinic_outcomes":outcomes,
      "clinic_partition_total":sum(outcomes.values()),"treatment_result_rows":len(treatment_rows),"signal_source_summary":source_rows,
      "new_confirmed_clinics":len(new_by["CONFIRMED"]),"new_mentioned_clinics":len(new_by["MENTIONED"]),"new_review_clinics":len(new_by["REVIEW"]),
      "current_safe":current,"hybrid_strong_actual":strong,"hybrid_sales_signal_actual":sales,"strong_to_3000":max(0,3000-strong),"sales_to_3000":max(0,3000-sales),
      "human_audit_sample_rows":len(audit_sample),"human_audit_adjudicated":False,
      "http_requests":history_total+sum(int(r["http_request_count"]) for r in fetch.values()),"pages_fetched":page_count,"link_graph_edges":graph_count,
      "max_pages_per_clinic":int(sqlite3.connect(f"file:{args.cache_db.resolve()}?mode=ro",uri=True).execute("SELECT COALESCE(max(pages_fetched),0) FROM clinic_fetch").fetchone()[0]),
      "cache_integrity_check":integrity,"production_sha256_before":hashes_before,"production_sha256_after":hashes_after,
      "production_hashes_unchanged":hashes_before==hashes_after,"production_db_changed":False,"treatment_sidecar_changed":False,"comdesk_changed":False,
      "phase4c_started":False}
    precision={"human_audit_adjudicated":False,"audit_sample_rows":len(audit_sample),
      "status_counts":{s:sum(r["hybrid_status"]==s for r in audit_sample) for s in quotas},
      "confirmed_precision":None,"mentioned_validity":None,"source_precision":{},
      "note":"Audit rows are stratified and prepared but require human adjudication; no precision claim is made."}
    (args.output_dir/"precision_summary.json").write_text(json.dumps(precision,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    projection={"current_safe":current,"new_confirmed_clinics":len(new_by["CONFIRMED"]),
      "new_mentioned_clinics":len(new_by["MENTIONED"]),"new_review_clinics":len(new_by["REVIEW"]),
      "strong_actual":strong,"sales_signal_actual":sales,"strong_to_3000":max(0,3000-strong),
      "sales_signal_to_3000":max(0,3000-sales),"basis":"Observed Phase-4B outcomes only; failures/unverified are not projected as rescued."}
    (args.output_dir/"population_projection.json").write_text(json.dumps(projection,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (args.output_dir/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    if len({r["clinic_id"] for r in clinic_rows})!=len(targets): raise AssertionError("duplicate clinic_id")
    if sum(outcomes.values())!=len(targets): raise AssertionError("clinic outcome partition mismatch")
    if hashes_before!=hashes_after: raise AssertionError("Production input hash changed")
    print(json.dumps(summary,ensure_ascii=False,indent=2))

if __name__=="__main__": main()
