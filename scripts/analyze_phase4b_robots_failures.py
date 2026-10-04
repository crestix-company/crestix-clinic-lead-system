"""Read-only diagnosis of Phase 4-B robots.txt safety failures.

The sample is deterministic and fixed to an artifact before probing.  HTTP
diagnosis delegates redirect and fail-closed behavior to the production
SafeFetcher._fetch_robots_text(); TracingTransport only records its decisions.
It never fetches a clinic page and never writes to any SQLite database.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from src.enrichment.hp_analysis import host
from src.enrichment.safe_web import (
    MAX_ROBOTS_REDIRECT_HOPS,
    PinnedTransport,
    SafeFetcher,
    USER_AGENT,
    WebError,
    validate_url,
)

SEED = "hybrid-treatment-phase4b-robots-diagnosis-20261003-v1"
CACHE = Path("data/hybrid_phase4b/structured_cache.sqlite3")
POPULATION = Path("artifacts/hybrid_phase4b/population.csv")
PRODUCTION_CLINIC_DB = Path("/Users/maekawahiroyuki/CrestixData/clinic-lead/clinics.sqlite3")
TREATMENT_SIDECAR = Path("/Users/maekawahiroyuki/CrestixData/clinic-lead/treatment_research_final.sqlite3")
MHLW_DB = Path("mhlw_dry_run/clinic_mhlw_departments_final.sqlite3")
OUT = Path("artifacts/hybrid_phase4b_retry")
SAMPLE_NAME = "robots_diagnosis_sample.csv"
DIAGNOSIS_NAME = "robots_failure_diagnosis.csv"
SUMMARY_NAME = "robots_failure_summary.json"
DIAGNOSIS_ALL_NAME = "robots_failure_diagnosis_all.csv"
SUMMARY_ALL_NAME = "robots_failure_summary_all.json"
SAFE_TARGETS_NAME = "robots_safe_retry_targets.csv"
CHECKPOINT_ALL_NAME = "robots_failure_diagnosis_all.checkpoint.jsonl"
CANARY_NAME = "robots_safe_retry_canary_50.csv"
BEFORE_NAME = "before_safe_retry_summary.json"
RETRY_RESULTS_NAME = "retry_results.csv"
AFTER_NAME = "after_safe_retry_summary.json"
ROBOTS_SAFE_ERROR = "robots.txtを安全に確認できないため取得を見送りました。"
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
REP_DIRECTIVE_RE = re.compile(r"(?im)^\s*(user-agent|allow|disallow|crawl-delay)\s*:")
SAMPLE_QUOTAS = {
    "NO_SIGNAL": 40,
    "NO_REPLAY_DATA": 40,
    "CANDIDATE_ONLY": 17,
    "NOT_CONFIRMED": 3,
}
CATEGORIES = (
    "VALID_ROBOTS", "ROBOTS_NOT_FOUND_4XX", "ROBOTS_429", "ROBOTS_5XX",
    "ROBOTS_TIMEOUT", "ROBOTS_DNS", "ROBOTS_SSL", "ROBOTS_REDIRECT_LOOP",
    "ROBOTS_UNSAFE_REDIRECT", "ROBOTS_HTML_ERROR_PAGE", "ROBOTS_EMPTY",
    "ROBOTS_PARSE_ERROR", "OTHER",
)


class TracingTransport:
    def __init__(self, delegate=None):
        self.delegate = delegate or PinnedTransport()
        self.events: list[dict] = []

    def get(self, url, timeout, max_bytes):
        try:
            response = self.delegate.get(url, timeout, max_bytes)
        except WebError as exc:
            self.events.append({"url": url, "error": str(exc)})
            raise
        self.events.append({
            "url": url,
            "status": response.status,
            "headers": response.headers,
            "body": response.body,
        })
        return response


def load_failure_population(cache_path: Path, population_path: Path) -> list[dict]:
    with population_path.open(encoding="utf-8-sig", newline="") as f:
        names = {int(r["clinic_id"]): r.get("clinic_name", "") for r in csv.DictReader(f)}
    with sqlite3.connect(f"file:{cache_path.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        rows = db.execute(
            """SELECT clinic_id,sampling_group,initial_url,error AS previous_error FROM clinic_fetch
               WHERE fetch_status='FETCH_FAILED' AND error=? ORDER BY clinic_id""",
            (ROBOTS_SAFE_ERROR,),
        ).fetchall()
    return [{**dict(r), "clinic_name": names.get(int(r["clinic_id"]), "")} for r in rows]


def select_sample(rows: list[dict]) -> list[dict]:
    selected = []
    for group, quota in SAMPLE_QUOTAS.items():
        pool = [r for r in rows if r["sampling_group"] == group]
        pool.sort(key=lambda r: hashlib.sha256(
            f"{SEED}|{group}|{r['clinic_id']}".encode()
        ).hexdigest())
        if len(pool) < quota:
            raise AssertionError(f"insufficient {group} rows: {len(pool)} < {quota}")
        selected.extend(pool[:quota])
    selected.sort(key=lambda r: int(r["clinic_id"]))
    return selected


def write_sample(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["clinic_id", "clinic_name", "sampling_group", "initial_url", "robots_url", "seed"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            p = urlsplit(row["initial_url"])
            writer.writerow({
                **row,
                "robots_url": f"{p.scheme}://{p.netloc}/robots.txt",
                "seed": SEED,
            })


def classify(events: list[dict], robots_text: str | None, initial_url: str) -> tuple[str, str, bool]:
    if not events:
        return "OTHER", "robots request produced no transport event", False
    last = events[-1]
    error = last.get("error", "")
    if error:
        if "URLまたはDNS" in error or "DNSの接続先" in error:
            return "ROBOTS_DNS", error, False
        if "SSL証明書" in error:
            return "ROBOTS_SSL", error, False
        if "タイムアウト" in error or "時間上限" in error or "接続・SSL" in error:
            return "ROBOTS_TIMEOUT", error, False
        return "OTHER", error, False

    status = int(last.get("status", 0))
    if status in REDIRECT_STATUSES:
        location = last.get("headers", {}).get("location", "")
        if location:
            target = urljoin(last["url"], location)
            try:
                validate_url(target, resolve=False)
            except WebError as exc:
                return "ROBOTS_UNSAFE_REDIRECT", str(exc), False
        if len(events) >= MAX_ROBOTS_REDIRECT_HOPS + 1:
            return "ROBOTS_REDIRECT_LOOP", f"redirect unresolved after {MAX_ROBOTS_REDIRECT_HOPS} hops", False
        return "OTHER", "redirect response without a usable final result", False
    if status == 429:
        return "ROBOTS_429", "robots endpoint returned HTTP 429", False
    if 500 <= status < 600:
        return "ROBOTS_5XX", f"robots endpoint returned HTTP {status}", False
    if 400 <= status < 500:
        return "ROBOTS_NOT_FOUND_4XX", f"robots endpoint returned HTTP {status}", True
    if status != 200:
        return "OTHER", f"robots endpoint returned HTTP {status}", False

    body = last.get("body", b"")
    content_type = last.get("headers", {}).get("content-type", "")
    text = robots_text if robots_text is not None else body.decode("utf-8", "ignore")
    if not body or not text.strip():
        return "ROBOTS_EMPTY", "HTTP 200 with an empty robots body", True
    is_html = "html" in content_type.lower() or bool(re.search(r"(?i)<html|<!doctype html", text))
    has_directive = bool(REP_DIRECTIVE_RE.search(text))
    if is_html and not has_directive:
        return "ROBOTS_HTML_ERROR_PAGE", "HTTP 200 HTML without a robots directive", False
    if not has_directive:
        return "ROBOTS_PARSE_ERROR", "non-empty body has no recognized robots directive", False
    parser = RobotFileParser()
    try:
        parser.parse(text.splitlines())
        allowed = parser.can_fetch(USER_AGENT, initial_url)
    except Exception as exc:
        return "ROBOTS_PARSE_ERROR", f"{type(exc).__name__}: {exc}", False
    return "VALID_ROBOTS", "robots directives parsed; initial URL allowed" if allowed else "explicit rules disallow initial URL", allowed


def probe(row: dict, timeout: float, max_bytes: int) -> dict:
    transport = TracingTransport()
    fetcher = SafeFetcher(transport=transport, timeout=timeout, max_bytes=max_bytes, interval=0)
    p = urlsplit(row["initial_url"])
    origin = f"{p.scheme}://{p.netloc}"
    robots_text = fetcher._fetch_robots_text(origin)
    category, detail, safe = classify(transport.events, robots_text, row["initial_url"])
    last = transport.events[-1] if transport.events else {}
    final_url = last.get("url", origin + "/robots.txt")
    redirect_count = sum(e.get("status") in REDIRECT_STATUSES for e in transport.events)
    return {
        **row,
        "robots_url": origin + "/robots.txt",
        "robots_result_category": category,
        "http_status": last.get("status", ""),
        "redirect_hops": redirect_count,
        "final_robots_url": final_url,
        "final_host": host(final_url),
        "content_type": last.get("headers", {}).get("content-type", ""),
        "error_detail": detail,
        "safe_retry_candidate": str(bool(safe)).lower(),
        "diagnosed_at": datetime.now(timezone.utc).isoformat(),
        "trace_json": json.dumps([
            {k: v for k, v in e.items() if k != "body"} for e in transport.events
        ], ensure_ascii=False),
    }


def read_sample(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_diagnosis(rows: list[dict], path: Path) -> None:
    fields = [
        "clinic_id", "clinic_name", "sampling_group", "initial_url", "robots_url",
        "robots_result_category", "http_status", "redirect_hops", "final_robots_url",
        "final_host", "content_type", "error_detail", "safe_retry_candidate", "trace_json",
        "diagnosed_at", "previous_error",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows({k: row.get(k, "") for k in fields} for row in rows)


def write_safe_targets(rows: list[dict], path: Path) -> list[dict]:
    safe_categories = {"VALID_ROBOTS", "ROBOTS_NOT_FOUND_4XX", "ROBOTS_EMPTY"}
    safe = [r for r in rows if r["robots_result_category"] in safe_categories
            and r["safe_retry_candidate"] == "true"]
    fields = ["clinic_id", "clinic_name", "sampling_group", "initial_url", "robots_category",
              "robots_final_url", "diagnosed_at", "previous_error"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in safe:
            writer.writerow({
                "clinic_id": row["clinic_id"],
                "clinic_name": row["clinic_name"],
                "sampling_group": row["sampling_group"],
                "initial_url": row["initial_url"],
                "robots_category": row["robots_result_category"],
                "robots_final_url": row["final_robots_url"],
                "diagnosed_at": row["diagnosed_at"],
                "previous_error": row.get("previous_error", ROBOTS_SAFE_ERROR),
            })
    return safe


def load_checkpoint(path: Path) -> dict[int, dict]:
    rows = {}
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as f:
        for line_number, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                rows[int(row["clinic_id"])] = row
            except (ValueError, KeyError, TypeError) as exc:
                raise RuntimeError(f"invalid checkpoint line {line_number}: {exc}") from exc
    return rows


def append_checkpoint(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()


def build_summary(rows: list[dict], group_names) -> dict:
    counts = {category: sum(r["robots_result_category"] == category for r in rows) for category in CATEGORIES}
    group_counts = {group: sum(r["sampling_group"] == group for r in rows) for group in group_names}
    return {
        "seed": SEED,
        "diagnosis_rows": len(rows),
        "diagnosis_group_counts": group_counts,
        "category_counts": counts,
        "safe_retry_candidates": sum(r["safe_retry_candidate"] == "true" for r in rows),
        "production_db_changed": False,
        "phase4b_cache_changed": False,
        "clinic_pages_fetched": False,
        "robots_requests_only": True,
    }


def select_canary(rows: list[dict], size: int = 50) -> list[dict]:
    """Deterministically balance both sampling group and robots category."""
    pool = list(rows)
    selected = []
    group_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    while pool and len(selected) < size:
        pool.sort(key=lambda r: (
            group_counts.get(r["sampling_group"], 0),
            category_counts.get(r["robots_category"], 0),
            hashlib.sha256(f"{SEED}|canary|{r['clinic_id']}".encode()).hexdigest(),
        ))
        row = pool.pop(0)
        selected.append(row)
        group_counts[row["sampling_group"]] = group_counts.get(row["sampling_group"], 0) + 1
        category_counts[row["robots_category"]] = category_counts.get(row["robots_category"], 0) + 1
    if len(selected) != size:
        raise AssertionError(f"insufficient safe rows for canary: {len(selected)} != {size}")
    return sorted(selected, key=lambda r: int(r["clinic_id"]))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-db", type=Path, default=CACHE)
    ap.add_argument("--population-csv", type=Path, default=POPULATION)
    ap.add_argument("--output-dir", type=Path, default=OUT)
    ap.add_argument("--clinic-db", type=Path, default=PRODUCTION_CLINIC_DB)
    ap.add_argument("--treatment-sidecar", type=Path, default=TREATMENT_SIDECAR)
    ap.add_argument("--mhlw-db", type=Path, default=MHLW_DB)
    ap.add_argument("--prepare-sample", action="store_true")
    ap.add_argument("--probe-sample", action="store_true")
    ap.add_argument("--probe-all", action="store_true")
    ap.add_argument("--prepare-canary", action="store_true")
    ap.add_argument("--summarize-canary", action="store_true")
    ap.add_argument("--timeout", type=float, default=12)
    ap.add_argument("--max-bytes", type=int, default=1_500_000)
    ap.add_argument("--interval", type=float, default=.6)
    args = ap.parse_args()
    if sum((args.prepare_sample, args.probe_sample, args.probe_all, args.prepare_canary,
            args.summarize_canary)) != 1:
        ap.error("choose exactly one operation")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    sample_path = args.output_dir / SAMPLE_NAME
    if args.summarize_canary:
        canary_path = args.output_dir / CANARY_NAME
        canary = read_sample(canary_path)
        ids = [int(r["clinic_id"]) for r in canary]
        by_id = {int(r["clinic_id"]): r for r in canary}
        placeholders = ",".join("?" for _ in ids)
        with sqlite3.connect(f"file:{args.cache_db.resolve()}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            current = {int(r["clinic_id"]): dict(r) for r in db.execute(
                f"SELECT * FROM clinic_fetch WHERE clinic_id IN ({placeholders})", ids)}
            history = {}
            for r in db.execute(
                f"""SELECT h.* FROM fetch_attempt_history h JOIN (
                       SELECT clinic_id,max(history_id) history_id FROM fetch_attempt_history
                       WHERE clinic_id IN ({placeholders}) GROUP BY clinic_id
                     ) latest ON latest.history_id=h.history_id""", ids):
                history[int(r["clinic_id"])] = dict(r)
            remaining_robots = int(db.execute(
                "SELECT count(*) FROM clinic_fetch WHERE fetch_status='FETCH_FAILED' AND error=?",
                (ROBOTS_SAFE_ERROR,)).fetchone()[0])
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        clinic_results = {}
        with (Path("artifacts/hybrid_phase4b") / "clinic_results.csv").open(
                encoding="utf-8-sig", newline="") as f:
            clinic_results = {int(r["clinic_id"]): r for r in csv.DictReader(f) if int(r["clinic_id"]) in set(ids)}
        result_rows = []
        for cid in ids:
            old, new, source = history[cid], current[cid], by_id[cid]
            result_rows.append({
                "clinic_id": cid, "clinic_name": source["clinic_name"],
                "sampling_group": source["sampling_group"], "robots_category": source["robots_category"],
                "previous_status": old["fetch_status"], "previous_error": old["error"],
                "new_status": new["fetch_status"], "new_error": new["error"],
                "identity_verified": new["identity_verified"], "pages_fetched": new["pages_fetched"],
                "http_request_count": new["http_request_count"], "completed_at": new["completed_at"],
            })
        result_fields = list(result_rows[0])
        with (args.output_dir / RETRY_RESULTS_NAME).open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=result_fields)
            writer.writeheader(); writer.writerows(result_rows)
        before = json.loads((args.output_dir / BEFORE_NAME).read_text(encoding="utf-8"))
        statuses = {s: sum(r["new_status"] == s for r in result_rows)
                    for s in ("OK", "IDENTITY_NOT_VERIFIED", "FETCH_FAILED", "ERROR")}
        outcomes = {s: sum(clinic_results.get(cid, {}).get("outcome") == s for cid in ids)
                    for s in ("CONFIRMED", "MENTIONED", "REVIEW")}
        after_hashes = {
            "production_db_sha256": file_sha256(args.clinic_db),
            "treatment_sidecar_sha256": file_sha256(args.treatment_sidecar),
            "mhlw_db_sha256": file_sha256(args.mhlw_db),
            "phase4b_cache_sha256": file_sha256(args.cache_db),
        }
        after = {
            "retry_scope": "CANARY_50", "retry_target_count": len(ids),
            "retry_statuses": statuses,
            "ok_delta": statuses["OK"], "identity_not_verified_delta": statuses["IDENTITY_NOT_VERIFIED"],
            "fetch_failed_delta": -len(ids) + statuses["FETCH_FAILED"], "error_count": statuses["ERROR"],
            "new_confirmed_clinics": outcomes["CONFIRMED"],
            "new_mentioned_clinics": outcomes["MENTIONED"], "new_review_clinics": outcomes["REVIEW"],
            "remaining_robots_failures": remaining_robots,
            **after_hashes,
            "production_db_sha_unchanged": after_hashes["production_db_sha256"] == before["production_db_sha256"],
            "treatment_sidecar_sha_unchanged": after_hashes["treatment_sidecar_sha256"] == before["treatment_sidecar_sha256"],
            "mhlw_db_sha_unchanged": after_hashes["mhlw_db_sha256"] == before["mhlw_db_sha256"],
            "cache_integrity_check": integrity,
        }
        (args.output_dir / AFTER_NAME).write_text(
            json.dumps(after, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(after, ensure_ascii=False, indent=2))
        return 0
    if args.prepare_canary:
        safe_path = args.output_dir / SAFE_TARGETS_NAME
        safe = read_sample(safe_path)
        canary = select_canary(safe)
        canary_path = args.output_dir / CANARY_NAME
        fields = list(canary[0])
        with canary_path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(canary)
        safe_ids = {int(r["clinic_id"]) for r in safe}
        canary_ids = {int(r["clinic_id"]) for r in canary}
        with sqlite3.connect(f"file:{args.cache_db.resolve()}?mode=ro", uri=True) as db:
            db.execute("PRAGMA query_only=ON")
            ok = {int(r[0]) for r in db.execute("SELECT clinic_id FROM clinic_fetch WHERE fetch_status='OK'")}
            unverified = {int(r[0]) for r in db.execute("SELECT clinic_id FROM clinic_fetch WHERE fetch_status='IDENTITY_NOT_VERIFIED'")}
            nonrobots = {int(r[0]) for r in db.execute(
                """SELECT clinic_id FROM clinic_fetch WHERE fetch_status IN ('FETCH_FAILED','ERROR')
                   AND NOT(fetch_status='FETCH_FAILED' AND error=?)""", (ROBOTS_SAFE_ERROR,))}
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        before = {
            "safe_target_count": len(safe_ids), "canary_target_count": len(canary_ids),
            "safe_intersect_existing_ok": len(safe_ids & ok),
            "safe_intersect_identity_not_verified": len(safe_ids & unverified),
            "safe_intersect_nonrobots_fetch_failed": len(safe_ids & nonrobots),
            "canary_subset_of_safe": canary_ids <= safe_ids,
            "production_db_sha256": file_sha256(args.clinic_db),
            "treatment_sidecar_sha256": file_sha256(args.treatment_sidecar),
            "mhlw_db_sha256": file_sha256(args.mhlw_db),
            "phase4b_cache_sha256": file_sha256(args.cache_db),
            "phase4b_cache_integrity_check": integrity,
            "http_started": False,
        }
        (args.output_dir / BEFORE_NAME).write_text(
            json.dumps(before, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({
            "safe_population": len(safe), "canary_rows": len(canary),
            "group_counts": {g: sum(r["sampling_group"] == g for r in canary) for g in SAMPLE_QUOTAS},
            "category_counts": {c: sum(r["robots_category"] == c for r in canary)
                                for c in ("VALID_ROBOTS", "ROBOTS_NOT_FOUND_4XX", "ROBOTS_EMPTY")},
            "canary_path": str(canary_path), "before_summary": str(args.output_dir / BEFORE_NAME),
            "http_started": False,
        }, ensure_ascii=False, indent=2))
        return 0
    if args.prepare_sample:
        population = load_failure_population(args.cache_db, args.population_csv)
        if len(population) != 2138:
            raise AssertionError(f"robots failure population changed: {len(population)} != 2138")
        sample = select_sample(population)
        write_sample(sample, sample_path)
        print(json.dumps({"population": len(population), "sample": len(sample), "quotas": SAMPLE_QUOTAS,
                          "sample_path": str(sample_path), "http_started": False}, ensure_ascii=False, indent=2))
        return 0

    if args.probe_all:
        population = load_failure_population(args.cache_db, args.population_csv)
        if len(population) != 2138:
            raise AssertionError(f"robots failure population changed: {len(population)} != 2138")
        checkpoint_path = args.output_dir / CHECKPOINT_ALL_NAME
        completed = load_checkpoint(checkpoint_path)
        population_ids = {int(r["clinic_id"]) for r in population}
        unexpected = set(completed) - population_ids
        if unexpected:
            raise AssertionError(f"checkpoint contains unexpected clinic_ids: {sorted(unexpected)[:10]}")
        for i, row in enumerate(population, 1):
            cid = int(row["clinic_id"])
            if cid in completed:
                continue
            result = probe(row, args.timeout, args.max_bytes)
            append_checkpoint(checkpoint_path, result)
            completed[cid] = result
            if i % 25 == 0 or i == len(population):
                print(f"robots diagnosis all {i}/{len(population)} completed={len(completed)} "
                      f"clinic_id={cid} {result['robots_result_category']}", flush=True)
            if len(completed) != len(population):
                time.sleep(args.interval)
        rows = [completed[int(r["clinic_id"])] for r in population]
        diagnosis_path = args.output_dir / DIAGNOSIS_ALL_NAME
        summary_path = args.output_dir / SUMMARY_ALL_NAME
        safe_path = args.output_dir / SAFE_TARGETS_NAME
        write_diagnosis(rows, diagnosis_path)
        safe = write_safe_targets(rows, safe_path)
        summary = build_summary(rows, SAMPLE_QUOTAS)
        summary["safe_retry_target_rows"] = len(safe)
        summary["diagnosis_path"] = str(diagnosis_path)
        summary["safe_targets_path"] = str(safe_path)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    sample = read_sample(sample_path)
    if len(sample) > 100:
        raise AssertionError(f"diagnostic sample exceeds 100: {len(sample)}")
    rows = []
    for i, row in enumerate(sample, 1):
        rows.append(probe(row, args.timeout, args.max_bytes))
        print(f"robots diagnosis {i}/{len(sample)} clinic_id={row['clinic_id']} {rows[-1]['robots_result_category']}", flush=True)
        if i != len(sample):
            time.sleep(args.interval)
    diagnosis_path = args.output_dir / DIAGNOSIS_NAME
    summary_path = args.output_dir / SUMMARY_NAME
    write_diagnosis(rows, diagnosis_path)
    summary = build_summary(rows, SAMPLE_QUOTAS)
    summary["sample_rows"] = summary.pop("diagnosis_rows")
    summary["sample_group_counts"] = summary.pop("diagnosis_group_counts")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
