#!/usr/bin/env python3
"""Build a deterministic, non-overlapping Email Canary cohort from Clinic Master (READ ONLY)."""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.normalizer.address import extract_municipality


def artifact_exclusions(root: Path) -> tuple[set[int], list[str]]:
    excluded, files = set(), []
    if not root.exists():
        return excluded, files
    for path in sorted(root.rglob("*.csv")):
        relative = str(path.relative_to(root)).casefold()
        if not any(token in relative for token in ("clinic_email", "email_canary", "performance_canary")):
            continue
        try:
            with path.open(newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames or not ({"clinic_id", "id"} & set(reader.fieldnames)):
                    continue
                found = False
                for row in reader:
                    raw_id = str(row.get("clinic_id") or row.get("id") or "").strip()
                    if raw_id.isdecimal():
                        excluded.add(int(raw_id))
                        found = True
                if found:
                    files.append(str(path))
        except (OSError, UnicodeError, csv.Error):
            # An unreadable historical artifact must not silently be treated as
            # an empty exclusion list.
            raise RuntimeError(f"past Email artifact could not be read: {path}")
    return excluded, files


def _real_uuid(value: object) -> str:
    raw = str(value or "").strip()
    try:
        return str(uuid.UUID(raw))
    except (ValueError, AttributeError, TypeError):
        return ""


def _read_only_db_clinics(credential_root: Path) -> tuple[list[dict], dict, set[int]]:
    from scripts.launch_v2 import load_runtime_env
    load_runtime_env(credential_root)
    from src.repository.email_database import build_email_repositories
    from src.repository.email_target_reader import connect_email_target_reader
    bundle, status = build_email_repositories()
    if bundle is None or bundle.email_research is None:
        raise RuntimeError(status.message)
    repo = bundle.email_research
    if not repo.available():
        raise RuntimeError("Email sidecar schema is unavailable; refusing to infer prior research state.")
    past_ids = repo.researched_clinic_ids()
    reader = connect_email_target_reader()
    try:
        targets = reader.eligible_targets()
    finally:
        reader._conn.close()
    clinics = [{**row, "uuid": row["clinic_uuid"], "hp_url": row["official_hp_url"],
                "hp_status": "VERIFIED", "maps_website_url": ""} for row in targets]
    return clinics, {"clinic_source": "dedicated_read_only_target_reader",
                     "sidecar_exclusion_count": len(past_ids),
                     "transaction_read_only": True, "production_write": False}, past_ids


def make_cohort(clinics: list[dict], *, excluded_ids: set[int], limit: int, seed: int) -> tuple[list[dict], dict]:
    eligible = [row for row in clinics if int(row["clinic_id"]) not in excluded_ids]
    # Defend against legacy duplicate UUIDs before sampling; a duplicate is not
    # silently counted as a second independent clinic.
    by_uuid, no_uuid = {}, []
    for row in eligible:
        normalized_uuid = _real_uuid(row.get("uuid"))
        if normalized_uuid:
            by_uuid.setdefault(normalized_uuid, []).append(row)
        else:
            no_uuid.append(row)
    unique_real = [sorted(items, key=lambda item: int(item["clinic_id"]))[0]
                   for items in by_uuid.values()]
    duplicate_uuid_rows = sum(len(items) - 1 for items in by_uuid.values())
    rng = random.Random(seed)
    rng.shuffle(unique_real)
    rng.shuffle(no_uuid)
    ordered = unique_real + no_uuid
    if len(ordered) < limit:
        raise RuntimeError(f"only {len(ordered)} eligible non-overlapping clinics; requested {limit}")
    selected = ordered[:limit]
    output = []
    for row in selected:
        uid = _real_uuid(row.get("uuid"))
        raw_uuid = str(row.get("uuid") or "").strip()
        hp_url = str(row.get("hp_url") or "").strip()
        hp_url_source = "CLINIC_MASTER_HP_URL" if hp_url else ""
        # Maps website URLs are not implicitly trusted. Reuse them as an official
        # research input only when Clinic Master already marks HP identity VERIFIED.
        if not hp_url and str(row.get("hp_status") or "").upper() == "VERIFIED":
            hp_url = str(row.get("maps_website_url") or "").strip()
            if hp_url:
                hp_url_source = "VERIFIED_MAPS_WEBSITE_URL"
        output.append({
            "clinic_id": int(row["clinic_id"]),
            "clinic_uuid": uid or f"legacy-id:{row['clinic_id']}",
            "clinic_uuid_kind": "REAL_UUID" if uid else "LEGACY_ID" if raw_uuid else "MISSING_UUID",
            "clinic_name": row.get("clinic_name") or "",
            "prefecture": row.get("prefecture") or "",
            "city": row.get("city") or extract_municipality(row.get("address") or ""),
            "address": row.get("address") or "",
            "phone": row.get("phone") or "",
            "hp_url": hp_url, "hp_url_source": hp_url_source,
        })
    real_values = [row["clinic_uuid"] for row in output if row["clinic_uuid_kind"] == "REAL_UUID"]
    if len(real_values) != len(set(real_values)):
        raise AssertionError("cohort contains duplicate real UUIDs")
    return output, {"eligible_after_exclusion": len(ordered), "duplicate_uuid_rows_excluded": duplicate_uuid_rows,
                    "real_uuid_count": len(real_values), "legacy_id_count": len(output) - len(real_values),
                    "duplicate_uuid_count_in_cohort": len(real_values) - len(set(real_values)),
                    "seed": seed}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-root", type=Path, required=True,
                        help="Clinic Lead System root containing private runtime credentials")
    parser.add_argument("--artifact-root", type=Path, required=True,
                        help="Read-only historical Email artifacts used for overlap exclusion")
    parser.add_argument("--output", type=Path, required=True, help="New output directory; never overwritten")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20261009)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise SystemExit(f"output exists; refusing to overwrite: {args.output}")
    clinic_rows, db_summary, db_past_ids = _read_only_db_clinics(args.credential_root)
    artifact_ids, artifact_files = artifact_exclusions(args.artifact_root)
    # Historical artifacts and formal research state are read from local files
    # and the dedicated Email sidecar respectively; Production Email tables are
    # deliberately not consulted.
    exclusions = artifact_ids | db_past_ids
    cohort, cohort_summary = make_cohort(clinic_rows, excluded_ids=exclusions,
                                         limit=args.limit, seed=args.seed)
    args.output.mkdir(parents=True, exist_ok=False)
    with (args.output / "cohort.csv").open("x", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(cohort[0]))
        writer.writeheader()
        writer.writerows(cohort)
    summary = {"mode": "READ_ONLY_COHORT_EXPORT", "clinic_total": len(cohort),
               "artifact_excluded_clinic_ids": len(artifact_ids), "artifact_exclusion_files": artifact_files,
               **db_summary, **cohort_summary, "production_db_write": False}
    with (args.output / "cohort_summary.json").open("x", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
