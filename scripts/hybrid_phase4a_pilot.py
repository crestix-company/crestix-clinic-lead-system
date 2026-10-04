"""Bounded, resumable Phase 4-A structured official-HP pilot.

Production databases are opened read-only. Network fetches are limited to the
deterministic 500-clinic sample; HTML and extracted structure go to a separate
pilot SQLite cache, never to a Worker or Production cache.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlparse

from scripts.phase7b_pilot import choose_identity_url
from src.enrichment.candidate_map import department_candidates
from src.enrichment.hp_analysis import Page, canonical_page, host, identity, is_official_candidate, keyword_match, page_soup
from src.enrichment.hybrid_treatment import evaluate_hybrid_treatments
from src.enrichment.safe_web import EXCLUDE, PRIORITY, SafeFetcher, PinnedTransport, WebError, crawl
from src.enrichment.treatment_taxonomy import phase7b_research_categories

SEED = "hybrid-phase4a-structured-pilot-20261002-v1"
QUOTAS = {"NO_SIGNAL": 300, "NO_REPLAY_DATA": 125, "CANDIDATE_ONLY": 75}
PHASE3_CSV = Path("artifacts/hybrid_phase3/strict_vs_hybrid.csv")
ARTIFACT_DIR = Path("artifacts/hybrid_phase4_pilot")
PILOT_DIR = Path("data/hybrid_phase4_pilot")
PRODUCTION_CLINIC_DB = Path("/Users/maekawahiroyuki/CrestixData/clinic-lead/clinics.sqlite3")
PRODUCTION_RESEARCH_DB = Path("/Users/maekawahiroyuki/CrestixData/clinic-lead/treatment_research_final.sqlite3")
MHLW_DB = Path("mhlw_dry_run/clinic_mhlw_departments_final.sqlite3")
POSITIVE = {"CONFIRMED", "MENTIONED", "REVIEW"}
STATUS_ORDER = ("CONFIRMED", "MENTIONED", "REVIEW", "NOT_CONFIRMED", "CANDIDATE_ONLY")
PAGE_TYPE = re.compile(r"(?:medical|service|treatment|department|guide|shinryo|menu|gairai|surgery|exam)", re.I)
INTRO_TEXT = re.compile(r"医院紹介|クリニック紹介|当院について|診療案内|診療内容|治療内容|専門外来|medical|service|treatment|department|guide", re.I)
ARTICLE_PATH = re.compile(r"/(?:blog|column|news|topics?|article|information|notice)(?:/|$)", re.I)

# Exhaustive saved-evidence audit of the 125 pilot Treatment rows. These labels
# are a manual review aid, not a replacement for independent client-side audit.
AUDIT_CONFIRMED_FALSE = {
    (4317, "ニキビ跡施術"): "Homepage card links to a dated physician blog article, not a Treatment menu.",
    (6681, "歯列矯正"): "Matched 矯正治療 is explicitly 巻き爪矯正 in dermatology text, not dental orthodontics.",
    (6360, "体外受精（IVF）"): "Official sentence contrasts the clinic's male-factor approach with IVF rather than offering IVF.",
}
AUDIT_MENTIONED_FALSE = {
    (1001, "体外受精（IVF）"): "IVF string is part of a staff member's conference award/publication history.",
    (1953, "オルソケラトロジー"): "String only supports a doctor's credential (certified physician), not clinic service evidence.",
    (3359, "胃カメラ検査"): "Endoscopy is described in a doctor's previous-work history at another clinic.",
    (13320, "ED治療"): "String appears only inside an HTML comment/disabled carousel content, not visible page text.",
    (6360, "顕微授精（ICSI）"): "General treatment-indication explanation on a semen-test page; no clinic offer evidence.",
}
AUDIT_MENTIONED_UNCERTAIN = {
    (2466, "CGM・持続血糖モニタリング"): "Reservation text addresses people already using Libre; it does not clearly establish this clinic's CGM service.",
    (5766, "脂肪吸引"): "Diet-clinic checkbox mentions cosmetic-surgery options; provider/service relationship is unclear.",
}
AUDIT_REVIEW_UNCERTAIN = {
    (383, "顕微授精（ICSI）"): "General ART definition; treatment relevance is clear but provision is not established.",
    (5944, "ED治療"): "ED appears in a gynecology clinic's diagnosis guide; service scope needs human confirmation.",
    (11867, "CGM・持続血糖モニタリング"): "General diabetes explainer describes CGM rather than this clinic's own provision.",
}


class CountingTransport:
    def __init__(self):
        self.transport = PinnedTransport()
        self.request_count = 0

    def get(self, *args, **kwargs):
        self.request_count += 1
        return self.transport.get(*args, **kwargs)


def open_ro(path: Path):
    db = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(value, fallback):
    try:
        out = json.loads(value or "")
        return out if isinstance(out, type(fallback)) else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _phase3_groups(path: Path) -> dict[int, str]:
    groups = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row["strict_status"] != "ZERO":
                continue
            coverage, status = row["replay_coverage"], row["hybrid_status"]
            if coverage == "NO_REPLAY_DATA":
                group = "NO_REPLAY_DATA"
            elif status == "NO_SIGNAL":
                group = "NO_SIGNAL"
            elif status == "CANDIDATE_ONLY":
                group = "CANDIDATE_ONLY"
            else:
                continue
            groups[int(row["clinic_id"])] = group
    counts = {g: sum(v == g for v in groups.values()) for g in QUOTAS}
    expected = {"NO_SIGNAL": 2900, "NO_REPLAY_DATA": 769, "CANDIDATE_ONLY": 107}
    if counts != expected:
        raise AssertionError(f"Phase 3 group mismatch: {counts}, expected {expected}")
    return groups


def _load_records(clinic_db: Path, research_db: Path, mhlw_db: Path, groups: dict[int, str]):
    with open_ro(clinic_db) as cdb, open_ro(research_db) as rdb, open_ro(mhlw_db) as mdb:
        for name, db in (("clinics", cdb), ("research", rdb), ("mhlw", mdb)):
            status = db.execute("PRAGMA integrity_check").fetchone()[0]
            if status != "ok":
                raise RuntimeError(f"{name} integrity_check={status}")
        old = {int(r["clinic_id"]): read_json(r["result_json"], {})
               for r in cdb.execute("SELECT clinic_id,result_json FROM research_results")}
        clinics = {int(r["id"]): dict(r) for r in cdb.execute("SELECT * FROM clinics")}
        research = {int(r["clinic_id"]): dict(r) for r in rdb.execute("SELECT * FROM clinic_research_status")}
        deps = {}
        for cid, dep in mdb.execute("SELECT clinic_id,mhlw_department_name FROM clinic_mhlw_departments_final WHERE mhlw_department_name<>''"):
            deps.setdefault(int(cid), []).append(dep)
    categories = phase7b_research_categories()
    records = []
    for cid, group in groups.items():
        c = clinics.get(cid)
        rs = research.get(cid)
        if not c or not rs or rs["research_status"] != "DONE":
            raise AssertionError(f"sample clinic is not Research DONE: {cid}")
        rec = dict(c)
        for payload in (read_json(c.get("effective_json"), {}), read_json(c.get("base_json"), {})):
            for key in ("hp_candidate_url", "hp_url"):
                if not rec.get(key) and payload.get(key):
                    rec[key] = payload[key]
        url, source = choose_identity_url(rec, old.get(cid, {}))
        if url and not is_official_candidate(url):
            url = ""
        formal = sorted(set(deps.get(cid, [])))
        record = {
            "clinic_id": cid, "clinic_name": c["clinic_name"], "sampling_group": group,
            "prefecture": c.get("prefecture", ""), "formal_departments": formal,
            "candidate_categories": sorted(department_candidates(formal)),
            "selected_identity_url": url, "url_source": source if url else "NO_OFFICIAL_URL",
        }
        records.append(record)
    return records, categories


def _stable(value: str) -> str:
    return hashlib.sha256(f"{SEED}|{value}".encode()).hexdigest()


def build_sample(records: list[dict], quotas=QUOTAS):
    sampled = []
    group_stats = {}
    for group, quota in quotas.items():
        pool = [r for r in records if r["sampling_group"] == group and r["selected_identity_url"]]
        counts = {"pref": {}, "domain": {}, "dept": {}, "category": {}}
        picked = []
        while pool and len(picked) < quota:
            def score(r):
                domain = host(r["selected_identity_url"])
                dimensions = [
                    [r["prefecture"] or "(unknown)"],
                    [domain or "(unknown)"],
                    r["formal_departments"] or ["(unknown)"],
                    r["candidate_categories"] or ["(no-map-candidate)"],
                ]
                keys = ("pref", "domain", "dept", "category")
                balance = sum(sum(1 / (1 + counts[k].get(x, 0)) for x in values) / len(values)
                              for k, values in zip(keys, dimensions))
                return (-balance, _stable(f"{group}|{r['clinic_id']}"))
            row = min(pool, key=score)
            pool.remove(row)
            picked.append(row)
            for k, values in zip(("pref", "domain", "dept", "category"),
                                 ([row["prefecture"] or "(unknown)"], [host(row["selected_identity_url"]) or "(unknown)"],
                                  row["formal_departments"] or ["(unknown)"], row["candidate_categories"] or ["(no-map-candidate)"])):
                for value in values:
                    counts[k][value] = counts[k].get(value, 0) + 1
        group_stats[group] = {"eligible_with_official_url": sum(r["sampling_group"] == group and bool(r["selected_identity_url"]) for r in records),
                              "requested": quota, "selected": len(picked),
                              "no_official_url": sum(r["sampling_group"] == group and not r["selected_identity_url"] for r in records)}
        if len(picked) != quota:
            raise RuntimeError(f"insufficient official-URL clinics for {group}: {group_stats[group]}")
        sampled.extend(picked)
    sampled.sort(key=lambda r: (r["sampling_group"], r["clinic_id"]))
    if len({r["clinic_id"] for r in sampled}) != sum(quotas.values()):
        raise AssertionError("sample clinic_id duplicate")
    return sampled, group_stats


def _page_type(page: Page) -> str:
    path = urlparse(page.url).path or "/"
    headings = " ".join(x.get_text(" ", strip=True) for x in page_soup(page).select("h1,h2")[:3])
    joined = f"{path} {page.title} {headings}"
    if path in {"", "/"} or re.fullmatch(r"/index\.html?", path, re.I):
        return "HOME"
    if ARTICLE_PATH.search(path) or re.search(r"ブログ|コラム|ニュース|お知らせ", joined):
        return "ARTICLE"
    if re.search(r"アクセス|交通|地図|問い合わせ|連絡先", joined, re.I):
        return "ACCESS_CONTACT"
    if INTRO_TEXT.search(joined) or PAGE_TYPE.search(path):
        return "INTRO_OR_TREATMENT"
    return "OTHER"


def _init_cache(path: Path, seed: str, sample_hash: str):
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
    """)
    existing = dict(db.execute("SELECT key,value FROM run_meta"))
    expected = {"seed": seed, "sample_sha256": sample_hash, "max_pages_per_clinic": "5"}
    if existing and any(existing.get(k) != v for k, v in expected.items()):
        db.close()
        raise RuntimeError("existing pilot cache belongs to a different sample/config; refusing to overwrite")
    if not existing:
        db.executemany("INSERT INTO run_meta VALUES(?,?)", expected.items())
    db.commit()
    return db


