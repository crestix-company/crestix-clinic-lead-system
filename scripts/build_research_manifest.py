"""Phase 7 Full Run manifest builder (Step 6).

Population (per explicit scope decision, not the original department-gated 4,140):

    legacy scope (first_seen_at < LEGACY_PRE_NATIONAL_CUTOFF, src/master/scope.py)
    AND effective official HP URL (choose_identity_url(), unchanged from phase7b_pilot.py)
    AND NOT already researched (Pilot 270 ∪ Canary v4 500, read from the sibling
        worktree's mhlw_dry_run/*.sqlite3 artifacts, read-only)

Department / Navi / Crestix sales category is deliberately NOT a gating condition --
see src/enrichment/candidate_map.py's fallback design (candidate_union always unions
in the global alias rescue scan, so a clinic with no department signal still gets
researched, not silently dropped).

Writes a versioned CSV manifest (one row per clinic) that scripts/research_worker.py
treats as the fixed, resumable Full Run population. Production DB is opened
read-only; nothing is written back to it or to the sibling worktree.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess

from scripts.phase7b_pilot import choose_identity_url
from src.enrichment.treatment_taxonomy import EVIDENCE_ENGINE_VERSION, RULE_VERSION
from src.master.scope import LEGACY_PRE_NATIONAL_CUTOFF
from src.utils.config import ROOT

CLINIC_DB = ROOT / "data/clinics.sqlite3"
OTHER_MHLW_DIR = Path.home() / "Desktop/clinic-list-filter-complete/mhlw_dry_run"
PILOT_DB = OTHER_MHLW_DIR / "phase7b_treatment_research.sqlite3"
CANARY_DB = OTHER_MHLW_DIR / "phase7c_canary_500_v4_canary.sqlite3"
MANIFEST_DIR = ROOT / "data/manifests"
MANIFEST_FIELDS = [
    "manifest_id", "clinic_id", "clinic_name", "phone", "address",
    "effective_official_hp_url", "url_source",
    "crestix_sales_category_hint", "git_commit_sha", "taxonomy_version", "evidence_engine_version",
    "generated_at",
]


def _git_commit_sha() -> str:
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _already_researched_clinic_ids() -> set[int]:
    if not PILOT_DB.exists() or not CANARY_DB.exists():
        raise FileNotFoundError(
            f"Pilot/Canary result artifacts not found (read-only reference): {PILOT_DB}, {CANARY_DB}"
        )
    pilot = sqlite3.connect(f"file:{PILOT_DB}?mode=ro", uri=True)
    pilot_ids = {row[0] for row in pilot.execute("SELECT DISTINCT clinic_id FROM clinic_treatment_research")}
    canary = sqlite3.connect(f"file:{CANARY_DB}?mode=ro", uri=True)
    canary_ids = {row[0] for row in canary.execute("SELECT DISTINCT clinic_id FROM canary_treatment_research")}
    overlap = pilot_ids & canary_ids
    if overlap:
        raise ValueError(f"Pilot/Canary clinic_id overlap must be 0, found {len(overlap)}")
    return pilot_ids | canary_ids


def build_manifest_rows() -> tuple[list[dict], dict]:
    if not CLINIC_DB.exists():
        raise FileNotFoundError(f"Production DB not available read-only at {CLINIC_DB}")
    db = sqlite3.connect(f"file:{CLINIC_DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise ValueError(f"Production DB integrity check failed: {integrity}")

    legacy_total = db.execute(
        "SELECT COUNT(*) FROM clinics WHERE first_seen_at < ? AND merged_into IS NULL", (LEGACY_PRE_NATIONAL_CUTOFF,)
    ).fetchone()[0]
    if legacy_total != 13970:
        raise ValueError(f"legacy scope count mismatch: expected 13970, got {legacy_total}")

    old_results = {int(row["clinic_id"]): json.loads(row["result_json"] or "{}")
                   for row in db.execute("SELECT clinic_id, result_json FROM research_results")}
    researched_ids = _already_researched_clinic_ids()

    git_sha = _git_commit_sha()
    generated_at = datetime.now(timezone.utc).isoformat()
    manifest_id = "phase7-fullrun-" + hashlib.sha256(
        f"{git_sha}|{RULE_VERSION}|{EVIDENCE_ENGINE_VERSION}|{generated_at}".encode()
    ).hexdigest()[:16]

    rows = []
    effective_hp_total = 0
    for raw in db.execute(
        "SELECT id,clinic_name,phone,address,hp_status,hp_url,maps_presence_status,maps_website_url,"
        "departments_json,treatments_json,effective_json,base_json,first_seen_at "
        "FROM clinics WHERE merged_into IS NULL AND first_seen_at < ? ORDER BY id",
        (LEGACY_PRE_NATIONAL_CUTOFF,),
    ):
        record = dict(raw)
        record["clinic_id"] = int(record.pop("id"))
        old = old_results.get(record["clinic_id"], {})
        url, source = choose_identity_url(record, old)
        if not url:
            continue
        effective_hp_total += 1
        if record["clinic_id"] in researched_ids:
            continue
        try:
            departments = json.loads(record.get("departments_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            departments = []
        rows.append({
            "manifest_id": manifest_id,
            "clinic_id": record["clinic_id"],
            "clinic_name": record.get("clinic_name", ""),
            "phone": record.get("phone", ""),
            "address": record.get("address", ""),
            "effective_official_hp_url": url,
            "url_source": source,
            "crestix_sales_category_hint": ";".join(departments),
            "git_commit_sha": git_sha,
            "taxonomy_version": RULE_VERSION,
            "evidence_engine_version": EVIDENCE_ENGINE_VERSION,
            "generated_at": generated_at,
        })

    meta = {
        "manifest_id": manifest_id,
        "git_commit_sha": git_sha,
        "taxonomy_version": RULE_VERSION,
        "evidence_engine_version": EVIDENCE_ENGINE_VERSION,
        "generated_at": generated_at,
        "legacy_total": legacy_total,
        "legacy_effective_hp_total": effective_hp_total,
        "already_researched": len(researched_ids),
        "manifest_clinic_count": len(rows),
    }
    return rows, meta


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None, help="override output CSV path")
    args = parser.parse_args()

    rows, meta = build_manifest_rows()
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else MANIFEST_DIR / f"{meta['manifest_id']}.csv"
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    meta_path = out_path.with_suffix(".meta.json")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(meta, ensure_ascii=False, indent=2))
    print(f"\nWrote {out_path}\nWrote {meta_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
