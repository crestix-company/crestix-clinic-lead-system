"""Offline re-evaluation of the Treatment department-context candidate guard.

This command never fetches a URL and never writes to any input database.  It
combines existing Treatment rows, formal MHLW departments, and local page_cache
sanitized_text, then emits an auditable candidate-guard-only Before/After diff.
Existing evidence statuses are preserved for every candidate that remains.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from src.enrichment.candidate_map import department_candidates
from src.enrichment.hp_analysis import keyword_match
from src.enrichment.treatment_taxonomy import phase7b_research_categories


RESULTS = (
    "KEEP_DEPARTMENT_MATCH",
    "KEEP_SPECIFIC_ALIAS",
    "KEEP_CONTEXT_MATCH",
    "DROP_AMBIGUOUS_CROSS_DEPARTMENT_ALIAS",
    "UNDETERMINED_CACHE_MISSING",
)
STATUS_ORDER = ("CONFIRMED", "REVIEW", "NOT_CONFIRMED")
COMDESK_INITIAL_CUTOFF = "2026-09-28T02:26:53.465716+00:00"


def _connect_ro(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _tables(db: sqlite3.Connection) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}


def _input_fingerprint(path: Path) -> dict:
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": str(path.resolve()), "size": stat.st_size, "sha256": digest.hexdigest()}


def _load_treatment_rows(db: sqlite3.Connection) -> tuple[list[dict], set[int]]:
    tables = _tables(db)
    table = "clinic_treatment_research_final" if "clinic_treatment_research_final" in tables else "clinic_treatment_research"
    if table not in tables:
        raise ValueError("research DB has no supported Treatment result table")
    cols = _columns(db, table)
    category = "treatment_category_name" if "treatment_category_name" in cols else "treatment_category"
    status = "research_status" if "research_status" in cols else "status"
    url = "source_url" if "source_url" in cols else "evidence_url"
    required = {"clinic_id", category, status}
    if not required <= cols:
        raise ValueError(f"unsupported {table} schema; missing {sorted(required - cols)}")
    selected = {
        "clinic_id": "clinic_id", "treatment_category": category,
        "existing_status": status,
        "matched_alias": "matched_alias" if "matched_alias" in cols else "''",
        "source_url": url if url in cols else "''",
    }
    query = "SELECT " + ", ".join(f'{source} AS "{name}"' for name, source in selected.items()) + f' FROM "{table}"'
    rows = [dict(row) for row in db.execute(query) if row["existing_status"] in STATUS_ORDER]

    excluded: set[int] = set()
    if "clinic_research_status" in tables:
        status_cols = _columns(db, "clinic_research_status")
        if "research_status" in status_cols:
            excluded.update(row[0] for row in db.execute(
                "SELECT clinic_id FROM clinic_research_status WHERE research_status='IDENTITY_NOT_VERIFIED'"
            ))
        if "last_error" in status_cols:
            excluded.update(row[0] for row in db.execute(
                "SELECT clinic_id FROM clinic_research_status WHERE last_error='IDENTITY_NOT_VERIFIED'"
            ))
    return rows, excluded


def _load_clinics(db: sqlite3.Connection, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    if "clinics" not in _tables(db):
        raise ValueError("clinic DB has no clinics table")
    output = {}
    ordered = sorted(ids)
    for start in range(0, len(ordered), 900):
        batch = ordered[start:start + 900]
        marks = ",".join("?" for _ in batch)
        query = f"""SELECT id, clinic_name FROM clinics WHERE id IN ({marks})
          AND merged_into IS NULL AND merge_hold=0 AND active=1 AND uuid=''
          AND first_seen_at < ?
          AND NOT (
            exclude_reason IN ('hospital','center')
            OR COALESCE(json_extract(effective_json,'$.facility_type'),'')='病院'
            OR clinic_name LIKE '%病院%' OR clinic_name LIKE '%センター%'
          )"""
        for row in db.execute(query, [*batch, COMDESK_INITIAL_CUTOFF]):
            output[int(row[0])] = row[1]
    return output


def _load_departments(db: sqlite3.Connection, ids: set[int]) -> dict[int, list[str]]:
    tables = _tables(db)
    if "clinic_mhlw_departments_final" in tables:
        table, column = "clinic_mhlw_departments_final", "mhlw_department_name"
    elif "clinic_mhlw_departments" in tables:
        table = "clinic_mhlw_departments"
        cols = _columns(db, table)
        column = "mhlw_department_name" if "mhlw_department_name" in cols else "department_name"
    else:
        raise ValueError("MHLW DB has no supported formal-department table")
    output: dict[int, set[str]] = defaultdict(set)
    ordered = sorted(ids)
    for start in range(0, len(ordered), 900):
        batch = ordered[start:start + 900]
        marks = ",".join("?" for _ in batch)
        query = f'SELECT clinic_id, "{column}" FROM "{table}" WHERE clinic_id IN ({marks})'
        for clinic_id, department in db.execute(query, batch):
            if department:
                output[int(clinic_id)].add(str(department))
    return {clinic_id: sorted(values) for clinic_id, values in output.items()}


def _load_cache(cache_root: Path, target_ids: set[int]) -> tuple[dict[int, list[dict]], dict]:
    cache_paths = sorted(cache_root.rglob("cache.sqlite3")) if cache_root.is_dir() else []
    pages: dict[int, dict[str, dict]] = defaultdict(dict)
    per_db = []
    for path in cache_paths:
        with _connect_ro(path) as db:
            if "page_cache" not in _tables(db):
                per_db.append({"path": str(path), "page_rows": 0, "target_clinics": 0, "usable": False})
                continue
            cols = _columns(db, "page_cache")
            required = {"clinic_id", "page_url", "sanitized_text"}
            if not required <= cols:
                per_db.append({"path": str(path), "page_rows": 0, "target_clinics": 0, "usable": False})
                continue
            row_count = db.execute("SELECT COUNT(*) FROM page_cache").fetchone()[0]
            db_targets = set()
            for row in db.execute(
                "SELECT clinic_id,page_url,final_url,page_title,sanitized_text,fetch_status FROM page_cache"
            ):
                clinic_id = int(row["clinic_id"])
                if clinic_id not in target_ids:
                    continue
                db_targets.add(clinic_id)
                key = row["final_url"] or row["page_url"]
                pages[clinic_id][key] = dict(row)
            per_db.append({"path": str(path), "page_rows": row_count, "target_clinics": len(db_targets), "usable": True})
    flattened = {clinic_id: list(by_url.values()) for clinic_id, by_url in pages.items()}
    counts = [len(flattened.get(clinic_id, [])) for clinic_id in target_ids if clinic_id in flattened]
    audit = {
        "cache_db_count": len(cache_paths), "usable_cache_db_count": sum(x["usable"] for x in per_db),
        "target_clinic_count": len(target_ids), "covered_target_clinics": len(flattened),
        "missing_target_clinics": len(target_ids - flattened.keys()),
        "coverage_pct": round(100 * len(flattened) / len(target_ids), 2) if target_ids else 0,
        "pages_per_cached_clinic": {
            "min": min(counts) if counts else 0, "median": statistics.median(counts) if counts else 0,
            "mean": round(statistics.mean(counts), 2) if counts else 0,
            "p95": sorted(counts)[max(0, int(len(counts) * .95) - 1)] if counts else 0,
            "max": max(counts) if counts else 0,
        },
        "sanitized_text_re_evaluation": bool(flattened), "cache_databases": per_db,
    }
    return flattened, audit


def _sentences(pages: list[dict]) -> list[tuple[str, str]]:
    output = []
    for page in pages:
        url = page.get("final_url") or page.get("page_url") or ""
        for sentence in re.split(r"[。！？!?\n]+", page.get("sanitized_text") or ""):
            if sentence.strip():
                output.append((sentence.strip(), url))
    return output


def _classify(category: str, departments: list[str], pages: list[dict] | None) -> tuple[str, str, str]:
    if category in department_candidates(departments):
        return "KEEP_DEPARTMENT_MATCH", "formal department maps to Treatment", ""
    if pages is None:
        return "UNDETERMINED_CACHE_MISSING", "no page_cache rows; candidate retained", ""
    definition = phase7b_research_categories().get(category)
    if not definition:
        return "UNDETERMINED_CACHE_MISSING", "category absent from active taxonomy; candidate retained", ""
    sentences = _sentences(pages)
    for sentence, url in sentences:
        if keyword_match(category, sentence):
            return "KEEP_SPECIFIC_ALIAS", "canonical Treatment name found in cached sentence", url
    matched = []
    for alias in definition.get("aliases", ()):
        for sentence, url in sentences:
            if keyword_match(alias, sentence):
                matched.append((alias, sentence, url))
    if not matched:
        return "UNDETERMINED_CACHE_MISSING", "cache exists but does not reproduce the prior alias hit; candidate retained", ""
    required = definition.get("cross_department_guard", {}).get("context_required_aliases", {})
    ambiguous = []
    for alias, sentence, url in matched:
        keywords = tuple(required.get(alias, ()))
        if not keywords:
            return "KEEP_SPECIFIC_ALIAS", f"specific alias found: {alias}", url
        if any(keyword in sentence for keyword in keywords):
            return "KEEP_CONTEXT_MATCH", f"ambiguous alias {alias} has same-sentence context", url
        ambiguous.append(alias)
    aliases = ", ".join(sorted(set(ambiguous)))
    return "DROP_AMBIGUOUS_CROSS_DEPARTMENT_ALIAS", f"cross-department ambiguous alias lacks same-sentence context: {aliases}", matched[0][2]


def _status_group(rows: list[dict]) -> str:
    statuses = {row["existing_status"] for row in rows}
    if "CONFIRMED" in statuses:
        return "CONFIRMEDあり"
    if "REVIEW" in statuses:
        return "REVIEWのみ"
    if "NOT_CONFIRMED" in statuses:
        return "NOT_CONFIRMEDのみ"
    return "新Guard上、対象Treatment候補なし"


def run(args: argparse.Namespace) -> dict:
    paths = [Path(args.clinic_db), Path(args.research_db), Path(args.mhlw_db)]
    before_fingerprints = [_input_fingerprint(path) for path in paths]
    with _connect_ro(paths[1]) as research_db:
        treatment_rows, excluded_ids = _load_treatment_rows(research_db)
    candidate_ids = {int(row["clinic_id"]) for row in treatment_rows}
    with _connect_ro(paths[0]) as clinic_db:
        clinic_names = _load_clinics(clinic_db, candidate_ids)
    with _connect_ro(paths[2]) as mhlw_db:
        departments = _load_departments(mhlw_db, candidate_ids)
    # Frozen population used by the initial 4,512-row Comdesk candidate export:
    # eligible active clinics with at least one formal department and Treatment row.
    initial_population = set(clinic_names) & set(departments)
    target_ids = initial_population - excluded_ids
    treatment_rows = [row for row in treatment_rows if int(row["clinic_id"]) in target_ids]
    clinic_names = {clinic_id: clinic_names[clinic_id] for clinic_id in target_ids}
    departments = {clinic_id: departments[clinic_id] for clinic_id in target_ids}
    cache_pages, cache_audit = _load_cache(Path(args.cache_root), target_ids)

    diffs = []
    before_by_clinic: dict[int, list[dict]] = defaultdict(list)
    after_by_clinic: dict[int, list[dict]] = defaultdict(list)
    for row in treatment_rows:
        clinic_id = int(row["clinic_id"])
        before_by_clinic[clinic_id].append(row)
        result, reason, cache_url = _classify(
            row["treatment_category"], departments.get(clinic_id, []), cache_pages.get(clinic_id)
        )
        diff = {
            "clinic_id": clinic_id, "clinic_name": clinic_names.get(clinic_id, ""),
            "formal_departments": " / ".join(departments.get(clinic_id, [])),
            "treatment_category": row["treatment_category"], "existing_status": row["existing_status"],
            "matched_alias": row.get("matched_alias", ""), "guard_result": result,
            "guard_reason": reason, "source_url": cache_url or row.get("source_url", ""),
        }
        diffs.append(diff)
        if result != "DROP_AMBIGUOUS_CROSS_DEPARTMENT_ALIAS":
            after_by_clinic[clinic_id].append(row)

    summaries = []
    for clinic_id in sorted(target_ids):
        before_rows, after_rows = before_by_clinic[clinic_id], after_by_clinic[clinic_id]
        summaries.append({
            "clinic_id": clinic_id, "clinic_name": clinic_names.get(clinic_id, ""),
            "formal_departments": " / ".join(departments.get(clinic_id, [])),
            "before_status_group": _status_group(before_rows), "after_status_group": _status_group(after_rows),
            "removed_candidate_count": len(before_rows) - len(after_rows),
            "remaining_candidate_count": len(after_rows), "cache_available": clinic_id in cache_pages,
        })

    before_counts = Counter(item["before_status_group"] for item in summaries)
    after_counts = Counter(item["after_status_group"] for item in summaries)
    removed = [row for row in diffs if row["guard_result"] == "DROP_AMBIGUOUS_CROSS_DEPARTMENT_ALIAS"]
    undetermined = [row for row in diffs if row["guard_result"] == "UNDETERMINED_CACHE_MISSING"]
    metrics = {
        "mode": "Candidate Guard offline re-evaluation",
        "before": {key: before_counts[key] for key in ("CONFIRMEDあり", "REVIEWのみ", "NOT_CONFIRMEDのみ")}
                  | {"合計": len(summaries)},
        "after_guard": {key: after_counts[key] for key in (
            "CONFIRMEDあり", "REVIEWのみ", "NOT_CONFIRMEDのみ", "新Guard上、対象Treatment候補なし"
        )} | {"合計": len(summaries)},
        "removed_treatment_rows": len(removed),
        "clinics_with_removed_candidates": len({row["clinic_id"] for row in removed}),
        "review_to_zero_clinics": sum(
            row["before_status_group"] == "REVIEWのみ" and row["after_status_group"] == "新Guard上、対象Treatment候補なし"
            for row in summaries
        ),
        "cache_missing_clinics": cache_audit["missing_target_clinics"],
        "undetermined_rows": len(undetermined),
        "undetermined_clinics": len({row["clinic_id"] for row in undetermined}),
        "identity_not_verified_excluded": len(initial_population & excluded_ids),
        "initial_comdesk_candidate_clinics": len(initial_population),
        "guard_result_counts": dict(Counter(row["guard_result"] for row in diffs)),
        "http_request_count": 0,
        "evidence_statuses_changed": 0,
    }
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "clinic_summary.csv", summaries)
    _write_csv(output_dir / "treatment_diff.csv", diffs)
    after_fingerprints = [_input_fingerprint(path) for path in paths]
    report = {
        "metrics": metrics, "cache_audit": cache_audit,
        "production_inputs_unchanged": before_fingerprints == after_fingerprints,
        "input_fingerprints": after_fingerprints,
    }
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clinic-db", required=True)
    parser.add_argument("--research-db", required=True)
    parser.add_argument("--mhlw-db", required=True)
    parser.add_argument("--cache-root", required=True)
    parser.add_argument("--output-dir", required=True)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    run(parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