def _store_clinic(db, record, pages, status, verified, reasons, requests, error=""):
    now = datetime.now(timezone.utc).isoformat()
    page_urls = {p.url for p in pages}
    for page in pages:
        soup = page_soup(page)
        h1 = [x.get_text(" ", strip=True) for x in soup.select("h1")]
        h2 = [x.get_text(" ", strip=True) for x in soup.select("h2")]
        labels = [x.get("text", "") for x in page.links]
        hrefs = [x.get("url", "") for x in page.links]
        normalized = str(soup)
        digest = hashlib.sha256(page.html.encode("utf-8", errors="replace")).hexdigest()
        db.execute("""INSERT OR REPLACE INTO page_cache VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (record["clinic_id"], page.url, page.url, page.title, page.html, normalized,
                    json.dumps(h1, ensure_ascii=False), json.dumps(h2, ensure_ascii=False),
                    json.dumps(labels, ensure_ascii=False), json.dumps(hrefs, ensure_ascii=False),
                    page.main_text, _page_type(page), now, "OK", digest))
        for link in page.links:
            destination = canonical_page(link.get("url", ""))
            if not destination or host(destination) != host(page.url):
                continue
            db.execute("INSERT OR IGNORE INTO link_graph VALUES(?,?,?,?,?)",
                       (record["clinic_id"], page.url, link.get("text", ""), destination,
                        int(destination in page_urls)))
    db.execute("""INSERT OR REPLACE INTO clinic_fetch VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
               (record["clinic_id"], record["sampling_group"], record["selected_identity_url"],
                pages[0].url if pages else "", status, int(verified), json.dumps(reasons, ensure_ascii=False),
                len(pages), requests, error, now))


