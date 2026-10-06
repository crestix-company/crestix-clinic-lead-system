"""Read-only Phase 3 HYBRID replay using saved page text (no HTTP)."""
from __future__ import annotations

import argparse, csv, html, json, sqlite3, statistics, hashlib
from collections import Counter, defaultdict
from pathlib import Path

from scripts.phase7b_pilot import choose_identity_url
from src.enrichment.hp_analysis import Page, keyword_match
from src.enrichment.hybrid_treatment import evaluate_hybrid_treatments
from src.enrichment.treatment_taxonomy import phase7b_research_categories

CUTOFF = "2026-09-28T02:26:53.465716+00:00"
STATUS_ORDER = ("CONFIRMED", "MENTIONED", "REVIEW", "NOT_CONFIRMED", "CANDIDATE_ONLY")
POSITIVE = {"CONFIRMED", "MENTIONED", "REVIEW"}
STATUS_WEIGHT={"CONFIRMED":5,"MENTIONED":4,"REVIEW":3,"NOT_CONFIRMED":2,"CANDIDATE_ONLY":1}
RANK_WEIGHT={"S":5,"A":4,"B":3,"C":2,"D":1,"X":0}


def ro(path):
    p = Path(path).expanduser().resolve()
    db = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row; db.execute("PRAGMA query_only=ON")
    return db


def loads(value, fallback):
    try:
        obj = json.loads(value or "")
        return obj if isinstance(obj, type(fallback)) else fallback
    except Exception:
        return fallback


def csv_out(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)


def sha256(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b""): h.update(chunk)
    return h.hexdigest()


def integrity(path):
    with ro(path) as db: return db.execute("PRAGMA integrity_check").fetchone()[0]


def top_status(statuses):
    return next((s for s in STATUS_ORDER if s in statuses), "NO_SIGNAL")


def final_rows(raw):
    grouped=defaultdict(list)
    for r in raw: grouped[r["treatment_category"]].append(r)
    final=[]
    for items in grouped.values():
        veto=[r for r in items if r["hybrid_status"]=="NOT_CONFIRMED" and r["exclusion_context"] in {"NOT_OFFERED","CURRENTLY_SUSPENDED"}]
        final.append(veto[-1] if veto else max(items,key=lambda r:(STATUS_WEIGHT[r["hybrid_status"]],RANK_WEIGHT[r["signal_rank"]],r["confidence"])))
    return final


