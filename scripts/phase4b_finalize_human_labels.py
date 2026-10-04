"""One-time transcription of the completed Phase-4B human review into official
artifacts.

Reads the human-reviewed working copies (exported from Numbers, containing the
machine suggested_label/review_priority/suggested_reason columns alongside the
human audit_label/audit_comment columns), verifies row-level integrity against
the original artifact, and writes
artifacts/hybrid_phase4b/{confirmed_human_review_100,review_all_22}_labeled.csv.

Only the human-entered audit_label/audit_comment are transcribed. suggested_label
is never copied into audit_label; it is carried along only as a read-only
reference column for the taxonomy error-analysis step. Does not touch
Production DB, Treatment sidecar, MHLW DB, Comdesk, or the Phase-4B cache.
"""
from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "hybrid_phase4b"

SOURCE_CONFIRMED = Path(
    "/Users/maekawahiroyuki/Downloads/"
    "confirmed_human_review_100_with_suggestions - confirmed_human_review_100_with_suggestions (2).csv"
)
SOURCE_REVIEW = Path(
    "/Users/maekawahiroyuki/Downloads/"
    "review_all_22_with_suggestions - review_all_22_with_suggestions (1).csv"
)
ORIG_CONFIRMED = OUT / "confirmed_human_review_100.csv"
ORIG_REVIEW = OUT / "review_all_22.csv"

ALLOWED = {"CORRECT", "INCORRECT", "UNCERTAIN"}


def load(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [row for row in csv.DictReader(f) if any(v.strip() for v in row.values())]


def key(row: dict) -> tuple:
    return (row["clinic_id"], row["treatment_category"])


def guard(source: list[dict], orig: list[dict], tamper_fields: list[str], expect_n: int, label: str) -> None:
    if len(source) != expect_n:
        raise AssertionError(f"{label}: expected {expect_n} rows, found {len(source)}")
    ks, ko = {key(r) for r in source}, {key(r) for r in orig}
    if ks != ko:
        raise AssertionError(f"{label}: row key set mismatch vs original artifact "
                              f"(orig-only={ko-ks}, source-only={ks-ko})")
    om = {key(r): tuple(r[f] for f in tamper_fields) for r in orig}
    bad = [r for r in source if om[key(r)] != tuple(r[f] for f in tamper_fields)]
    if bad:
        raise AssertionError(f"{label}: {len(bad)} row(s) have changed evidence/identity fields")
    blanks = [r for r in source if not r["audit_label"].strip()]
    if blanks:
        raise AssertionError(f"{label}: {len(blanks)} row(s) still have a blank audit_label")
    bad_labels = sorted({r["audit_label"].strip() for r in source
                          if r["audit_label"].strip().upper() not in ALLOWED})
    if bad_labels:
        raise AssertionError(f"{label}: non-allowed audit_label value(s) {bad_labels!r}")


def finalize(source_rows: list[dict], output_cols: list[str]) -> list[dict]:
    out = []
    for r in source_rows:
        row = {c: r.get(c, "") for c in output_cols}
        raw_label = r["audit_label"].strip()
        normalized = raw_label.upper()
        if normalized != raw_label:
            print(f"  [normalize] clinic_id={r['clinic_id']} treatment={r['treatment_category']}: "
                  f"audit_label {raw_label!r} -> {normalized!r} (case only)")
        row["audit_label"] = normalized
        row["audit_comment"] = r.get("audit_comment", "").strip()
        row["suggested_label_reference_only"] = r.get("suggested_label", "")
        row["review_priority_reference_only"] = r.get("review_priority", "")
        row["suggested_reason_reference_only"] = r.get("suggested_reason", "")
        out.append(row)
    return out


def main() -> None:
    source_c = load(SOURCE_CONFIRMED)
    orig_c = load(ORIG_CONFIRMED)
    guard(source_c, orig_c,
          ["clinic_id", "clinic_name", "treatment_category", "matched_alias", "signal_status",
           "signal_source", "evidence_url", "evidence_text", "initial_url"],
          100, "CONFIRMED sample")

    source_r = load(SOURCE_REVIEW)
    orig_r = load(ORIG_REVIEW)
    guard(source_r, orig_r,
          ["clinic_id", "clinic_name", "treatment_category", "matched_alias", "hybrid_status",
           "signal_source", "evidence_url", "evidence_text"],
          25, "REVIEW all")

    print("CONFIRMED guard checks passed (100 rows, keys/evidence unchanged, labels all valid):")
    confirmed_cols = ["clinic_id", "clinic_name", "treatment_name", "treatment_category", "matched_alias",
                       "signal_status", "signal_source", "evidence_url", "evidence_text", "initial_url",
                       "audit_label", "audit_comment"]
    confirmed_out = finalize(source_c, confirmed_cols)
    with (OUT / "confirmed_human_review_100_labeled.csv").open("w", encoding="utf-8-sig", newline="") as f:
        cols = confirmed_cols + ["suggested_label_reference_only", "review_priority_reference_only",
                                  "suggested_reason_reference_only"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(confirmed_out)

    print("\nREVIEW guard checks passed (25 rows, keys/evidence unchanged, labels all valid):")
    review_cols = ["clinic_id", "treatment_category", "hybrid_status", "signal_source", "signal_rank",
                   "matched_alias", "evidence_text", "evidence_url", "page_title", "provider_context",
                   "exclusion_context", "department_context", "confidence", "hybrid_rule_version",
                   "taxonomy_version", "evidence_engine_version", "checked_at", "clinic_name",
                   "sampling_group", "formal_departments", "page_count", "fetch_status",
                   "audit_label", "audit_comment"]
    review_out = finalize(source_r, review_cols)
    with (OUT / "review_all_22_labeled.csv").open("w", encoding="utf-8-sig", newline="") as f:
        cols = review_cols + ["suggested_label_reference_only", "review_priority_reference_only",
                               "suggested_reason_reference_only"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(review_out)

    # Prove no auto-substitution happened: every written label equals the
    # source file's own audit_label (case-normalized only), never suggested_label.
    assert all(c["audit_label"] == s["audit_label"].strip().upper() for c, s in zip(confirmed_out, source_c))
    assert all(r["audit_label"] == s["audit_label"].strip().upper() for r, s in zip(review_out, source_r))
    print("\nWrote confirmed_human_review_100_labeled.csv and review_all_22_labeled.csv")
    print("No suggested_label was ever written into audit_label (verified by direct equality check).")


if __name__ == "__main__":
    main()