def fetch_pilot(sampled, cache_path: Path, sample_hash: str, clinic_db: Path, research_db: Path, mhlw_db: Path, retry_failed=False):
    transport = CountingTransport()
    fetcher = SafeFetcher(transport=transport, timeout=12, max_bytes=1_500_000, interval=.6)
    cache = _init_cache(cache_path, SEED, sample_hash)
    with open_ro(clinic_db) as cdb:
        clinics = {int(r["id"]): dict(r) for r in cdb.execute("SELECT * FROM clinics")}
        old = {int(r["clinic_id"]): read_json(r["result_json"], {})
               for r in cdb.execute("SELECT clinic_id,result_json FROM research_results")}
    mhlw = {}
    with open_ro(mhlw_db) as mdb:
        for cid, dep in mdb.execute("SELECT clinic_id,mhlw_department_name FROM clinic_mhlw_departments_final WHERE mhlw_department_name<>''"):
            mhlw.setdefault(int(cid), []).append(dep)
    if retry_failed:
        failed = cache.execute("SELECT * FROM clinic_fetch WHERE fetch_status='FETCH_FAILED'").fetchall()
        with cache:
            cache.executemany("""INSERT INTO fetch_attempt_history
              (clinic_id,sampling_group,initial_url,fetch_status,pages_fetched,http_request_count,error,completed_at)
              VALUES(?,?,?,?,?,?,?,?)""",
              [(r["clinic_id"],r["sampling_group"],r["initial_url"],r["fetch_status"],r["pages_fetched"],r["http_request_count"],r["error"],r["completed_at"]) for r in failed])
            cache.execute("DELETE FROM clinic_fetch WHERE fetch_status='FETCH_FAILED'")
    done = {int(r[0]) for r in cache.execute("SELECT clinic_id FROM clinic_fetch")}
    request_total = int(cache.execute("SELECT COALESCE(sum(http_request_count),0) FROM fetch_attempt_history").fetchone()[0])
    for i, record in enumerate(sampled, 1):
        cid = record["clinic_id"]
        if cid in done:
            request_total += int(cache.execute("SELECT http_request_count FROM clinic_fetch WHERE clinic_id=?", (cid,)).fetchone()[0])
            continue
        before = transport.request_count
        pages, status, verified, reasons, error = [], "FETCH_FAILED", False, [], ""
        c = clinics[cid]
        try:
            first = fetcher.fetch(record["selected_identity_url"])
            pages = [first]
            check = identity(c, first)
            verified, reasons = bool(check.get("verified")), check.get("reasons", [])
            if verified:
                pages, errors = crawl(first, fetcher, max_pages=5)
                status = "OK"
                if errors:
                    error = " | ".join(x["reason"] for x in errors[:3])
            else:
                status = "IDENTITY_NOT_VERIFIED"
        except WebError as exc:
            error = str(exc)
            status = "FETCH_FAILED"
        per_clinic_requests = transport.request_count - before
        with cache:
            _store_clinic(cache, record, pages, status, verified, reasons, per_clinic_requests, error)
        request_total += per_clinic_requests
        if i % 10 == 0 or i == len(sampled):
            print(f"pilot {i}/{len(sampled)} clinic_id={cid} status={status} pages={len(pages)} requests={request_total}", flush=True)
    # Build structured HYBRID replay only from identity-verified official pages.
    replay = []
    fetch_rows = {int(r["clinic_id"]): dict(r) for r in cache.execute("SELECT * FROM clinic_fetch")}
    for record in sampled:
        cid = record["clinic_id"]
        fr = fetch_rows[cid]
        if fr["fetch_status"] != "OK" or not fr["identity_verified"]:
            continue
        pages = [Page(r["page_url"], r["raw_html"]) for r in cache.execute(
            "SELECT page_url,raw_html FROM page_cache WHERE clinic_id=? ORDER BY page_url", (cid,))]
        results, focuses = evaluate_hybrid_treatments(
            clinic_id=cid, clinic_name=record["clinic_name"], departments=record["formal_departments"],
            pages=pages, checked_at=datetime.now(timezone.utc).isoformat(), identity_verified=True,
        )
        for result in results:
            row = result.to_dict()
            row.update({"clinic_name": record["clinic_name"], "sampling_group": record["sampling_group"],
                        "formal_departments": " / ".join(record["formal_departments"]),
                        "page_count": len(pages), "fetch_status": fr["fetch_status"]})
            replay.append(row)
    cache.close()
    return replay, fetch_rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--clinic-db", type=Path, default=PRODUCTION_CLINIC_DB)
    p.add_argument("--research-db", type=Path, default=PRODUCTION_RESEARCH_DB)
    p.add_argument("--mhlw-db", type=Path, default=MHLW_DB)
    p.add_argument("--phase3-csv", type=Path, default=PHASE3_CSV)
    p.add_argument("--pilot-dir", type=Path, default=PILOT_DIR)
    p.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    p.add_argument("--sample-only", action="store_true")
    p.add_argument("--retry-failed", action="store_true", help="retry only saved FETCH_FAILED clinics; preserve prior attempts in the pilot cache")
    args = p.parse_args()
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    args.pilot_dir.mkdir(parents=True, exist_ok=True)
    groups = _phase3_groups(args.phase3_csv)
    records, categories = _load_records(args.clinic_db, args.research_db, args.mhlw_db, groups)
    sampled, sample_stats = build_sample(records)
    sample_rows = [{"clinic_id": r["clinic_id"], "clinic_name": r["clinic_name"],
                    "sampling_group": r["sampling_group"], "prefecture": r["prefecture"],
                    "formal_departments": " / ".join(r["formal_departments"]),
                    "candidate_categories": " / ".join(r["candidate_categories"]),
                    "hp_domain": host(r["selected_identity_url"]), "selected_identity_url": r["selected_identity_url"],
                    "url_source": r["url_source"], "sample_seed": SEED} for r in sampled]
    sample_path = args.artifact_dir / "sample_population.csv"
    write_csv(sample_path, sample_rows, list(sample_rows[0]))
    if args.sample_only:
        print(json.dumps({"seed": SEED, "quota": QUOTAS, "sampled": len(sampled), "groups": sample_stats,
                          "pilot_cache_exists": (args.pilot_dir / "structured_cache.sqlite3").exists()},
                         ensure_ascii=False, indent=2))
        return

    clinic_hash_before = sha256(args.clinic_db)
    research_hash_before = sha256(args.research_db)
    mhlw_hash_before = sha256(args.mhlw_db)
    cache_path = args.pilot_dir / "structured_cache.sqlite3"
    sample_hash = sha256(sample_path)
    replay, fetch_rows = fetch_pilot(sampled, cache_path, sample_hash, args.clinic_db, args.research_db, args.mhlw_db, args.retry_failed)
    clinic_hash_after, research_hash_after, mhlw_hash_after = sha256(args.clinic_db), sha256(args.research_db), sha256(args.mhlw_db)
    if (clinic_hash_before, research_hash_before, mhlw_hash_before) != (clinic_hash_after, research_hash_after, mhlw_hash_after):
        raise AssertionError("Production input SHA-256 changed during pilot")
    with sqlite3.connect(f"file:{cache_path}?mode=ro", uri=True) as cache:
        cache.execute("PRAGMA query_only=ON")
        integrity = cache.execute("PRAGMA integrity_check").fetchone()[0]
        link_count = cache.execute("SELECT count(*) FROM link_graph").fetchone()[0]
        page_count = cache.execute("SELECT count(*) FROM page_cache").fetchone()[0]
        previous_request_count = cache.execute("SELECT COALESCE(sum(http_request_count),0) FROM fetch_attempt_history").fetchone()[0]
        previous_by_group = {g: int(cache.execute("SELECT COALESCE(sum(http_request_count),0) FROM fetch_attempt_history WHERE sampling_group=?", (g,)).fetchone()[0]) for g in QUOTAS}
        cache_clinics = int(cache.execute("SELECT count(*) FROM clinic_fetch").fetchone()[0])
        cache_max_pages = int(cache.execute("SELECT COALESCE(max(pages_fetched),0) FROM clinic_fetch").fetchone()[0])
    statuses = {s: sum(r["fetch_status"] == s for r in fetch_rows.values()) for s in ("OK", "FETCH_FAILED", "IDENTITY_NOT_VERIFIED")}
    # The replay has one final result per category; source/rank describe the
    # winning evidence. Department candidates remain separate and are not signals.
    best = {}
    for row in replay:
        key = (row["clinic_id"], row["treatment_category"])
        if key not in best or (STATUS_ORDER.index(row["hybrid_status"]), row["confidence"]) < (STATUS_ORDER.index(best[key]["hybrid_status"]), best[key]["confidence"]):
            best[key] = row
    by_clinic = {}
    for (cid, _), row in best.items():
        by_clinic.setdefault(cid, []).append(row)
    group_by_id = {r["clinic_id"]: r["sampling_group"] for r in sampled}
    outcome_rows, rescued = [], []
    for record in sampled:
        cid = record["clinic_id"]
        fr = fetch_rows[cid]
        rows = by_clinic.get(cid, [])
        if fr["fetch_status"] == "FETCH_FAILED": outcome = "FETCH_FAILED"
        elif fr["fetch_status"] == "IDENTITY_NOT_VERIFIED": outcome = "IDENTITY_NOT_VERIFIED"
        elif any(r["hybrid_status"] == "CONFIRMED" for r in rows): outcome = "ZERO_TO_CONFIRMED"
        elif any(r["hybrid_status"] == "MENTIONED" for r in rows): outcome = "ZERO_TO_MENTIONED"
        elif any(r["hybrid_status"] == "REVIEW" for r in rows): outcome = "ZERO_TO_REVIEW"
        elif any(r["hybrid_status"] == "CANDIDATE_ONLY" for r in rows): outcome = "CANDIDATE_ONLY"
        elif any(r["hybrid_status"] == "NOT_CONFIRMED" for r in rows): outcome = "NOT_CONFIRMED"
        else: outcome = "NO_SIGNAL"
        outcome_rows.append({"clinic_id": cid, "clinic_name": record["clinic_name"], "sampling_group": record["sampling_group"],
                             "outcome": outcome, "fetch_status": fr["fetch_status"], "identity_verified": fr["identity_verified"],
                             "pages_fetched": fr["pages_fetched"], "http_requests": fr["http_request_count"]})
        positives = [r for r in rows if r["hybrid_status"] in POSITIVE]
        if positives:
            rescued.append({"clinic_id": cid, "clinic_name": record["clinic_name"], "sampling_group": record["sampling_group"],
                            "formal_departments": " / ".join(record["formal_departments"]),
                            "hybrid_status": next(s for s in STATUS_ORDER if any(x["hybrid_status"] == s for x in positives)),
                            "treatments": " / ".join(sorted({r["treatment_category"] for r in positives})),
                            "sources": " / ".join(sorted({r["signal_source"] for r in positives})),
                            "evidence_urls": " / ".join(sorted({r["evidence_url"] for r in positives if r["evidence_url"]}))})

    source_counts = {}
    category_counts = {}
    for row in best.values():
        if row["hybrid_status"] not in POSITIVE or row["signal_source"] == "DEPARTMENT_CANDIDATE":
            continue
        key = row["signal_source"]
        source_counts.setdefault(key, []).append(row)
        category_counts.setdefault(row["treatment_category"], []).append(row)
    source_rows = [{"signal_source": s, "treatment_rows": len(source_counts.get(s, [])),
                    "unique_clinics": len({r["clinic_id"] for r in source_counts.get(s, [])})}
                   for s in ("CLINIC_NAME", "HOME_MENU", "INTRO_MENU", "DEDICATED_PAGE", "OFFICIAL_HP_TEXT")]
    category_rows = [{"treatment_category": cat,
                      "CONFIRMED": sum(r["hybrid_status"] == "CONFIRMED" for r in rows),
                      "MENTIONED": sum(r["hybrid_status"] == "MENTIONED" for r in rows),
                      "REVIEW": sum(r["hybrid_status"] == "REVIEW" for r in rows),
                      "unique_clinics": len({r["clinic_id"] for r in rows})}
                     for cat, rows in sorted(category_counts.items())]
    audit = []
    for row in best.values():
        if row["hybrid_status"] not in POSITIVE or row["signal_source"] == "DEPARTMENT_CANDIDATE":
            continue
        key = (int(row["clinic_id"]), row["treatment_category"])
        if row["hybrid_status"] == "CONFIRMED" and key in AUDIT_CONFIRMED_FALSE:
            label, comment = "FALSE_POSITIVE", AUDIT_CONFIRMED_FALSE[key]
        elif row["hybrid_status"] == "MENTIONED" and key in AUDIT_MENTIONED_FALSE:
            label, comment = "FALSE_POSITIVE", AUDIT_MENTIONED_FALSE[key]
        elif row["hybrid_status"] == "MENTIONED" and key in AUDIT_MENTIONED_UNCERTAIN:
            label, comment = "UNCERTAIN", AUDIT_MENTIONED_UNCERTAIN[key]
        elif row["hybrid_status"] == "REVIEW" and key in AUDIT_REVIEW_UNCERTAIN:
            label, comment = "UNCERTAIN", AUDIT_REVIEW_UNCERTAIN[key]
        else:
            label, comment = "TRUE_POSITIVE", "Saved official page contains a concrete category-matched Treatment signal in the stated source/context."
        audit.append({**row, "human_label": label, "human_comment": comment})

    confirmed_audit = [r for r in audit if r["hybrid_status"] == "CONFIRMED"]
    mentioned_audit = [r for r in audit if r["hybrid_status"] == "MENTIONED"]
    confirmed_tp = sum(r["human_label"] == "TRUE_POSITIVE" for r in confirmed_audit)
    confirmed_fp = sum(r["human_label"] == "FALSE_POSITIVE" for r in confirmed_audit)
    mentioned_literal = sum(bool(r["matched_alias"] and keyword_match(r["matched_alias"], r["evidence_text"] + " " + r["page_title"])) for r in mentioned_audit)
    audit_label_counts = {label: sum(r["human_label"] == label for r in audit) for label in ("TRUE_POSITIVE", "FALSE_POSITIVE", "UNCERTAIN")}
    rescued_clinic_labels = {}
    for r in audit:
        cid = int(r["clinic_id"])
        rescued_clinic_labels.setdefault(cid, []).append(r)

    fetch_summary = []
    for g in QUOTAS:
        members = [r for r in sampled if r["sampling_group"] == g]
        fetch_summary.append({"sampling_group": g, "selected_clinics": len(members),
                              "OK": sum(fetch_rows[r["clinic_id"]]["fetch_status"] == "OK" for r in members),
                              "FETCH_FAILED": sum(fetch_rows[r["clinic_id"]]["fetch_status"] == "FETCH_FAILED" for r in members),
                              "IDENTITY_NOT_VERIFIED": sum(fetch_rows[r["clinic_id"]]["fetch_status"] == "IDENTITY_NOT_VERIFIED" for r in members),
                              "pages_fetched": sum(fetch_rows[r["clinic_id"]]["pages_fetched"] for r in members),
                              "initial_sandbox_failed_requests": previous_by_group[g],
                              "final_attempt_requests": sum(fetch_rows[r["clinic_id"]]["http_request_count"] for r in members),
                              "total_http_requests": previous_by_group[g] + sum(fetch_rows[r["clinic_id"]]["http_request_count"] for r in members)})
    write_csv(args.artifact_dir / "fetch_summary.csv", fetch_summary,
              ["sampling_group", "selected_clinics", "OK", "FETCH_FAILED", "IDENTITY_NOT_VERIFIED", "pages_fetched",
               "initial_sandbox_failed_requests", "final_attempt_requests", "total_http_requests"])
    write_csv(args.artifact_dir / "structured_replay.csv", list(best.values()), list(next(iter(best.values())).keys()) if best else ["clinic_id"])
    write_csv(args.artifact_dir / "rescued_clinics.csv", rescued, list(rescued[0]) if rescued else ["clinic_id"])
    write_csv(args.artifact_dir / "signal_source_summary.csv", source_rows, ["signal_source", "treatment_rows", "unique_clinics"])
    write_csv(args.artifact_dir / "treatment_category_summary.csv", category_rows, ["treatment_category", "CONFIRMED", "MENTIONED", "REVIEW", "unique_clinics"])
    write_csv(args.artifact_dir / "human_audit.csv", audit, list(audit[0]) if audit else ["clinic_id", "human_label", "human_comment"])
    outcomes = {k: sum(r["outcome"] == k for r in outcome_rows) for k in
                ("ZERO_TO_CONFIRMED", "ZERO_TO_MENTIONED", "ZERO_TO_REVIEW", "CANDIDATE_ONLY", "NOT_CONFIRMED", "NO_SIGNAL", "FETCH_FAILED", "IDENTITY_NOT_VERIFIED")}
    if cache_clinics != len(sampled) or cache_max_pages > 5 or sum(outcomes.values()) != len(sampled):
        raise AssertionError({"cache_clinics": cache_clinics, "sampled": len(sampled), "max_pages": cache_max_pages, "outcome_total": sum(outcomes.values())})
    group_population = {g: sum(v == g for v in groups.values()) for g in QUOTAS}
    extrapolation = {}
    for group in QUOTAS:
        subset = [r for r in outcome_rows if r["sampling_group"] == group]
        positive_clinics = {int(r["clinic_id"]) for r in subset if r["fetch_status"] == "OK"}
        group_ids = {int(r["clinic_id"]) for r in subset}
        conservative_sales = {cid for cid in group_ids if any(x["human_label"] == "TRUE_POSITIVE" and x["hybrid_status"] in POSITIVE for x in rescued_clinic_labels.get(cid, []))}
        base_sales = {cid for cid in positive_clinics if any(x["human_label"] in {"TRUE_POSITIVE", "UNCERTAIN"} and x["hybrid_status"] in POSITIVE for x in rescued_clinic_labels.get(cid, []))}
        conservative_strong = {cid for cid in group_ids if any(x["human_label"] == "TRUE_POSITIVE" and x["hybrid_status"] in {"CONFIRMED", "MENTIONED"} for x in rescued_clinic_labels.get(cid, []))}
        base_strong = {cid for cid in positive_clinics if any(x["human_label"] in {"TRUE_POSITIVE", "UNCERTAIN"} and x["hybrid_status"] in {"CONFIRMED", "MENTIONED"} for x in rescued_clinic_labels.get(cid, []))}
        conservative_rate = len(conservative_sales) / len(subset) if subset else 0
        base_rate = len(base_sales) / len(positive_clinics) if positive_clinics else 0
        conservative_strong_rate = len(conservative_strong) / len(subset) if subset else 0
        base_strong_rate = len(base_strong) / len(positive_clinics) if positive_clinics else 0
        extrapolation[group] = {"population": group_population[group], "sample": len(subset),
                                "successfully_identity_verified": len(positive_clinics),
                                "conservative_audited_sales_clinics": len(conservative_sales),
                                "base_audited_or_uncertain_sales_clinics": len(base_sales),
                                "conservative_sales_rescue_rate": conservative_rate, "base_sales_rescue_rate": base_rate,
                                "conservative_sales_add": int(group_population[group] * conservative_rate),
                                "base_sales_add": round(group_population[group] * base_rate),
                                "conservative_audited_strong_clinics": len(conservative_strong),
                                "base_audited_or_uncertain_strong_clinics": len(base_strong),
                                "conservative_strong_add": int(group_population[group] * conservative_strong_rate),
                                "base_strong_add": round(group_population[group] * base_strong_rate)}
    exp = {"method": "reason-group rates based on exhaustive manual review of all new pilot Treatment rows; conservative counts TRUE_POSITIVE only and treats fetch/identity failures as zero, base uses identity-verified clinics and includes UNCERTAIN as signal",
           "current_sales_signal": 2206, "pilot": outcomes, "by_reason_group": extrapolation,
           "conservative_sales_total": 2206 + sum(x["conservative_sales_add"] for x in extrapolation.values()),
           "base_sales_total": 2206 + sum(x["base_sales_add"] for x in extrapolation.values()),
           "conservative_strong_total": 2206 + sum(x["conservative_strong_add"] for x in extrapolation.values()),
           "base_strong_total": 2206 + sum(x["base_strong_add"] for x in extrapolation.values()),
           "note": "Assisted saved-evidence audit, not an independent external human audit. Point estimates only; no structural signals were replayed for failed/unverified clinics."}
    (args.artifact_dir / "extrapolation.json").write_text(json.dumps(exp, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    request_count = int(previous_request_count) + sum(int(r["http_request_count"]) for r in fetch_rows.values())
    summary = {"seed": SEED, "quota": QUOTAS, "sample_clinics": len(sampled), "group_sample_stats": sample_stats,
               "fetch_statuses": statuses, "outcomes": outcomes, "replay_rows": len(best),
               "rescued_clinics": len(rescued), "source_summary": source_rows,
               "structural_rescued_clinics": {s: source_rows[[r["signal_source"] for r in source_rows].index(s)]["unique_clinics"] for s in ("HOME_MENU", "INTRO_MENU", "DEDICATED_PAGE")},
               "human_audit_rows": len(audit), "audit_labels": audit_label_counts,
               "confirmed_precision": confirmed_tp / (confirmed_tp + confirmed_fp) if confirmed_tp + confirmed_fp else None,
               "confirmed_precision_counts": {"true_positive": confirmed_tp, "false_positive": confirmed_fp, "denominator": confirmed_tp + confirmed_fp},
               "mentioned_literal_validity": mentioned_literal / len(mentioned_audit) if mentioned_audit else None,
               "mentioned_literal_validity_counts": {"literal_visible": mentioned_literal, "rows": len(mentioned_audit)},
               "independent_human_audit": False,
               "human_audit_request_100_met": len(audit) >= 100,
               "audit_gate_confirmed_precision_98_percent": confirmed_tp / (confirmed_tp + confirmed_fp) >= .98 if confirmed_tp + confirmed_fp else False,
               "outcome_partition_total": sum(outcomes.values()), "unique_fetched_clinics": cache_clinics,
               "max_saved_pages_per_clinic": cache_max_pages,
               "http_request_count_including_robots_redirects": request_count,
               "pages_fetched": page_count, "link_graph_edges": link_count,
               "cache_integrity_check": integrity, "cache_path": str(cache_path),
               "production_input_sha256_before": {"clinics": clinic_hash_before, "research": research_hash_before, "mhlw": mhlw_hash_before},
               "production_input_sha256_after": {"clinics": clinic_hash_after, "research": research_hash_after, "mhlw": mhlw_hash_after},
               "extrapolation": exp,
               "production_hashes_unchanged": True, "production_db_changed": False,
               "production_sidecar_changed": False, "comdesk_changed": False,
               "phase4b_started": False}
    (args.artifact_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_csv(args.artifact_dir / "pilot_outcomes.csv", outcome_rows, list(outcome_rows[0]) if outcome_rows else ["clinic_id"])
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
