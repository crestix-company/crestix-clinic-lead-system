"""Offline audit of DONE clinics with no post-guard Treatment rows."""
from __future__ import annotations

import argparse, csv, json, re, sqlite3, statistics
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

from scripts.phase7b_pilot import choose_identity_url
from src.enrichment.candidate_map import department_candidates
from src.enrichment.treatment_taxonomy import phase7b_research_categories

CUTOFF = "2026-09-28T02:26:53.465716+00:00"
MEDICAL_SUFFIXES = ("治療", "療法", "手術", "検査", "注射", "点滴", "内視鏡", "レーザー", "処置", "リハビリ", "外来")
NEGATIVE = ("実施していません", "行っていません", "対応していません", "他院", "紹介", "学会", "経歴", "ブログ", "記事")
PAGE_POSITIVE = ("medical", "treatment", "service", "surgery", "exam", "menu", "診療", "治療", "手術", "検査", "施術")


def ro(path):
    p = Path(path).expanduser().resolve()
    db = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def load(value, fallback):
    try:
        obj = json.loads(value or "")
        return obj if isinstance(obj, type(fallback)) else fallback
    except Exception:
        return fallback


def host(url):
    return urlparse(url or "").netloc.lower().removeprefix("www.")


def write_csv(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(rows)


def context(text, term, radius=90):
    pos = text.lower().find(term.lower())
    return text[max(0, pos-radius):pos+len(term)+radius].replace("\n", " ") if pos >= 0 else ""


def main(argv=None):
    ap = argparse.ArgumentParser()
    for key in ("clinic-db", "research-db", "mhlw-db", "cache-root", "output-dir"):
        ap.add_argument(f"--{key}", required=True)
    a = ap.parse_args(argv); out = Path(a.output_dir); out.mkdir(parents=True, exist_ok=True)
    taxonomy = phase7b_research_categories()
    terms = {}
    for name, definition in taxonomy.items():
        vals = [name, *definition.get("aliases", ()), *definition.get("source_sales_items", ())]
        terms[name] = sorted({str(v).strip() for v in vals if str(v).strip()}, key=len, reverse=True)

    with ro(a.research_db) as db:
        statuses = {int(r["clinic_id"]): dict(r) for r in db.execute("SELECT * FROM clinic_research_status")}
        row_ids = {int(r[0]) for r in db.execute("SELECT DISTINCT clinic_id FROM clinic_treatment_research_final")}
    target = {cid for cid, s in statuses.items() if s["research_status"] == "DONE" and cid not in row_ids}

    with ro(a.mhlw_db) as db:
        mhlw = defaultdict(list)
        for cid, dep in db.execute("SELECT clinic_id,mhlw_department_name FROM clinic_mhlw_departments_final WHERE mhlw_department_name<>''"):
            mhlw[int(cid)].append(dep)

    with ro(a.clinic_db) as db:
        clinics = {int(r["id"]): dict(r) for r in db.execute("SELECT * FROM clinics WHERE first_seen_at<?", (CUTOFF,))}
        old_results = {int(r["clinic_id"]): load(r["result_json"], {}) for r in db.execute("SELECT clinic_id,result_json FROM research_results")}
        old_pages = defaultdict(list)
        for r in db.execute("SELECT clinic_id,url,page_json FROM hp_pages"):
            if int(r["clinic_id"]) in target:
                p = load(r["page_json"], {})
                old_pages[int(r["clinic_id"])].append({"url": r["url"], "title": p.get("title", ""), "text": p.get("text", ""), "headings": p.get("headings", "")})

    # Worker cache inventory. Test caches are ignored unless located below the supplied root.
    cache_paths = sorted(Path(a.cache_root).expanduser().rglob("cache.sqlite3")) if Path(a.cache_root).expanduser().exists() else []
    pages = defaultdict(list)
    for path in cache_paths:
        with ro(path) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='page_cache'").fetchone(): continue
            for r in db.execute("SELECT clinic_id,page_url,final_url,page_title,sanitized_text,fetch_status FROM page_cache"):
                if int(r["clinic_id"]) in target:
                    pages[int(r["clinic_id"])].append(dict(r))

    # Historical guard report is evidence of why rows disappeared, not a replacement page cache.
    guard_zero = set()
    guard_summary = Path("artifacts/treatment_guard_offline_dry_run/clinic_summary.csv")
    if guard_summary.exists():
        with guard_summary.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if int(r.get("remaining_candidate_count") or 0) == 0 and int(r.get("removed_candidate_count") or 0) > 0:
                    guard_zero.add(int(r["clinic_id"]))

    rows=[]; aliases=[]; gaps=[]; insufficient=[]; phrase_counts=Counter(); phrase_examples={}
    for cid in sorted(target):
        c=clinics[cid]; effective=load(c.get("effective_json"),{}); base=load(c.get("base_json"),{})
        record=dict(c)
        for payload in (effective,base):
            for k in ("hp_candidate_url","hp_url"):
                if not record.get(k) and payload.get(k): record[k]=payload[k]
        official, source=choose_identity_url(record, old_results.get(cid, {}))
        depts=sorted(set(mhlw.get(cid,[]) or load(c.get("departments_json"),[])))
        worker=pages.get(cid,[])
        legacy=old_pages.get(cid,[])
        usable=[]
        for p in worker:
            usable.append({"url":p.get("final_url") or p.get("page_url"),"title":p.get("page_title", ""),"text":p.get("sanitized_text", ""),"kind":"WORKER_CACHE"})
        # Legacy hp_pages can support a conservative rescue only when it is the official host.
        for p in legacy:
            if host(p["url"]) and host(p["url"]) == host(official): usable.append({**p,"kind":"LEGACY_OFFICIAL_CACHE"})
        text="\n".join(p.get("text","") for p in usable)
        hit=[]
        for category, ts in terms.items():
            for term in ts:
                if term.lower() in text.lower():
                    ctx=context(text,term)
                    if not any(n in ctx for n in NEGATIVE):
                        hit.append((category,term,ctx)); break
        mapped=department_candidates(depts)
        has_medical_page=any(
            any(k.lower() in (p.get("url", "") + " " + p.get("title", "")).lower() for k in PAGE_POSITIVE)
            for p in usable
        )
        phrases=[]
        for sentence in re.split(r"[。！？!?\n]", text):
            if any(n in sentence for n in NEGATIVE): continue
            for m in re.finditer(r"[^、。！？\s]{2,24}(?:"+"|".join(MEDICAL_SUFFIXES)+r")", sentence):
                phrase=m.group(0)[-30:]; phrases.append(phrase); phrase_counts[phrase]+=1; phrase_examples.setdefault(phrase,(cid,sentence[:240]))
        if cid in guard_zero: reason="GUARD_DROPPED"
        elif not usable: reason="CACHE_PAGE_COVERAGE_INSUFFICIENT"
        elif hit: reason="TREATMENT_ALIAS_MISSED"
        elif not mapped and depts: reason="CANDIDATE_MAP_GAP"
        elif phrases: reason="LIKELY_TREATMENT_OUTSIDE_TAXONOMY"
        elif has_medical_page and any(x in text for x in ("糖尿病","高血圧","アトピー","緑内障","疾患")): reason="DISEASE_ONLY"
        elif not has_medical_page: reason="DEPARTMENT_ONLY"
        else: reason="NO_TREATMENT_SIGNAL"
        eligible=bool(official and c.get("active") and not c.get("uuid") and not c.get("merged_into") and not c.get("merge_hold") and statuses[cid].get("identity_verified"))
        # Conservative: an explicit taxonomy hit in official cached text, outside guard-dropped clinics.
        rescue="SAFE_RESCUE" if reason=="TREATMENT_ALIAS_MISSED" and eligible else ("LIKELY_RESCUE_NEEDS_RULE_CHANGE" if reason in ("LIKELY_TREATMENT_OUTSIDE_TAXONOMY","CANDIDATE_MAP_GAP") else ("REQUIRES_REFETCH" if reason=="CACHE_PAGE_COVERAGE_INSUFFICIENT" else "NOT_RESCUABLE"))
        row={"clinic_id":cid,"clinic_name":c.get("clinic_name",""),"departments":" / ".join(depts),"official_hp_url":official,"url_source":source,"reason":reason,"rescue_class":rescue,"worker_cache_pages":len(worker),"legacy_official_cache_pages":len(usable)-len(worker),"cached_characters":len(text),"department_candidate_count":len(mapped),"matched_treatments":" / ".join(sorted({h[0] for h in hit})),"matched_text":" / ".join(h[1] for h in hit),"source_page":next((p["url"] for p in usable if hit and hit[0][1].lower() in p.get("text","").lower()),""),"context":hit[0][2] if hit else "","classification_note":"worker cache unavailable" if not worker else "worker cache inspected"}
        rows.append(row)
        if hit: aliases.append(row)
        if reason=="CANDIDATE_MAP_GAP": gaps.append(row)
        if reason=="CACHE_PAGE_COVERAGE_INSUFFICIENT": insufficient.append(row)

    # Four official-URL clinics absent from research status.
    missing=[]
    manifest_ids={p.parent.name for p in cache_paths}
    for cid,c in clinics.items():
        record=dict(c); effective=load(c.get("effective_json"),{}); base=load(c.get("base_json"),{})
        for payload in (effective,base):
            for k in ("hp_candidate_url","hp_url"):
                if not record.get(k) and payload.get(k): record[k]=payload[k]
        url,src=choose_identity_url(record,old_results.get(cid, {}))
        if url and cid not in statuses:
            missing.append({"clinic_id":cid,"clinic_name":c.get("clinic_name",""),"hp_url_effective":url,"url_source":src,"active":c.get("active"),"exclude_reason":c.get("exclude_reason") or "","hp_status":c.get("hp_status") or "","maps_website_url":c.get("maps_website_url") or "","hp_url":c.get("hp_url") or "","choose_identity_url_result":url,"research_manifest_exists":int(any(str(cid) in p.name for p in cache_paths)),"audit_note":"local manifest/cache artifact unavailable"})

    phrase_rows=[]
    for phrase,n in phrase_counts.most_common(100):
        cid,ex=phrase_examples[phrase]
        represented=any(phrase.lower() in t.lower() or t.lower() in phrase.lower() for ts in terms.values() for t in ts)
        disposition="既存taxonomyで表現可能" if represented else "要人手確認"
        phrase_rows.append({"phrase":phrase,"clinic_count_or_occurrences":n,"example_clinic_id":cid,"example_context":ex,"disposition":disposition})
    counts=Counter(r["reason"] for r in rows); rescues=Counter(r["rescue_class"] for r in rows)
    samples=[]
    for reason in ("TREATMENT_ALIAS_MISSED","LIKELY_TREATMENT_OUTSIDE_TAXONOMY","DISEASE_ONLY","DEPARTMENT_ONLY","CACHE_PAGE_COVERAGE_INSUFFICIENT","CANDIDATE_MAP_GAP","GUARD_DROPPED","NO_TREATMENT_SIGNAL"):
        samples.extend([r for r in rows if r["reason"]==reason][:20])
    page_counts=[len(pages.get(cid,[])) for cid in target if pages.get(cid)]
    hp_count = 0
    for cid, c in clinics.items():
        record = dict(c); effective=load(c.get("effective_json"),{}); base=load(c.get("base_json"),{})
        for payload in (effective, base):
            for k in ("hp_candidate_url", "hp_url"):
                if not record.get(k) and payload.get(k): record[k]=payload[k]
        hp_count += bool(choose_identity_url(record, old_results.get(cid, {}))[0])
    summary={"fixed_counts":{"legacy_scope":len(clinics),"hp_url_available":hp_count,"research_status":len(statuses),"done":sum(s["research_status"]=="DONE" for s in statuses.values()),"fetch_failed":sum(s["research_status"]=="FETCH_FAILED" for s in statuses.values()),"treatment_row_clinics":len(row_ids),"done_no_rows":len(target),"hp_url_without_research":len(missing)},"cache":{"cache_db_count":len(cache_paths),"clinics_with_worker_cache":len(pages),"clinics_without_worker_cache":len(target-set(pages)),"pages_min":min(page_counts) if page_counts else None,"pages_median":statistics.median(page_counts) if page_counts else None,"pages_avg":statistics.mean(page_counts) if page_counts else None,"pages_max":max(page_counts) if page_counts else None,"sanitized_text_characters":sum(len(p.get("sanitized_text", "")) for ps in pages.values() for p in ps),"legacy_official_cache_clinics":sum(bool(old_pages.get(cid)) for cid in target),"limitation":"production worker cache and manifests are absent from supplied/local paths"},"exclusive_reason_counts":dict(counts),"rescue_counts":dict(rescues),"current_safe":2206,"new_safe":2206+rescues["SAFE_RESCUE"],"http_request_count":0}
    fields=list(rows[0]); write_csv(out/"clinic_classification.csv",rows,fields); write_csv(out/"safe_rescue.csv",[r for r in rows if r["rescue_class"]=="SAFE_RESCUE"],fields); write_csv(out/"alias_misses.csv",aliases,fields); write_csv(out/"candidate_map_gaps.csv",gaps,fields); write_csv(out/"cache_insufficient.csv",insufficient,fields); write_csv(out/"samples.csv",samples,fields)
    write_csv(out/"taxonomy_gap_phrases.csv",phrase_rows,["phrase","clinic_count_or_occurrences","example_clinic_id","example_context","disposition"])
    write_csv(out/"hp_url_without_research_4.csv",missing,["clinic_id","clinic_name","hp_url_effective","url_source","active","exclude_reason","hp_status","maps_website_url","hp_url","choose_identity_url_result","research_manifest_exists","audit_note"])
    (out/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))

if __name__ == "__main__": main()
