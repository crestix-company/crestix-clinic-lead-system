#!/usr/bin/env python3
"""Standalone Phase 7 Treatment Research Worker (Step 5 / Step 9).

Deliberately Claude-Code independent: an ordinary Python CLI you run, stop, check,
and resume from any terminal (or cron/launchd). Nothing about the Full Run depends
on a Claude Code session staying open.

    python -m scripts.research_worker start  --manifest data/manifests/<id>.csv
    python -m scripts.research_worker resume --manifest data/manifests/<id>.csv
    python -m scripts.research_worker status --manifest data/manifests/<id>.csv
    python -m scripts.research_worker stop    --manifest data/manifests/<id>.csv

Reuses the frozen v4 evidence engine and candidate_union pre-filter unchanged
(evaluate_treatment_evidence, candidate_union). This file only adds execution
infrastructure: checkpoint/resume, failure isolation, a fetch/classification
cache, bounded retry+backoff, and bounded same-domain concurrency.

Three SQLite stores, kept deliberately separate:
  - LOCAL cache DB   (data/research_worker/<manifest_id>/cache.sqlite3, gitignored):
    raw fetched-page cache, keyed by clinic_id+page_url. Lets a resumed run skip
    HTTP the classification step already has good data for.
  - LOCAL progress DB (data/research_worker/<manifest_id>/progress.sqlite3, gitignored):
    one row per clinic_id, drives checkpoint/resume and failure isolation.
  - Production final results: Supabase treatment.* via clinic_runtime.
    The historical final SQLite is used only in explicit CLINIC_WRITE_BACKEND=sqlite
    admin/migration/test mode.
    the only thing the parallel Filter/UI session reads. WAL + busy_timeout +
    one-clinic-per-transaction so it is always safely readable mid-run.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from scripts.phase7b_pilot import choose_identity_url
from src.enrichment.candidate_map import candidate_union
from src.enrichment.hp_analysis import Page, host, identity, is_official_candidate
from src.enrichment.safe_web import PinnedTransport, SafeFetcher, WebError, crawl
from src.enrichment.treatment_context import build_evidence_blocks
from src.enrichment.treatment_taxonomy import (
    EVIDENCE_ENGINE_VERSION,
    RULE_VERSION,
    active_treatment_category_names,
    evaluate_treatment_evidence,
)
from src.utils.config import ROOT

# ---------------------------------------------------------------------------
# Paths / configuration
# ---------------------------------------------------------------------------
CLINIC_DB = ROOT / "data/clinics.sqlite3"
FINAL_DB_DEFAULT = Path.home() / "CrestixData/clinic-lead/treatment_research_final.sqlite3"
FINAL_DB = Path(os.environ.get("TREATMENT_RESEARCH_DB_PATH", str(FINAL_DB_DEFAULT)))
WORKER_ROOT = ROOT / "data/research_worker"

GLOBAL_CONCURRENCY = 8
FETCH_TIMEOUT = 10
FETCH_MAX_BYTES = 2_000_000
FETCH_INTERVAL = 0.6
MAX_RETRY_ATTEMPTS = 3
RETRY_BACKOFF_BASE = 1.0  # seconds; attempt N waits RETRY_BACKOFF_BASE * N -> 1s, 2s
# Separate cap on cross-resume reclaims of a clinic already marked FETCH_FAILED/ERROR.
# Without this, `resume --retry-failed` against a permanently-broken site (non-retryable
# WebError, e.g. access-restricted) reclaims the same clinic forever in a tight loop --
# claim_next_batch never sees it leave FETCH_FAILED, so the run never terminates.
MAX_CLINIC_ATTEMPTS = 3
# Transient-only: generic connection/timeout, the separate read-deadline timeout, and
# exactly the four 5xx codes treated as retryable (not e.g. 501/505, which are more
# likely a permanent server misconfiguration than a transient blip). Deliberately
# excludes "HTTP 429" -- safe_web.py's SafeFetcher._get() already gives 429 its own
# bounded, Retry-After-respecting single retry at the fetch layer; by the time a 429
# reaches here that budget is already spent, so retrying again would double it.
# Also excludes the SSL-certificate-specific message (safe_web.py raises a distinct
# string for ssl.SSLError) -- a certificate failure is permanent for this host, not a
# transient network blip, and must never be retried.
RETRYABLE_ERROR = re.compile(r"タイムアウト|時間上限を超えました|HTTP (?:500|502|503|504)")

STATUSES = ("CONFIRMED", "REVIEW", "NOT_CONFIRMED", "FETCH_FAILED", "NOT_RESEARCHED")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
def load_manifest(path: Path) -> list[dict]:
    import csv
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"manifest is empty: {path}")
    manifest_ids = {row["manifest_id"] for row in rows}
    if len(manifest_ids) != 1:
        raise ValueError(f"manifest must have exactly one manifest_id, found {manifest_ids}")
    seen = set()
    for row in rows:
        cid = int(row["clinic_id"])
        if cid in seen:
            raise ValueError(f"duplicate clinic_id in manifest: {cid}")
        seen.add(cid)
    return rows


def manifest_paths(manifest_id: str) -> dict[str, Path]:
    base = WORKER_ROOT / manifest_id
    base.mkdir(parents=True, exist_ok=True)
    return {
        "base": base,
        "cache_db": base / "cache.sqlite3",
        "progress_db": base / "progress.sqlite3",
        "stop_file": base / "STOP",
        "lock_file": base / "worker.lock",
        "log_file": base / "worker.log",
    }


# ---------------------------------------------------------------------------
# Local progress DB (checkpoint / resume / failure isolation)
# ---------------------------------------------------------------------------
def open_progress_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("""
        CREATE TABLE IF NOT EXISTS clinic_progress (
            clinic_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK(status IN ('PENDING','IN_PROGRESS','DONE','FETCH_FAILED','ERROR')),
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            candidate_count INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL DEFAULT '',
            finished_at TEXT NOT NULL DEFAULT ''
        )
    """)
    return db


def seed_progress(db: sqlite3.Connection, clinic_ids: list[int]) -> None:
    db.executemany(
        "INSERT OR IGNORE INTO clinic_progress (clinic_id, status) VALUES (?, 'PENDING')",
        [(cid,) for cid in clinic_ids],
    )


def claim_next_batch(db: sqlite3.Connection, retry_failed: bool, limit: int) -> list[int]:
    if not retry_failed:
        query = "SELECT clinic_id FROM clinic_progress WHERE status='PENDING' ORDER BY clinic_id LIMIT ?"
        rows = db.execute(query, (limit,)).fetchall()
    else:
        # A clinic stays reclaimable across resumes only while under the attempt cap --
        # otherwise a permanently-broken site (non-retryable WebError) would be reclaimed,
        # fail again, and get reclaimed again forever within a single `resume
        # --retry-failed` run (see MAX_CLINIC_ATTEMPTS).
        query = (
            "SELECT clinic_id FROM clinic_progress "
            "WHERE status='PENDING' OR (status IN ('FETCH_FAILED','ERROR') AND attempts < ?) "
            "ORDER BY clinic_id LIMIT ?"
        )
        rows = db.execute(query, (MAX_CLINIC_ATTEMPTS, limit)).fetchall()
    ids = [r[0] for r in rows]
    if ids:
        db.executemany(
            "UPDATE clinic_progress SET status='IN_PROGRESS', attempts=attempts+1, started_at=? WHERE clinic_id=?",
            [(_now(), cid) for cid in ids],
        )
    return ids


def mark_progress(db: sqlite3.Connection, clinic_id: int, status: str, error: str = "", candidate_count: int = 0) -> None:
    db.execute(
        "UPDATE clinic_progress SET status=?, last_error=?, candidate_count=?, finished_at=? WHERE clinic_id=?",
        (status, error, candidate_count, _now(), clinic_id),
    )


def progress_summary(db: sqlite3.Connection) -> dict:
    rows = db.execute("SELECT status, COUNT(*) FROM clinic_progress GROUP BY status").fetchall()
    counts = {status: 0 for status in ("PENDING", "IN_PROGRESS", "DONE", "FETCH_FAILED", "ERROR")}
    counts.update(dict(rows))
    total = sum(counts.values())
    done = counts["DONE"] + counts["FETCH_FAILED"]
    return {"counts": counts, "total": total, "progress_pct": round(100 * done / total, 2) if total else 0.0}


# ---------------------------------------------------------------------------
# Local fetch cache (separate from classification; resume-friendly)
# ---------------------------------------------------------------------------
def open_cache_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("""
        CREATE TABLE IF NOT EXISTS page_cache (
            clinic_id INTEGER NOT NULL,
            page_url TEXT NOT NULL,
            final_url TEXT NOT NULL DEFAULT '',
            page_title TEXT NOT NULL DEFAULT '',
            page_type TEXT NOT NULL DEFAULT '',
            sanitized_text TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL DEFAULT '',
            fetched_at TEXT NOT NULL,
            fetch_status TEXT NOT NULL,
            PRIMARY KEY (clinic_id, page_url)
        )
    """)
    return db


def cache_clinic_pages(db: sqlite3.Connection, clinic_id: int, pages: list) -> None:
    import hashlib
    rows = []
    for p in pages:
        text = getattr(p, "main_text", "") or ""
        rows.append((
            clinic_id, p.url, p.url, getattr(p, "title", ""), "",
            text, hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest(), _now(), "OK",
        ))
    if rows:
        db.executemany(
            "INSERT OR REPLACE INTO page_cache "
            "(clinic_id, page_url, final_url, page_title, page_type, sanitized_text, content_hash, fetched_at, fetch_status) "
            "VALUES (?,?,?,?,?,?,?,?,?)", rows,
        )


# ---------------------------------------------------------------------------
# Shared final results DB (read by the parallel Filter/UI session)
# ---------------------------------------------------------------------------
def open_final_db(path: Path) -> sqlite3.Connection:
    """Two tables, two different grains -- never mix clinic-level attempt status
    into Treatment-level results again (see the research/v4-base sidecar
    consistency fix: a clinic that failed on attempt N but succeeded with few or
    zero candidate categories on attempt N+1 left 1-43 stale FETCH_FAILED
    Treatment rows behind, because only the categories in candidate_union were
    ever INSERT OR REPLACEd).

    clinic_research_status: ONE row per clinic, clinic-level attempt outcome.
    DONE or FETCH_FAILED. A missing row means NOT_RESEARCHED -- this table is the
    sole source of truth for that three-way state; Treatment-table row presence/
    absence must never be used to infer it.

    clinic_treatment_research_final: Treatment-level results ONLY --
    CONFIRMED/REVIEW/NOT_CONFIRMED. FETCH_FAILED is deliberately not a legal
    value here any more: a fetch failure has nothing to report at the Treatment
    grain (see write_clinic_atomic: on failure this table is left untouched,
    never written with placeholder failure rows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("""
        CREATE TABLE IF NOT EXISTS clinic_treatment_research_final (
            clinic_id INTEGER NOT NULL,
            treatment_category_id TEXT NOT NULL,
            treatment_category_name TEXT NOT NULL,
            research_status TEXT NOT NULL
                CHECK(research_status IN ('CONFIRMED','REVIEW','NOT_CONFIRMED')),
            matched_alias TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            page_title TEXT NOT NULL DEFAULT '',
            provider_context TEXT NOT NULL DEFAULT '',
            exclusion_context TEXT NOT NULL DEFAULT '',
            evidence_engine_version TEXT NOT NULL,
            taxonomy_version TEXT NOT NULL,
            researched_at TEXT NOT NULL,
            git_commit_sha TEXT NOT NULL,
            manifest_id TEXT NOT NULL,
            PRIMARY KEY (clinic_id, treatment_category_name)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS clinic_research_status (
            clinic_id INTEGER PRIMARY KEY,
            research_status TEXT NOT NULL CHECK(research_status IN ('DONE','FETCH_FAILED')),
            candidate_count INTEGER NOT NULL DEFAULT 0,
            source_url TEXT NOT NULL DEFAULT '',
            final_url TEXT NOT NULL DEFAULT '',
            identity_verified INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            taxonomy_version TEXT NOT NULL,
            evidence_engine_version TEXT NOT NULL,
            manifest_id TEXT NOT NULL,
            git_commit_sha TEXT NOT NULL,
            researched_at TEXT NOT NULL
        )
    """)
    return db


def write_clinic_atomic(db: sqlite3.Connection, clinic_id: int, rows: list[dict], status_row: dict) -> None:
    """One clinic, one transaction: Filter/UI never observes a half-written clinic.

    On DONE: clinic_treatment_research_final is fully replaced for this
    clinic_id (DELETE then INSERT) with exactly the current candidate_union's
    results -- never a merge/upsert, so a category that was a candidate on a
    previous attempt but is not one now cannot leave a stale row behind
    (invariant: 1 clinic x 1 treatment_category = at most 1 row, and it is
    always the LATEST attempt's row).

    On FETCH_FAILED: clinic_treatment_research_final is left completely
    untouched for this clinic_id -- a transient failure must never delete or
    overwrite a prior successful result. FETCH_FAILED is recorded only in
    clinic_research_status.
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        if status_row["research_status"] == "DONE":
            db.execute("DELETE FROM clinic_treatment_research_final WHERE clinic_id=?", (clinic_id,))
            if rows:
                payload = [(
                    clinic_id, r["treatment_category_name"], r["treatment_category_name"], r["research_status"],
                    r.get("matched_alias", ""), r.get("source_url", ""), r.get("page_title", ""),
                    r.get("provider_context", ""), r.get("exclusion_context", ""),
                    r["evidence_engine_version"], r["taxonomy_version"], r["researched_at"],
                    r["git_commit_sha"], r["manifest_id"],
                ) for r in rows]
                db.executemany(
                    "INSERT INTO clinic_treatment_research_final "
                    "(clinic_id, treatment_category_id, treatment_category_name, research_status, matched_alias, "
                    " source_url, page_title, provider_context, exclusion_context, evidence_engine_version, "
                    " taxonomy_version, researched_at, git_commit_sha, manifest_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", payload,
                )
        db.execute(
            "INSERT INTO clinic_research_status "
            "(clinic_id, research_status, candidate_count, source_url, final_url, identity_verified, "
            " attempts, last_error, taxonomy_version, evidence_engine_version, manifest_id, git_commit_sha, researched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(clinic_id) DO UPDATE SET "
            "research_status=excluded.research_status, candidate_count=excluded.candidate_count, "
            "source_url=excluded.source_url, final_url=excluded.final_url, "
            "identity_verified=excluded.identity_verified, attempts=excluded.attempts, "
            "last_error=excluded.last_error, taxonomy_version=excluded.taxonomy_version, "
            "evidence_engine_version=excluded.evidence_engine_version, manifest_id=excluded.manifest_id, "
            "git_commit_sha=excluded.git_commit_sha, researched_at=excluded.researched_at",
            (
                clinic_id, status_row["research_status"], status_row["candidate_count"],
                status_row["source_url"], status_row["final_url"], int(status_row["identity_verified"]),
                status_row["attempts"], status_row["last_error"], status_row["taxonomy_version"],
                status_row["evidence_engine_version"], status_row["manifest_id"], status_row["git_commit_sha"],
                status_row["researched_at"],
            ),
        )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def _build_treatment_repo(final_db_conn):
    from src.repository.treatment_write_backend import build_treatment_write_repository
    return build_treatment_write_repository(final_db_conn)


# ---------------------------------------------------------------------------
# Fetch + classify one clinic (bounded retry/backoff on the fetch step only;
# retrying the classifier is pointless -- it is pure/deterministic)
# ---------------------------------------------------------------------------
class _MeteredFetcher:
    """Counts every individual HTTP attempt (including retries) and bounds-retries
    transient failures (RETRYABLE_ERROR) with linear backoff. Used for every fetch
    in research_one_clinic -- the initial page, support-link fetches, AND (passed
    directly to crawl()) every same-host page crawl() fetches -- so a transient
    blip during the crawl phase gets the same retry benefit as the first fetch,
    not just the identity-verification page."""
    def __init__(self, fetcher: SafeFetcher):
        self.fetcher = fetcher
        self.attempts = 0
        self.successes = 0

    def fetch(self, *args, **kwargs):
        last_exc = None
        for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
            self.attempts += 1
            try:
                page = self.fetcher.fetch(*args, **kwargs)
                self.successes += 1
                return page
            except WebError as exc:
                last_exc = exc
                if not RETRYABLE_ERROR.search(str(exc)) or attempt == MAX_RETRY_ATTEMPTS:
                    raise
                time.sleep(RETRY_BACKOFF_BASE * attempt)
        raise last_exc  # pragma: no cover - unreachable, satisfies type checkers


def _is_same_site(url_a: str, url_b: str) -> bool:
    """www-insensitive host match, mirroring safe_web.py's private
    _same_site_host/_host_key exactly (duplicated rather than imported across
    the module boundary, since it is a single-line rule)."""
    ha, hb = host(url_a), host(url_b)
    if not ha or not hb:
        return False
    strip = lambda h: h[4:] if h.startswith("www.") else h
    return strip(ha) == strip(hb)


def research_one_clinic(record: dict, fetcher: _MeteredFetcher, meta: dict) -> tuple[list[dict], str, int]:
    """Returns (result_rows, fetch_status, candidate_count). Never raises for
    ordinary fetch failures -- those become FETCH_FAILED/REVIEW rows, not
    exceptions, so one clinic's bad site never takes down the run (failure
    isolation).

    fetch_status distinguishes two different things, matching the original
    frozen evidence engine's convention (scripts/phase7b_pilot.py):
      - FETCH_ERROR: the HTTP fetch itself failed (timeout/5xx/429/DNS/robots/
        access-restricted/etc.) -- a genuine "we have nothing" case. Written as
        FETCH_FAILED (new operational status; never NOT_CONFIRMED, so it is
        never silently treated as "researched and found nothing").
      - IDENTITY_NOT_VERIFIED: the fetch succeeded (we have page content) but
        could not confirm the page belongs to this clinic -- written as REVIEW,
        exactly as the original engine already does, not FETCH_FAILED (the
        page fetch itself did not fail)."""
    checked_at = _now()
    active_categories = set(active_treatment_category_names())
    try:
        # Initial-fetch-only redirect rescue: a real official-HP domain migration
        # (e.g. clinic.example.jp -> www.example.jp) would otherwise permanently
        # FETCH_FAILED every such clinic. Allowed only here -- never for
        # support-link fetches or crawl() -- and the destination still must pass
        # the normal NON_OFFICIAL check plus identity() below; neither check is
        # loosened, and this never grants more than one cross-domain hop.
        first = fetcher.fetch(record["effective_official_hp_url"], max_cross_domain_hops=1)
        if not _is_same_site(first.url, record["effective_official_hp_url"]) and not is_official_candidate(first.url):
            raise WebError("別ドメインへのリダイレクト先が公式HPと判定できません。")
        pages = [first]
        check = identity(record, first)
        if not check.get("verified"):
            support_links = [
                link["url"] for link in first.links
                if host(link["url"]) == host(first.url)
                and any(token in (link.get("text", "") + link["url"]).lower()
                        for token in ("アクセス", "医院概要", "医院紹介", "contact", "access", "about"))
            ]
            support = []
            for link_url in list(dict.fromkeys(support_links))[:2]:
                try:
                    support.append(fetcher.fetch(link_url))
                except WebError:
                    pass
            if support:
                check = identity(record, Page(first.url, first.html + "\n" + "\n".join(p.html for p in support)))
                pages.extend(support)
        if not check.get("verified"):
            fetch_status = "IDENTITY_NOT_VERIFIED"
        else:
            crawled, _errors = crawl(first, fetcher, max_pages=5)
            seen = {p.url for p in pages}
            pages.extend(p for p in crawled if p.url not in seen)
            fetch_status = "OK"
    except WebError as exc:
        # FETCH_FAILED is a clinic-level status now (clinic_research_status), not a
        # Treatment-level one: a fetch failure means we have no evidence at all for
        # any category, so there is nothing to write to clinic_treatment_research_final
        # -- and critically, writing nothing here means a PRIOR successful result for
        # this clinic (if any) is never clobbered by a later transient failure.
        meta["last_error"] = str(exc)
        return [], "FETCH_ERROR", 0

    if fetch_status == "IDENTITY_NOT_VERIFIED":
        rows = _non_ok_rows(record, active_categories, checked_at, "REVIEW", fetch_status)
        return rows, fetch_status, 0

    meta["pages"] = pages
    official_pages = [p for p in pages if is_official_candidate(p.url)]
    evidence = [{"url": p.url, "page_title": p.title, "blocks": build_evidence_blocks(p),
                 "source_type": "OFFICIAL_HP"} for p in official_pages]
    try:
        departments = json.loads(record.get("crestix_sales_category_hint_raw") or "[]")
    except (json.JSONDecodeError, TypeError):
        departments = []
    candidates = candidate_union(departments, evidence)

    rows = []
    for category in sorted(candidates):
        result = evaluate_treatment_evidence(category, evidence, clinic_id=record["clinic_id"],
                                              checked_at=checked_at, clinic_name=record.get("clinic_name", ""))
        rows.append({
            "treatment_category_name": category, "research_status": result["status"],
            "matched_alias": result.get("matched_alias", ""),
            "source_url": result.get("evidence_url", "") or (pages[0].url if pages else ""),
            "page_title": result.get("evidence_page_title", ""),
            "provider_context": result.get("provider_context", "UNKNOWN"),
            "exclusion_context": result.get("exclusion_context", "NONE"),
            "evidence_engine_version": EVIDENCE_ENGINE_VERSION, "taxonomy_version": RULE_VERSION,
            "researched_at": checked_at, "git_commit_sha": record["git_commit_sha"],
            "manifest_id": record["manifest_id"],
        })
    return rows, "OK", len(candidates)


def _non_ok_rows(record: dict, categories: set[str], checked_at: str, status: str, reason: str) -> list[dict]:
    return [{
        "treatment_category_name": category, "research_status": status,
        "matched_alias": "", "source_url": record.get("effective_official_hp_url", ""),
        "page_title": "", "provider_context": "UNKNOWN", "exclusion_context": reason[:200],
        "evidence_engine_version": EVIDENCE_ENGINE_VERSION, "taxonomy_version": RULE_VERSION,
        "researched_at": checked_at, "git_commit_sha": record["git_commit_sha"],
        "manifest_id": record["manifest_id"],
    } for category in sorted(categories)]


# ---------------------------------------------------------------------------
# Bounded, same-domain-serialized concurrency
# ---------------------------------------------------------------------------
class DomainGate:
    """global_concurrency total in-flight fetches; at most one in-flight per domain."""
    def __init__(self, global_concurrency: int):
        self._global = threading.Semaphore(global_concurrency)
        self._domain_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._registry_lock = threading.Lock()

    def _lock_for(self, domain: str) -> threading.Lock:
        with self._registry_lock:
            return self._domain_locks[domain]

    def run(self, domain: str, fn, *args, **kwargs):
        with self._global:
            with self._lock_for(domain):
                return fn(*args, **kwargs)


# ---------------------------------------------------------------------------
# Worker main loop
# ---------------------------------------------------------------------------
def _load_records_for_manifest(manifest_rows: list[dict]) -> dict[int, dict]:
    records = {}
    for row in manifest_rows:
        records[int(row["clinic_id"])] = {
            "clinic_id": int(row["clinic_id"]),
            "clinic_name": row.get("clinic_name", ""),
            "phone": row.get("phone", ""),
            "address": row.get("address", ""),
            "effective_official_hp_url": row["effective_official_hp_url"],
            "crestix_sales_category_hint_raw": json.dumps(
                [d for d in row.get("crestix_sales_category_hint", "").split(";") if d]
            ),
            "git_commit_sha": row["git_commit_sha"],
            "manifest_id": row["manifest_id"],
        }
    return records


def _write_lockfile(lock_file: Path) -> None:
    if lock_file.exists():
        try:
            pid = int(lock_file.read_text().strip())
            os.kill(pid, 0)
            raise RuntimeError(f"another worker appears to be running for this manifest (pid={pid})")
        except (ValueError, ProcessLookupError):
            pass  # stale lockfile
    lock_file.write_text(str(os.getpid()))


def run_worker(manifest_path: Path, retry_failed: bool, concurrency: int, batch_size: int = 200) -> None:
    manifest_rows = load_manifest(manifest_path)
    manifest_id = manifest_rows[0]["manifest_id"]
    paths = manifest_paths(manifest_id)
    _write_lockfile(paths["lock_file"])
    if paths["stop_file"].exists():
        paths["stop_file"].unlink()

    records = _load_records_for_manifest(manifest_rows)
    progress_db = open_progress_db(paths["progress_db"])
    cache_db = open_cache_db(paths["cache_db"])
    from src.repository.write_backend import active_write_backend, WRITE_BACKEND_SQLITE
    # Local cache/progress DBs are non-authoritative resumability aids.  The persistent
    # Treatment SoT is opened only in explicit SQLite mode; production writes directly to
    # treatment.* through clinic_runtime.
    final_db = open_final_db(FINAL_DB) if active_write_backend() == WRITE_BACKEND_SQLITE else None
    treatment_repo = _build_treatment_repo(final_db)
    seed_progress(progress_db, list(records))
    # The lockfile above guarantees no other live worker holds this manifest, so any
    # IN_PROGRESS rows here are leftovers from a prior crash/kill, not a concurrent
    # run -- reclaim them as PENDING so a crash mid-batch is actually resumable.
    reclaimed = progress_db.execute(
        "UPDATE clinic_progress SET status='PENDING' WHERE status='IN_PROGRESS'"
    ).rowcount
    if reclaimed:
        print(f"[{_now()}] reclaimed {reclaimed} clinic(s) left IN_PROGRESS by a prior crash")

    gate = DomainGate(concurrency)
    write_lock = threading.Lock()  # serialize final_db writes (still one-clinic-atomic)

    def process(clinic_id: int) -> None:
        record = records[clinic_id]
        domain = urlsplit(record["effective_official_hp_url"]).netloc
        transport = PinnedTransport()
        safe_fetcher = SafeFetcher(transport=transport, timeout=FETCH_TIMEOUT,
                                    max_bytes=FETCH_MAX_BYTES, interval=FETCH_INTERVAL)
        fetcher = _MeteredFetcher(safe_fetcher)
        meta: dict = {}
        try:
            rows, fetch_status, candidate_count = gate.run(
                domain, research_one_clinic, record, fetcher, meta
            )
        except Exception as exc:  # failure isolation: one clinic never kills the run
            with write_lock:
                mark_progress(progress_db, clinic_id, "ERROR", error=str(exc)[:500])
            return
        with write_lock:
            # Only a true fetch-level failure is retry-eligible progress state.
            # IDENTITY_NOT_VERIFIED wrote real REVIEW rows -- that is a completed,
            # non-retryable outcome, same as OK. clinic_research_status mirrors
            # this exact same decision, by construction (invariant: progress.sqlite3
            # DONE/FETCH_FAILED == clinic_research_status DONE/FETCH_FAILED).
            status = "FETCH_FAILED" if fetch_status == "FETCH_ERROR" else "DONE"
            attempts = progress_db.execute(
                "SELECT attempts FROM clinic_progress WHERE clinic_id=?", (clinic_id,)
            ).fetchone()[0]
            status_row = {
                "research_status": status,
                "candidate_count": candidate_count,
                "source_url": record["effective_official_hp_url"],
                "final_url": meta["pages"][0].url if meta.get("pages") else "",
                "identity_verified": fetch_status == "OK",
                "attempts": attempts,
                "last_error": meta.get("last_error", "") or fetch_status,
                "taxonomy_version": RULE_VERSION,
                "evidence_engine_version": EVIDENCE_ENGINE_VERSION,
                "manifest_id": record["manifest_id"],
                "git_commit_sha": record["git_commit_sha"],
                "researched_at": _now(),
            }
            # Stage4-D Gate2: persistent WRITE to the shared final results DB goes through the
            # Repository (same SQL/atomicity -- see write_clinic_atomic() above, now called
            # from inside SqliteTreatmentWriteRepository rather than directly from here).
            # cache.sqlite3/progress.sqlite3 stay local, unmanaged by the Repository (see
            # src/repository/treatment_write_contracts.py's module docstring for why).
            treatment_repo.write_clinic_result(clinic_id, rows, status_row)
            if "pages" in meta:
                cache_clinic_pages(cache_db, clinic_id, meta["pages"])
            mark_progress(progress_db, clinic_id, status, error=fetch_status, candidate_count=candidate_count)

    try:
        while True:
            if paths["stop_file"].exists():
                print(f"[{_now()}] stop requested, exiting cleanly")
                break
            batch = claim_next_batch(progress_db, retry_failed, batch_size)
            if not batch:
                print(f"[{_now()}] no more clinics to process")
                break
            print(f"[{_now()}] processing batch of {len(batch)} clinics "
                  f"({progress_summary(progress_db)['progress_pct']}% done before this batch)")
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {pool.submit(process, cid): cid for cid in batch}
                for future in as_completed(futures):
                    future.result()  # re-raise unexpected bugs; per-clinic errors already isolated above
    finally:
        try:
            paths["lock_file"].unlink(missing_ok=True)
        except OSError:
            pass

    summary = progress_summary(progress_db)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def cmd_start(args: argparse.Namespace) -> int:
    manifest_rows = load_manifest(Path(args.manifest))
    manifest_id = manifest_rows[0]["manifest_id"]
    paths = manifest_paths(manifest_id)
    if paths["progress_db"].exists() and not args.force:
        summary = progress_summary(open_progress_db(paths["progress_db"]))
        if summary["total"] and summary["counts"]["PENDING"] < summary["total"]:
            print("progress already exists for this manifest; use `resume` or pass --force to restart", file=sys.stderr)
            return 1
    run_worker(Path(args.manifest), retry_failed=False, concurrency=args.concurrency)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    run_worker(Path(args.manifest), retry_failed=args.retry_failed, concurrency=args.concurrency)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    manifest_rows = load_manifest(Path(args.manifest))
    manifest_id = manifest_rows[0]["manifest_id"]
    paths = manifest_paths(manifest_id)
    if not paths["progress_db"].exists():
        print(json.dumps({"manifest_id": manifest_id, "status": "NOT_STARTED"}, ensure_ascii=False, indent=2))
        return 0
    db = open_progress_db(paths["progress_db"])
    summary = progress_summary(db)
    running = False
    if paths["lock_file"].exists():
        try:
            os.kill(int(paths["lock_file"].read_text().strip()), 0)
            running = True
        except (ValueError, ProcessLookupError):
            running = False
    print(json.dumps({"manifest_id": manifest_id, "running": running, **summary}, ensure_ascii=False, indent=2))
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    manifest_rows = load_manifest(Path(args.manifest))
    manifest_id = manifest_rows[0]["manifest_id"]
    paths = manifest_paths(manifest_id)
    paths["stop_file"].write_text(_now())
    print(f"stop requested for manifest {manifest_id} (worker will exit after its current batch)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    for name, fn in (("start", cmd_start), ("resume", cmd_resume), ("status", cmd_status), ("stop", cmd_stop)):
        p = sub.add_parser(name)
        p.add_argument("--manifest", required=True)
        if name in ("start", "resume"):
            p.add_argument("--concurrency", type=int, default=GLOBAL_CONCURRENCY)
        if name == "start":
            p.add_argument("--force", action="store_true")
        if name == "resume":
            p.add_argument("--retry-failed", action="store_true")
        p.set_defaults(func=fn)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
