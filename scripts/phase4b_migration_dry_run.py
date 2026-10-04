"""Phase 4-B -> Treatment sidecar Production migration dry-run.

Builds the migration candidate set (CONFIRMED minus 2 known taxonomy false
positives, plus 24 human-verified REVIEW rows minus 1 human-rejected row),
the excluded-rows file, and the MENTIONED hold file. Runs a read-only
preflight against the Treatment sidecar and classifies every candidate as
WOULD_INSERT / ALREADY_EXISTS / CONFLICT / INVALID.

Does not write to the Treatment sidecar, Production DB, MHLW DB, Comdesk, or
the Phase-4B cache, and never re-crawls. Only artifacts/hybrid_phase4b/migration/
is written.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "hybrid_phase4b"
MIG = OUT / "migration"
SIDECAR = Path("/Users/maekawahiroyuki/CrestixData/clinic-lead/treatment_research_final.sqlite3")

VALID_RESEARCH_STATUS = {"CONFIRMED", "REVIEW", "NOT_CONFIRMED"}

# Known false-positive signatures confirmed by root-cause analysis (see
# taxonomy_correction_recommendation.md). Excluded from the CONFIRMED tier.
CONFIRMED_EXCLUSIONS = {
    ("5861", "歯列矯正"): "ORTHODONTIC_GENERIC_ALIAS_FALSE_POSITIVE",
    ("13029", "歯列矯正"): "ORTHODONTIC_GENERIC_ALIAS_FALSE_POSITIVE",
}
# Human-rejected REVIEW row.
REVIEW_EXCLUSIONS = {
    ("303", "採卵"): "ADJACENT_PROCEDURE_MENTION_FALSE_POSITIVE",
}


def load_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if any(v.strip() for v in r.values())]


def key(row: dict) -> tuple:
    return (row["clinic_id"], row["treatment_category"])


def main() -> None:
    MIG.mkdir(parents=True, exist_ok=True)
    treatments = load_csv(OUT / "treatment_results.csv")
    review_labeled = load_csv(OUT / "review_all_22_labeled.csv")
    confirmed_sample_labeled = load_csv(OUT / "confirmed_human_review_100_labeled.csv")

    confirmed_all = [r for r in treatments if r["hybrid_status"] == "CONFIRMED"]
    mentioned_all = [r for r in treatments if r["hybrid_status"] == "MENTIONED"]
    review_all = [r for r in treatments if r["hybrid_status"] == "REVIEW"]
    if len(confirmed_all) != 355 or len(mentioned_all) != 180 or len(review_all) != 25:
        raise AssertionError(f"signal pool sizes changed: CONFIRMED={len(confirmed_all)} "
                              f"MENTIONED={len(mentioned_all)} REVIEW={len(review_all)}")

    sampled_correct = {key(r) for r in confirmed_sample_labeled if r["audit_label"] == "CORRECT"}
    review_label = {key(r): r["audit_label"] for r in review_labeled}

    # ---- A. CONFIRMED tier ----
    confirmed_kept, confirmed_excluded = [], []
    for r in confirmed_all:
        k = key(r)
        if k in CONFIRMED_EXCLUSIONS:
            confirmed_excluded.append((r, CONFIRMED_EXCLUSIONS[k]))
        else:
            confirmed_kept.append(r)
    if len(confirmed_kept) != 353 or len(confirmed_excluded) != 2:
        raise AssertionError(f"CONFIRMED tier mismatch: kept={len(confirmed_kept)} excluded={len(confirmed_excluded)}")

    # ---- B. REVIEW tier (human-verified only) ----
    review_kept, review_excluded = [], []
    for r in review_all:
        k = key(r)
        label = review_label.get(k)
        if label == "CORRECT":
            review_kept.append(r)
        elif k in REVIEW_EXCLUSIONS:
            review_excluded.append((r, REVIEW_EXCLUSIONS[k]))
        else:
            raise AssertionError(f"REVIEW row {k} has unexpected label {label!r}; every REVIEW row "
                                  f"must be explicitly accepted (CORRECT) or in REVIEW_EXCLUSIONS")
    if len(review_kept) != 24 or len(review_excluded) != 1:
        raise AssertionError(f"REVIEW tier mismatch: kept={len(review_kept)} excluded={len(review_excluded)}")

    # ---- production_candidates.csv (377 rows) ----
    candidate_cols = ["clinic_id", "clinic_name", "treatment_name", "treatment_category", "matched_alias",
                       "final_status", "original_phase4b_status", "human_verified", "signal_source",
                       "evidence_url", "evidence_text", "hybrid_rule_version", "taxonomy_version",
                       "evidence_engine_version", "migration_reason"]
    candidates = []
    for r in confirmed_kept:
        candidates.append({
            "clinic_id": r["clinic_id"], "clinic_name": r["clinic_name"],
            "treatment_name": r["treatment_category"], "treatment_category": r["treatment_category"],
            "matched_alias": r["matched_alias"], "final_status": "CONFIRMED",
            "original_phase4b_status": "CONFIRMED",
            "human_verified": "true" if key(r) in sampled_correct else "false",
            "signal_source": r["signal_source"], "evidence_url": r["evidence_url"],
            "evidence_text": r["evidence_text"], "hybrid_rule_version": r["hybrid_rule_version"],
            "taxonomy_version": r["taxonomy_version"], "evidence_engine_version": r["evidence_engine_version"],
            "migration_reason": "AUTO_CONFIRMED_HUMAN_AUDITED_SAMPLE_POLICY",
        })
    for r in review_kept:
        candidates.append({
            "clinic_id": r["clinic_id"], "clinic_name": r["clinic_name"],
            "treatment_name": r["treatment_category"], "treatment_category": r["treatment_category"],
            "matched_alias": r["matched_alias"], "final_status": "CONFIRMED",
            "original_phase4b_status": "REVIEW", "human_verified": "true",
            "signal_source": r["signal_source"], "evidence_url": r["evidence_url"],
            "evidence_text": r["evidence_text"], "hybrid_rule_version": r["hybrid_rule_version"],
            "taxonomy_version": r["taxonomy_version"], "evidence_engine_version": r["evidence_engine_version"],
            "migration_reason": "HUMAN_VERIFIED_REVIEW",
        })
    if len(candidates) != 377:
        raise AssertionError(f"expected 377 candidates, got {len(candidates)}")
    dup_keys = [k for k in {key(c) for c in candidates}
                if sum(1 for c in candidates if key(c) == k) > 1]
    if dup_keys:
        raise AssertionError(f"duplicate candidate key(s): {dup_keys}")

    with (MIG / "production_candidates.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=candidate_cols)
        w.writeheader()
        w.writerows(candidates)

    # ---- excluded_rows.csv (3 rows) ----
    excluded_cols = ["clinic_id", "clinic_name", "treatment_category", "matched_alias",
                      "original_phase4b_status", "evidence_url", "evidence_text", "exclusion_reason"]
    excluded = []
    for r, reason in confirmed_excluded + review_excluded:
        excluded.append({
            "clinic_id": r["clinic_id"], "clinic_name": r["clinic_name"],
            "treatment_category": r["treatment_category"], "matched_alias": r["matched_alias"],
            "original_phase4b_status": r["hybrid_status"], "evidence_url": r["evidence_url"],
            "evidence_text": r["evidence_text"], "exclusion_reason": reason,
        })
    if len(excluded) != 3:
        raise AssertionError(f"expected 3 excluded rows, got {len(excluded)}")
    with (MIG / "excluded_rows.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=excluded_cols)
        w.writeheader()
        w.writerows(excluded)

    # ---- mentioned_hold.csv (180 rows, untouched) ----
    with (MIG / "mentioned_hold.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(mentioned_all[0].keys()))
        w.writeheader()
        w.writerows(mentioned_all)

    # ---- Sidecar preflight (read-only) ----
    with sqlite3.connect(f"file:{SIDECAR.resolve()}?mode=ro", uri=True) as sdb:
        sdb.execute("PRAGMA query_only=ON")
        sidecar_rows = sdb.execute(
            "SELECT clinic_id, treatment_category_name, research_status FROM clinic_treatment_research_final"
        ).fetchall()
        sidecar_integrity = sdb.execute("PRAGMA integrity_check").fetchone()[0]
        sidecar_clinic_ids = {row[0] for row in sdb.execute(
            "SELECT DISTINCT clinic_id FROM clinic_treatment_research_final")}
    sidecar_map = {(str(cid), cat): status for cid, cat, status in sidecar_rows}

    population_ids = {int(r["clinic_id"]) for r in load_csv(OUT / "population.csv")}
    overlap = population_ids & sidecar_clinic_ids
    candidate_ids = {int(c["clinic_id"]) for c in candidates}
    candidate_overlap = candidate_ids & sidecar_clinic_ids

    preflight = {
        "candidate_rows": len(candidates),
        "clinic_id_format_invalid": sum(1 for c in candidates if not c["clinic_id"].isdigit()),
        "treatment_category_present": sum(1 for c in candidates if c["treatment_category"].strip()),
        "duplicate_clinic_treatment_within_candidates": len(dup_keys),
        "sidecar_identical_row_exists": sum(1 for c in candidates
                                             if sidecar_map.get(key({"clinic_id": c["clinic_id"],
                                                                       "treatment_category": c["treatment_category"]}))
                                             == c["final_status"]),
        "sidecar_conflict_exists": sum(1 for c in candidates
                                        if key({"clinic_id": c["clinic_id"], "treatment_category": c["treatment_category"]}) in sidecar_map
                                        and sidecar_map[key({"clinic_id": c["clinic_id"], "treatment_category": c["treatment_category"]})] != c["final_status"]),
        "phase4b_population_vs_sidecar_clinic_overlap": len(overlap),
        "migration_candidates_vs_sidecar_clinic_overlap": len(candidate_overlap),
        "sidecar_integrity_check": sidecar_integrity,
    }

    # ---- Migration preview classification ----
    classification = {"WOULD_INSERT": 0, "ALREADY_EXISTS": 0, "CONFLICT": 0, "INVALID": 0}
    classified_rows = []
    for c in candidates:
        k = (c["clinic_id"], c["treatment_category"])
        if c["final_status"] not in VALID_RESEARCH_STATUS or not c["clinic_id"].isdigit() or not c["treatment_category"].strip():
            verdict = "INVALID"
        elif k not in sidecar_map:
            verdict = "WOULD_INSERT"
        elif sidecar_map[k] == c["final_status"]:
            verdict = "ALREADY_EXISTS"
        else:
            verdict = "CONFLICT"
        classification[verdict] += 1
        classified_rows.append({**c, "preview_verdict": verdict})

    with (MIG / "migration_preview_classified.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=candidate_cols + ["preview_verdict"])
        w.writeheader()
        w.writerows(classified_rows)

    report = {
        "candidates_total": len(candidates),
        "confirmed_tier_rows": len(confirmed_kept),
        "review_tier_rows": len(review_kept),
        "excluded_rows": len(excluded),
        "mentioned_hold_rows": len(mentioned_all),
        "preflight": preflight,
        "migration_preview": classification,
    }
    (MIG / "sidecar_preflight_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