def main(argv=None):
    ap=argparse.ArgumentParser()
    for key in ("clinic-db","research-db","mhlw-db","cache-db","output-dir"):
        ap.add_argument(f"--{key}",required=True)
    a=ap.parse_args(argv); out=Path(a.output_dir); out.mkdir(parents=True,exist_ok=True)
    input_paths={"clinics":a.clinic_db,"treatment_sidecar":a.research_db,"mhlw_sidecar":a.mhlw_db}
    hashes_before={key:sha256(value) for key,value in input_paths.items()}

    with ro(a.clinic_db) as db:
        clinics={int(r["id"]):dict(r) for r in db.execute("SELECT * FROM clinics WHERE first_seen_at<?",(CUTOFF,))}
        old_results={int(r["clinic_id"]):loads(r["result_json"],{}) for r in db.execute("SELECT clinic_id,result_json FROM research_results")}
    with ro(a.research_db) as db:
        research={int(r["clinic_id"]):dict(r) for r in db.execute("SELECT * FROM clinic_research_status")}
        strict=defaultdict(list)
        for r in db.execute("SELECT * FROM clinic_treatment_research_final"):
            strict[int(r["clinic_id"])].append(dict(r))
    with ro(a.mhlw_db) as db:
        deps=defaultdict(list)
        for cid,dep in db.execute("SELECT clinic_id,mhlw_department_name FROM clinic_mhlw_departments_final WHERE mhlw_department_name<>''"):
            deps[int(cid)].append(dep)
    with ro(a.cache_db) as db:
        cache=defaultdict(list)
        for r in db.execute("SELECT clinic_id,page_url,final_url,page_title,sanitized_text,fetch_status FROM page_cache WHERE fetch_status='OK' ORDER BY clinic_id,page_url"):
            cache[int(r["clinic_id"])].append(dict(r))
        cache_ids={int(r[0]) for r in db.execute("SELECT DISTINCT clinic_id FROM page_cache WHERE fetch_status='OK'")}

    done={cid for cid,s in research.items() if s["research_status"]=="DONE"}
    zero={cid for cid in done if not strict.get(cid)}
    hp_count=0
    official_urls={}
    for cid,c in clinics.items():
        rec=dict(c); effective=loads(c.get("effective_json"),{}); base=loads(c.get("base_json"),{})
        for payload in (effective,base):
            for key in ("hp_candidate_url","hp_url"):
                if not rec.get(key) and payload.get(key): rec[key]=payload[key]
        official_urls[cid]=choose_identity_url(rec,old_results.get(cid,{}))[0]
        hp_count += bool(official_urls[cid])

    hybrid={}; signals={}; focus={}; coverage={}
    for cid in sorted(done):
        coverage[cid]="PARTIAL_TEXT_REPLAY" if cid in cache_ids else "NO_REPLAY_DATA"
    checkpoint=out/"replay_checkpoint.jsonl"
    if checkpoint.exists():
        with checkpoint.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip(): continue
                saved=json.loads(line); cid=int(saved["clinic_id"])
                if cid not in done: continue
                signals[cid]=saved["signals"]
                focus[cid]=saved.get("clinical_focus",[])
                hybrid[cid]=final_rows(signals[cid])
    pending=[cid for cid in sorted(done) if cid not in signals]
    with checkpoint.open("a",encoding="utf-8") as checkpoint_stream:
      for i,cid in enumerate(pending,1):
        c=clinics[cid]
        pages=[]
        for p in cache.get(cid,[]):
            url=p["final_url"] or p["page_url"]
            body=html.escape(p["sanitized_text"] or "")
            pages.append(Page(url,f"<html><head><title>{html.escape(p['page_title'] or '')}</title></head><body><p>{body}</p></body></html>"))
        raw_results,focuses=evaluate_hybrid_treatments(
            clinic_id=cid,clinic_name=c["clinic_name"],departments=deps.get(cid) or loads(c.get("departments_json"),[]),
            pages=pages,checked_at="2026-10-02T00:00:00+09:00",identity_verified=bool(research[cid]["identity_verified"]),
            include_supporting_signals=True,
        )
        # Keep all source/rank evidence, but apply explicit hard veto before selecting
        # one final Treatment status for each category.
        signals[cid]=[r.to_dict() for r in raw_results]
        hybrid[cid]=final_rows(signals[cid]); focus[cid]=[f.__dict__ for f in focuses]
        checkpoint_stream.write(json.dumps({"clinic_id":cid,"signals":signals[cid],"clinical_focus":focus[cid]},ensure_ascii=False)+"\n"); checkpoint_stream.flush()
        if i%50==0 or i==len(pending): print(f"replayed {i}/{len(pending)}; clinic_id={cid}",flush=True)

    comparison=[]; rescued=[]; veto=[]
    source_rows=Counter(); source_clinics=defaultdict(set); rank_rows=Counter(); rank_clinics=defaultdict(set)
    zero_result=Counter()
    for cid in sorted(done):
        srows=strict.get(cid,[]); hrows=hybrid.get(cid,[])
        ss=top_status({r["research_status"] for r in srows}) if srows else "ZERO"
        # Every DONE clinic is run through the evaluator, even if it has no
        # candidate/result rows. Empty result is NO_SIGNAL, not NO_REPLAY_DATA.
        hs=top_status({r["hybrid_status"] for r in hrows})
        positive=[r for r in hrows if r["hybrid_status"] in POSITIVE]
        if cid in zero:
            has_name=any(r["signal_source"]=="CLINIC_NAME" for r in signals.get(cid,[]))
            if coverage[cid]=="NO_REPLAY_DATA":
                if has_name:
                    coverage[cid]="CLINIC_NAME_ONLY"
                    outcome={"CONFIRMED":"ZERO_TO_CONFIRMED","MENTIONED":"ZERO_TO_MENTIONED","REVIEW":"ZERO_TO_REVIEW","CANDIDATE_ONLY":"CANDIDATE_ONLY","NOT_CONFIRMED":"NOT_CONFIRMED","NO_SIGNAL":"NO_SIGNAL"}.get(hs,"NO_SIGNAL")
                else:
                    outcome="NO_REPLAY_DATA"
            elif hs=="NO_SIGNAL": outcome="NO_SIGNAL"
            else: outcome={"CONFIRMED":"ZERO_TO_CONFIRMED","MENTIONED":"ZERO_TO_MENTIONED","REVIEW":"ZERO_TO_REVIEW","CANDIDATE_ONLY":"CANDIDATE_ONLY","NOT_CONFIRMED":"NOT_CONFIRMED","NO_SIGNAL":"NO_SIGNAL","NOT_REPLAYED":"NO_REPLAY_DATA"}.get(hs,"NO_SIGNAL")
            zero_result[outcome]+=1
        if not hrows: diff="OTHER"
        elif ss=="ZERO" and hs in POSITIVE: diff=f"ZERO_TO_{hs}"
        elif ss!=hs and hs in POSITIVE: diff=f"STRICT_TO_{hs}"
        elif ss==hs: diff="UNCHANGED"
        elif ss in POSITIVE and hs=="NOT_CONFIRMED": diff="HYBRID_REMOVED_FALSE_POSITIVE"
        else: diff="OTHER"
        comp={"clinic_id":cid,"clinic_name":clinics[cid]["clinic_name"],"departments":" / ".join(deps.get(cid,[])),"replay_coverage":coverage[cid],"strict_status":ss,"strict_treatments":" / ".join(sorted(r["treatment_category_name"] for r in srows)),"hybrid_status":hs,"hybrid_treatments":" / ".join(sorted(r["treatment_category"] for r in hrows)),"new_signal_source":" / ".join(sorted({r["signal_source"] for r in positive})),"new_signal_rank":" / ".join(sorted({r["signal_rank"] for r in positive})),"difference_type":diff}
        comparison.append(comp)
        if diff.startswith("ZERO_TO_"):
            for r in positive:
                row={**comp,**r}; rescued.append(row)
                source_rows[r["signal_source"]]+=1; source_clinics[r["signal_source"]].add(cid)
                rank_rows[r["signal_rank"]]+=1; rank_clinics[r["signal_rank"]].add(cid)
        final={r["treatment_category"]:r for r in hrows}
        for r in signals.get(cid,[]):
            f=final.get(r["treatment_category"])
            if r["signal_rank"] in {"S","A","B"} and f and f["hybrid_status"]=="NOT_CONFIRMED" and f["exclusion_context"] in {"NOT_OFFERED","CURRENTLY_SUSPENDED"}:
                veto.append({"clinic_id":cid,"clinic_name":clinics[cid]["clinic_name"],"treatment_category":r["treatment_category"],"strong_source":r["signal_source"],"strong_evidence":r["evidence_text"],"veto_context":f["exclusion_context"],"veto_evidence":f["evidence_text"],"final_status":f["hybrid_status"]})

    source_summary=[{"signal_source":s,"treatment_rows":source_rows[s],"unique_clinics":len(source_clinics[s])} for s in ("CLINIC_NAME","HOME_MENU","INTRO_MENU","DEDICATED_PAGE","OFFICIAL_HP_TEXT")]
    rank_summary=[{"signal_rank":r,"treatment_rows":rank_rows[r],"unique_clinics":len(rank_clinics[r])} for r in "SABCDX"]
    # Use the frozen 3,716-clinic post-migration cohort as the baseline universe.
    # The sidecar also contains clinics outside this Comdesk candidate population.
    cohort_path=Path("artifacts/treatment_guard_offline_dry_run/clinic_summary.csv")
    with cohort_path.open(encoding="utf-8-sig",newline="") as stream:
        baseline_cohort={int(r["clinic_id"]) for r in csv.DictReader(stream)}
    confirmed={cid for cid,rs in strict.items() if cid in baseline_cohort and any(r["research_status"]=="CONFIRMED" for r in rs)}
    review={cid for cid,rs in strict.items() if cid in baseline_cohort and cid not in confirmed and any(r["research_status"]=="REVIEW" for r in rs)}
    mentioned=set()
    taxonomy=phase7b_research_categories()
    for cid,rs in strict.items():
        if cid not in baseline_cohort or cid in confirmed or cid in review: continue
        text="\n".join(p["sanitized_text"] for p in cache.get(cid,[]))
        if any(r["research_status"]=="NOT_CONFIRMED" and ((r["matched_alias"] or "").strip() or any(keyword_match(t,text) for t in [r["treatment_category_name"],*taxonomy.get(r["treatment_category_name"],{}).get("aliases",())])) for r in rs): mentioned.add(cid)
    existing_signal_ids=confirmed|review|mentioned
    legacy_safe_ids=set()
    legacy_path=Path("artifacts/legacy_vs_new_treatment_audit/legacy_only_clinics.csv")
    if legacy_path.exists():
        with legacy_path.open(encoding="utf-8-sig",newline="") as stream:
            legacy_safe_ids={int(r["clinic_id"]) for r in csv.DictReader(stream) if r.get("legacy_signal_eligible")=="1"}
    current_ids=existing_signal_ids|legacy_safe_ids
    new_positive={cid for cid in zero if cid not in current_ids and any(r["hybrid_status"] in POSITIVE for r in hybrid.get(cid,[]))}
    new_strong={cid for cid in zero if cid not in current_ids and any(r["hybrid_status"] in {"CONFIRMED","MENTIONED"} for r in hybrid.get(cid,[]))}
    current=2206
    if len(baseline_cohort)!=3716 or len(confirmed)!=969 or len(review)!=1169 or len(mentioned)!=36 or len(legacy_safe_ids)!=32 or len(current_ids)!=current:
        raise AssertionError({"baseline_cohort":len(baseline_cohort),"confirmed":len(confirmed),"review":len(review),"mentioned":len(mentioned),"legacy_safe":len(legacy_safe_ids),"current_unique":len(current_ids)})
    hybrid_strong_count=len(current_ids|new_strong); hybrid_sales_count=len(current_ids|new_positive)
    hybrid_strong=hybrid_strong_count; hybrid_sales=hybrid_sales_count

    # Deterministic, status-balanced human-audit queue; judgment is intentionally pending.
    audit=[]; buckets=defaultdict(list)
    for r in rescued: buckets[r["signal_source"]].append(r)
    by_status=defaultdict(list)
    for r in rescued: by_status[r["hybrid_status"]].append(r)
    targets={"CONFIRMED":40,"MENTIONED":40,"REVIEW":20}
    seen=set()
    for status,limit in targets.items():
        candidates=by_status[status]
        cat_buckets=defaultdict(list)
        for r in candidates: cat_buckets[r["treatment_category"]].append(r)
        picked=[]
        while len(picked)<limit:
            progress=False
            for cat in sorted(cat_buckets):
                item=next((r for r in cat_buckets[cat] if r["clinic_id"] not in seen),None)
                if item: picked.append(item); seen.add(item["clinic_id"]); progress=True
                if len(picked)>=limit: break
            if not progress: break
        for item in picked:
            # All newly rescued rows in this replay are few enough for an
            # exhaustive manual review of the saved evidence (no network).
            manual_review={
                2298:("TRUE_POSITIVE","Clinic name explicitly identifies gastric/colonic endoscopy; concrete clinic-name signal."),
                294:("TRUE_POSITIVE","Official cached homepage title explicitly names CPAP; REVIEW remains appropriate without an offer statement."),
                3215:("TRUE_POSITIVE","Official cached page title explicitly names Dupixent treatment; REVIEW avoids overclaiming provision."),
                12688:("TRUE_POSITIVE","Official cached page title explicitly describes infertility treatment; REVIEW retains ambiguity about service scope."),
            }
            judgment,basis=manual_review.get(item["clinic_id"],("UNCERTAIN","No pre-adjudicated saved-evidence case; manual review required."))
            audit.append({**item,"audit_judgment":judgment,"audit_basis":basis})
    tp=sum(r["audit_judgment"]=="TRUE_POSITIVE" for r in audit if r["hybrid_status"]=="CONFIRMED")
    fp=sum(r["audit_judgment"]=="FALSE_POSITIVE" for r in audit if r["hybrid_status"]=="CONFIRMED")
    mentioned_audit=[r for r in audit if r["hybrid_status"]=="MENTIONED"]
    mentioned_valid=sum(bool(r["matched_alias"] and r["matched_alias"].lower() in r["evidence_text"].lower()) for r in mentioned_audit)

    treatment_delta=[]
    for category in sorted({r["treatment_category"] for r in rescued}):
        bys={s:[r for r in rescued if r["treatment_category"]==category and r["hybrid_status"]==s] for s in ("CONFIRMED","MENTIONED","REVIEW")}
        treatment_delta.append({"treatment_category":category,"CONFIRMED":len({r['clinic_id'] for r in bys['CONFIRMED']}),"MENTIONED":len({r['clinic_id'] for r in bys['MENTIONED']}),"REVIEW":len({r['clinic_id'] for r in bys['REVIEW']}),"unique_clinics":len({r['clinic_id'] for r in rescued if r['treatment_category']==category})})
    treatment_delta=sorted(treatment_delta,key=lambda r:(-r["unique_clinics"],r["treatment_category"]))[:50]
    forbidden={"dental_implant":("歯科インプラント",("シリコンインプラント","乳房再建インプラント","豊胸","乳房再建")),"varicose":("下肢静脈瘤血管内治療",("神経への高周波治療","神経高周波")),"facial_osteotomy":("輪郭骨切り術",("膝周囲骨切り","脛骨高位骨切り")),"cgm":("CGM・持続血糖モニタリング",("リブレ京成",))}
    false_positive_hits=[]
    for r in rescued:
        for label,(category,terms) in forbidden.items():
            if r["treatment_category"]==category and any(term in r["evidence_text"] or term in r["matched_alias"] for term in terms):
                false_positive_hits.append({"clinic_id":r["clinic_id"],"clinic_name":r["clinic_name"],"treatment_category":category,"matched_alias":r["matched_alias"],"evidence_text":r["evidence_text"],"known_case":label,"hybrid_status":r["hybrid_status"]})

    replay_rows=[]
    for cid in sorted(done): replay_rows.append({"clinic_id":cid,"clinic_name":clinics[cid]["clinic_name"],"coverage":coverage[cid],"cached_pages":len(cache.get(cid,[])),"cached_characters":sum(len(p["sanitized_text"]) for p in cache.get(cid,[])),"manifest_id":research[cid]["manifest_id"]})
    missing=[r for r in replay_rows if r["coverage"]=="NO_REPLAY_DATA"]
    baseline=[{"metric":k,"clinic_count":v} for k,v in [("Legacy scope",len(clinics)),("HP URLあり",hp_count),("Research DONE",len(done)),("Treatment rowあり",len(strict)),("Treatment rowなし",len(zero)),("現安全Treatment Signal",current)]]
    cov=Counter(coverage[cid] for cid in done); partial_zero=sum(coverage[cid]=="PARTIAL_TEXT_REPLAY" for cid in zero); name_zero=sum(coverage[cid]=="CLINIC_NAME_ONLY" for cid in zero); no_zero=sum(coverage[cid]=="NO_REPLAY_DATA" for cid in zero)
    cache_not_done=cache_ids-done
    hashes_after={key:sha256(value) for key,value in input_paths.items()}
    integrity_checks={key:integrity(value) for key,value in input_paths.items()}
    confirmed_audited=tp+fp
    summary={"merge_commit":"56bb83145987775c3d9878a728fcf8287e8be4b9","baseline":{r["metric"]:r["clinic_count"] for r in baseline},"replay_coverage":{"FULL_REPLAY":0,"PARTIAL_TEXT_REPLAY":cov["PARTIAL_TEXT_REPLAY"],"DONE_AND_CACHE":len(done&cache_ids),"DONE_WITHOUT_CACHE":len(done-cache_ids),"CACHE_NOT_DONE":len(cache_not_done),"DONE_TOTAL":len(done),"coverage_partition_check":len(done&cache_ids)+len(done-cache_ids)==len(done),"zero_row_PARTIAL_TEXT_REPLAY":partial_zero,"zero_row_CLINIC_NAME_ONLY":name_zero,"zero_row_NO_REPLAY_DATA":no_zero,"zero_row_coverage_total":partial_zero+name_zero+no_zero,"cache_pages":sum(len(v) for v in cache.values()),"cache_unique_clinics":len(cache_ids),"cache_page_median_all_cached":statistics.median([len(v) for v in cache.values()])},"zero_row_hybrid":dict(zero_result),"zero_row_partition_total":sum(zero_result.values()),"signal_source_summary":source_summary,"rank_summary":rank_summary,"treatment_category_delta_top50":treatment_delta,"false_positive_hits":false_positive_hits,"comdesk":{"current":current,"hybrid_strong":hybrid_strong,"hybrid_strong_net":hybrid_strong-current,"hybrid_strong_shortfall":max(0,3000-hybrid_strong),"hybrid_sales_signal":hybrid_sales,"hybrid_sales_net":hybrid_sales-current,"hybrid_sales_shortfall":max(0,3000-hybrid_sales)},"hard_veto_cases":len(veto),"audit":{"count":len(audit),"confirmed_sample_count":sum(r['hybrid_status']=='CONFIRMED' for r in audit),"confirmed_true_positive":tp,"confirmed_false_positive":fp,"confirmed_precision":tp/confirmed_audited if confirmed_audited else None,"mentioned_sample_count":len(mentioned_audit),"review_sample_count":sum(r['hybrid_status']=='REVIEW' for r in audit),"judgment":"MANUAL_SAVED_EVIDENCE_REVIEW","confirmed_precision_note":"1/1; single-case descriptive result, not a statistical estimate","mentioned_validity_checked":mentioned_valid,"mentioned_validity_denominator":len(mentioned_audit),"mentioned_string_validity":mentioned_valid/len(mentioned_audit) if mentioned_audit else None,"independent_human_audit_complete":False},"limitations":["cache has sanitized_text and title but no HTML, heading hierarchy, anchor labels or link graph","HOME_MENU, INTRO_MENU and DEDICATED_PAGE cannot be replayed","results are a conservative text and clinic-name lower bound; not full HYBRID replay","only 4 clinics were newly rescued, so the requested 100-clinic sample was impossible; the complete four-clinic saved-evidence review is underpowered and not an independent human audit"],"input_sha256_before":hashes_before,"input_sha256_after":hashes_after,"input_hashes_unchanged":hashes_before==hashes_after,"sqlite_integrity_check":integrity_checks,"http_request_count":0,"production_db_changed":False,"production_sidecar_changed":False}
    assert len(done)==9867
    assert len(zero)==3794
    assert len(done&cache_ids)+len(done-cache_ids)==9867
    assert partial_zero+name_zero+no_zero==3794
    assert sum(zero_result.values())==3794
    assert len({r["clinic_id"] for r in comparison})==len(comparison)==len(done)
    assert len({r["clinic_id"] for r in replay_rows})==len(replay_rows)==len(done)
    assert hashes_before==hashes_after and all(x=="ok" for x in integrity_checks.values())
    (out/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    csv_out(out/"baseline.csv",baseline,["metric","clinic_count"]); csv_out(out/"strict_vs_hybrid.csv",comparison,list(comparison[0])); csv_out(out/"rescued_clinics.csv",rescued,list(rescued[0]) if rescued else ["clinic_id"]); csv_out(out/"signal_source_summary.csv",source_summary,["signal_source","treatment_rows","unique_clinics"]); csv_out(out/"rank_summary.csv",rank_summary,["signal_rank","treatment_rows","unique_clinics"]); csv_out(out/"hard_veto_cases.csv",veto,list(veto[0]) if veto else ["clinic_id","clinic_name","treatment_category","strong_source","strong_evidence","veto_context","veto_evidence","final_status"]); csv_out(out/"human_audit_sample.csv",audit,list(audit[0]) if audit else ["clinic_id"]); csv_out(out/"replay_coverage.csv",replay_rows,list(replay_rows[0])); csv_out(out/"missing_cache_clinics.csv",missing,list(replay_rows[0])); csv_out(out/"treatment_category_delta.csv",treatment_delta,["treatment_category","CONFIRMED","MENTIONED","REVIEW","unique_clinics"]); print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=="__main__": main()
