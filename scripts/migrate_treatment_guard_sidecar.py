"""One-time, no-network migration for the Treatment candidate guard.

The source sidecar is first copied and fully validated. Production is changed
only with --apply-production, after the copy matches every frozen expectation.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from src.enrichment.hp_analysis import keyword_match
from src.enrichment.treatment_taxonomy import phase7b_research_categories

EXPECTED = {
    "drop_rows": 618,
    "drop_clinics": 599,
    "drop_categories": {
        "下肢静脈瘤血管内治療": 556,
        "歯科インプラント": 43,
        "輪郭骨切り術": 19,
    },
    "after": {
        "CONFIRMEDあり": 969,
        "REVIEWのみ": 1169,
        "NOT_CONFIRMEDのみ": 1444,
        "新Guard上、対象Treatment候補なし": 134,
        "合計": 3716,
    },
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        db = sqlite3.connect(f"file:{path.resolve()}?mode=ro&immutable=1", uri=True)
        db.execute("PRAGMA query_only=ON")
    else:
        db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def load_dry_run(dry_run_dir: Path) -> tuple[list[dict], list[dict], dict]:
    report = json.loads((dry_run_dir / "report.json").read_text(encoding="utf-8"))
    with (dry_run_dir / "treatment_diff.csv").open(encoding="utf-8-sig") as stream:
        diffs = list(csv.DictReader(stream))
    with (dry_run_dir / "clinic_summary.csv").open(encoding="utf-8-sig") as stream:
        summaries = list(csv.DictReader(stream))
    drops = [row for row in diffs if row["guard_result"] == "DROP_AMBIGUOUS_CROSS_DEPARTMENT_ALIAS"]
    return drops, summaries, report


def verify_frozen_drop_set(drops: list[dict], summaries: list[dict]) -> None:
    categories = Counter(row["treatment_category"] for row in drops)
    confirmed = [row for row in drops if row["existing_status"] == "CONFIRMED"]
    if len(drops) != EXPECTED["drop_rows"]:
        raise ValueError(f"DROP row mismatch: {len(drops)} != {EXPECTED['drop_rows']}")
    if len({row["clinic_id"] for row in drops}) != EXPECTED["drop_clinics"]:
        raise ValueError("DROP clinic mismatch")
    if dict(categories) != EXPECTED["drop_categories"]:
        raise ValueError(f"DROP category mismatch: {dict(categories)}")
    if len(confirmed) != 6:
        raise ValueError(f"CONFIRMED DROP mismatch: {len(confirmed)} != 6")
    after = Counter(row["after_status_group"] for row in summaries)
    after["合計"] = len(summaries)
    if dict(after) != EXPECTED["after"]:
        raise ValueError(f"After summary mismatch: {dict(after)}")


def _table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}


def apply_drop_transaction(db: sqlite3.Connection, drops: list[dict]) -> dict:
    affected = sorted({int(row["clinic_id"]) for row in drops})
    keys = [(int(row["clinic_id"]), row["treatment_category"]) for row in drops]
    before_rows = db.execute("SELECT COUNT(*) FROM clinic_treatment_research_final").fetchone()[0]
    db.execute("BEGIN IMMEDIATE")
    try:
        existing = 0
        for clinic_id, category in keys:
            existing += db.execute(
                "SELECT COUNT(*) FROM clinic_treatment_research_final "
                "WHERE clinic_id=? AND treatment_category_name=?", (clinic_id, category)
            ).fetchone()[0]
        if existing != EXPECTED["drop_rows"]:
            raise ValueError(f"sidecar contains {existing} of {EXPECTED['drop_rows']} frozen DROP rows")
        db.executemany(
            "DELETE FROM clinic_treatment_research_final WHERE clinic_id=? AND treatment_category_name=?", keys
        )
        if "candidate_count" in _table_columns(db, "clinic_research_status"):
            db.executemany(
                "UPDATE clinic_research_status SET candidate_count=("
                "SELECT COUNT(*) FROM clinic_treatment_research_final t WHERE t.clinic_id=clinic_research_status.clinic_id"
                ") WHERE clinic_id=?", [(clinic_id,) for clinic_id in affected]
            )
        after_rows = db.execute("SELECT COUNT(*) FROM clinic_treatment_research_final").fetchone()[0]
        if before_rows - after_rows != EXPECTED["drop_rows"]:
            raise ValueError("row-count delta mismatch")
        remaining = sum(db.execute(
            "SELECT COUNT(*) FROM clinic_treatment_research_final WHERE clinic_id=? AND treatment_category_name=?",
            key,
        ).fetchone()[0] for key in keys)
        if remaining:
            raise ValueError(f"{remaining} DROP rows remain")
        inconsistent = db.execute(
            "SELECT COUNT(*) FROM clinic_research_status s WHERE s.clinic_id IN ("
            + ",".join("?" for _ in affected) + ") AND s.candidate_count<>("
            "SELECT COUNT(*) FROM clinic_treatment_research_final t WHERE t.clinic_id=s.clinic_id)", affected
        ).fetchone()[0]
        if inconsistent:
            raise ValueError(f"candidate_count inconsistent for {inconsistent} affected clinics")
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"integrity_check: {integrity}")
        status_counts = dict(db.execute(
            "SELECT research_status,COUNT(*) FROM clinic_treatment_research_final GROUP BY research_status"
        ))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return {
        "before_rows": before_rows, "after_rows": after_rows,
        "deleted_rows": before_rows - after_rows, "affected_clinics": len(affected),
        "candidate_count_inconsistent": 0, "integrity_check": "ok",
        "treatment_status_counts": status_counts,
    }


def verify_after_population(db: sqlite3.Connection, summaries: list[dict]) -> dict:
    target_ids = [int(row["clinic_id"]) for row in summaries]
    groups = Counter()
    for clinic_id in target_ids:
        statuses = {row[0] for row in db.execute(
            "SELECT research_status FROM clinic_treatment_research_final WHERE clinic_id=?", (clinic_id,)
        )}
        if "CONFIRMED" in statuses:
            groups["CONFIRMEDあり"] += 1
        elif "REVIEW" in statuses:
            groups["REVIEWのみ"] += 1
        elif "NOT_CONFIRMED" in statuses:
            groups["NOT_CONFIRMEDのみ"] += 1
        else:
            groups["新Guard上、対象Treatment候補なし"] += 1
    groups["合計"] = len(target_ids)
    result = dict(groups)
    if result != EXPECTED["after"]:
        raise ValueError(f"post-migration population mismatch: {result}")
    return result


def load_cache_text(cache_root: Path, target_ids: set[int]) -> dict[int, str]:
    output: dict[int, list[str]] = defaultdict(list)
    for path in sorted(cache_root.rglob("cache.sqlite3")):
        with connect(path, readonly=True) as db:
            for clinic_id, text in db.execute("SELECT clinic_id,sanitized_text FROM page_cache"):
                if int(clinic_id) in target_ids and text:
                    output[int(clinic_id)].append(text)
    return {clinic_id: "\n".join(texts) for clinic_id, texts in output.items()}


def final_comdesk_population(db: sqlite3.Connection, summaries: list[dict], cache_root: Path) -> dict:
    target_ids = {int(row["clinic_id"]) for row in summaries}
    cache_text = load_cache_text(cache_root, target_ids)
    taxonomy = phase7b_research_categories()
    rows_by_clinic: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for start in range(0, len(target_ids), 900):
        batch = sorted(target_ids)[start:start + 900]
        marks = ",".join("?" for _ in batch)
        for row in db.execute(
            "SELECT clinic_id,treatment_category_name,research_status,matched_alias "
            f"FROM clinic_treatment_research_final WHERE clinic_id IN ({marks})", batch
        ):
            rows_by_clinic[int(row["clinic_id"])].append(row)

    groups = Counter()
    for clinic_id in target_ids:
        rows = rows_by_clinic.get(clinic_id, [])
        statuses = {row["research_status"] for row in rows}
        if "CONFIRMED" in statuses:
            groups["CONFIRMED"] += 1
            continue
        if "REVIEW" in statuses:
            groups["REVIEW"] += 1
            continue
        if not rows:
            groups["NO_TREATMENT_CANDIDATE"] += 1
            continue
        mentioned = False
        text = cache_text.get(clinic_id, "")
        for row in rows:
            if row["research_status"] != "NOT_CONFIRMED":
                continue
            if (row["matched_alias"] or "").strip():
                mentioned = True
                break
            definition = taxonomy.get(row["treatment_category_name"], {})
            terms = [row["treatment_category_name"], *definition.get("aliases", ())]
            if any(keyword_match(term, text) for term in terms):
                mentioned = True
                break
        groups["MENTIONED_NOT_CONFIRMED" if mentioned else "CANDIDATE_ONLY"] += 1
    result = {key: groups[key] for key in (
        "CONFIRMED", "REVIEW", "MENTIONED_NOT_CONFIRMED", "CANDIDATE_ONLY", "NO_TREATMENT_CANDIDATE"
    )}
    result["CONFIRMED_REVIEW_MENTIONED_UNIQUE"] = (
        result["CONFIRMED"] + result["REVIEW"] + result["MENTIONED_NOT_CONFIRMED"]
    )
    result["TOTAL"] = sum(result[key] for key in (
        "CONFIRMED", "REVIEW", "MENTIONED_NOT_CONFIRMED", "CANDIDATE_ONLY", "NO_TREATMENT_CANDIDATE"
    ))
    return result


def run(args: argparse.Namespace) -> dict:
    production = Path(args.production_sidecar).expanduser().resolve()
    staging = Path(args.staging_copy).expanduser().resolve()
    dry_run_dir = Path(args.dry_run_dir).resolve()
    drops, summaries, dry_report = load_dry_run(dry_run_dir)
    verify_frozen_drop_set(drops, summaries)
    production_before_sha = sha256(production)
    report_sha = next(
        item["sha256"] for item in dry_report["input_fingerprints"]
        if Path(item["path"]).resolve() == production
    )
    if production_before_sha != report_sha:
        raise ValueError("Production sidecar SHA differs from the validated dry-run input")

    staging.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(production, staging)
    with connect(staging) as db:
        copy_migration = apply_drop_transaction(db, drops)
        copy_after = verify_after_population(db, summaries)
        comdesk = final_comdesk_population(db, summaries, Path(args.cache_root).expanduser())
    result = {
        "production_before_sha256": production_before_sha,
        "staging_copy": str(staging), "copy_migration": copy_migration,
        "after_population": copy_after, "comdesk_population": comdesk,
        "http_request_count": 0, "production_applied": False,
    }

    if args.apply_production:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = production.with_name(production.name + f".pre_guard_{timestamp}.bak")
        shutil.copy2(production, backup)
        if sha256(backup) != production_before_sha:
            raise ValueError("backup SHA mismatch")
        with connect(production) as db:
            production_migration = apply_drop_transaction(db, drops)
            production_after = verify_after_population(db, summaries)
        result.update({
            "production_applied": True, "production_backup": str(backup),
            "production_backup_sha256": sha256(backup),
            "production_after_sha256": sha256(production),
            "production_migration": production_migration,
            "production_after_population": production_after,
        })
    output = Path(args.output_report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-sidecar", required=True)
    parser.add_argument("--staging-copy", required=True)
    parser.add_argument("--dry-run-dir", required=True)
    parser.add_argument("--cache-root", required=True)
    parser.add_argument("--output-report", required=True)
    parser.add_argument("--apply-production", action="store_true")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run(parse_args()) is None)
