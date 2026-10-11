#!/usr/bin/env python3
"""Offline AUTO_TRUE/AUTO_FALSE/REVIEW replay over labeled Email audit CSVs."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.master.email_sales_eligibility import classify_email_candidate


DEFAULT_FILES = (
    "clinic_email_fresh_canary_v3/prefetch_human_audit.csv",
    "clinic_email_fresh_canary_v4/prefetch_human_audit.csv",
    "clinic_email_fresh_canary_v5/verified_review.csv",
    "clinic_email_fresh_canary_v5/trusted_public_review.csv",
    "clinic_email_fresh_canary_v5/review_review.csv",
    "clinic_email_fresh_canary_v5/recruitment_review.csv",
    "clinic_email_fresh_canary_v5/prefetch_review.csv",
    "clinic_email_fresh_canary_v5/third_party_review.csv",
    "clinic_email_fresh_canary_v6/final_analysis/verified_and_primary_audit.csv",
    "clinic_email_fresh_canary_v6/final_analysis/prefetch_reject_audit.csv",
)
HUMAN_TRUE = {"TRUE_POSITIVE", "CORRECT_PASS", "SHOULD_FETCH", "FALSE_REJECT"}
HUMAN_FALSE = {"WRONG_CLINIC", "CORRECT_REJECT"}
DISPOSITIONS = ("AUTO_TRUE", "AUTO_FALSE", "REVIEW")
GROUP_FIELDS = {
    "Role": "role", "Ownership": "ownership", "Source": "discovery_source",
    "Confidence": "confidence", "Verification Status": "verification_status",
}


def _bool(value):
    text = str(value or "").strip().casefold()
    if text in {"true", "yes", "1"}: return True
    if text in {"false", "no", "0"}: return False
    return None


def _human_label(row):
    verdict = str(row.get("human_verdict") or row.get("human_label") or "").strip().upper()
    if verdict in HUMAN_TRUE: return "TRUE", verdict
    if verdict in HUMAN_FALSE: return "FALSE", verdict
    if verdict in {"CORRECT_REVIEW", "UNCERTAIN", "AMBIGUOUS"}: return "UNCERTAIN", verdict
    return "", verdict


def _normalize(row, source_file):
    clinic_id = str(row.get("clinic_id") or row.get("id") or "").strip()
    email = str(row.get("email") or row.get("primary_email") or "").strip()
    if not clinic_id or not email or "@" not in email:
        return None
    raw_uuid = str(row.get("clinic_uuid") or row.get("uuid") or "").strip()
    is_real_uuid = bool(raw_uuid and not raw_uuid.startswith("legacy-id:"))
    clinic_uuid = raw_uuid if raw_uuid else f"legacy-id:{clinic_id}"
    normalized_email = email.casefold()
    status = str(row.get("verification_status") or row.get("status") or "").strip().upper()
    if not status and str(row.get("verifier_raw_status") or "").upper().startswith("REJECTED"):
        status = "REJECTED"
    if not status and str(row.get("pre_fetch_decision") or "").upper() == "REJECTED":
        status = "REJECTED"
    match_fields = {
        "clinic_name_match": _bool(row.get("clinic_name_match")),
        "prefecture_match": _bool(row.get("prefecture_match")),
        "city_match": _bool(row.get("city_match")),
    }
    # Historical engine rows omit individual match columns. For completed VERIFIED_EMAIL
    # evidence only, mark the engine's accepted identity/location gate as inferred, and label it.
    inferred_identity = False
    verified_official = _bool(row.get("verified_on_official"))
    source = str(row.get("discovery_source") or row.get("source_type") or "").strip().upper()
    source_host = (urlsplit(str(row.get("source_url") or "")).hostname or "").lower()
    if source_host == "www.iryou.teikyouseido.mhlw.go.jp" or source_host.endswith(".iryou.teikyouseido.mhlw.go.jp"):
        source = "NAVII"
    elif source in {"", "UNKNOWN"}:
        source = "SEARCH_QUERY"
    if status == "VERIFIED_EMAIL" and verified_official and any(value is None for value in match_fields.values()):
        match_fields = {key: True for key in match_fields}
        inferred_identity = True
    role = str(row.get("role") or row.get("email_role") or "UNKNOWN").strip().upper()
    ownership = str(row.get("ownership") or "OWNERSHIP_UNKNOWN").strip().upper()
    rejection_reason = " ".join(str(row.get(k) or "") for k in (
        "rejection_reason", "rationale", "auto_reason", "prefetch_reason",
    ))
    payload = {
        "email": email, "verification_status": status, "role": role,
        "ownership": ownership, "discovery_source": source,
        "source_type": source, "source_url": row.get("source_url") or "",
        "rejection_reason": rejection_reason,
        **match_fields,
    }
    classification = classify_email_candidate(payload)
    human, verdict = _human_label(row)
    candidate_id_raw = str(row.get("candidate_id") or "").strip()
    digest = hashlib.sha256(f"{clinic_id}\0{normalized_email}".encode()).hexdigest()[:20]
    return {
        **row,
        "clinic_id": clinic_id,
        "clinic_uuid": clinic_uuid,
        "clinic_uuid_kind": "REAL_UUID" if is_real_uuid else "LEGACY_ID" if raw_uuid.startswith("legacy-id:") else "MISSING",
        "clinic_uuid_missing": not is_real_uuid,
        "candidate_id": candidate_id_raw or f"offline:{clinic_id}:{digest}",
        "candidate_id_kind": "DATABASE" if candidate_id_raw else "OFFLINE_SYNTHETIC",
        "audit_source_file": source_file,
        "normalized_email": normalized_email,
        "verification_status": status,
        "role": classification_role(role),
        "ownership": classification_ownership(ownership),
        "discovery_source": source,
        "identity_inferred_from_verified_status": inferred_identity,
        "auto_decision": classification.auto_decision,
        "auto_identity_decision": classification.auto_identity_decision,
        "confidence": classification.confidence,
        "sales_usable": classification.sales_usable,
        "hard_block_codes": ";".join(classification.hard_block_codes),
        "final_sales_eligible": classification.final_sales_eligible,
        "classification_reason": ";".join(classification.rationale),
        "human_identity_label": human,
        "human_verdict_normalized": verdict,
    }


def classification_role(value):
    from src.master.email_sales_eligibility import normalize_role
    return normalize_role(value)


def classification_ownership(value):
    from src.master.email_sales_eligibility import normalize_ownership
    return normalize_ownership(value)


def read_labeled_rows(audit_root: Path, filenames=DEFAULT_FILES):
    output, source_counts = [], Counter()
    for relative in filenames:
        path = audit_root / relative
        if not path.is_file():
            continue
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                human, _ = _human_label(row)
                if not human:
                    continue
                normalized = _normalize(row, relative)
                if normalized is not None:
                    output.append(normalized)
                    source_counts[relative] += 1
    return output, source_counts


def matrix(rows):
    counts = {decision: {label: 0 for label in ("TRUE", "FALSE", "UNCERTAIN")} for decision in DISPOSITIONS}
    for row in rows:
        if row["human_identity_label"]:
            counts[row["auto_decision"]][row["human_identity_label"]] += 1
    return counts


def latest_unique_candidates(rows):
    grouped_rows = defaultdict(list)
    for row in rows:
        grouped_rows[(row["clinic_id"], row["normalized_email"])].append(row)
    selected, conflicts = [], []
    for key, observations in grouped_rows.items():
        latest_at = max(str(row.get("checked_at") or row.get("reviewed_at") or "") for row in observations)
        latest = [row for row in observations
                  if str(row.get("checked_at") or row.get("reviewed_at") or "") == latest_at]
        labels = {row["human_identity_label"] for row in latest}
        if len(labels) > 1:
            conflicts.append({
                "clinic_id": key[0], "clinic_uuid": latest[-1]["clinic_uuid"],
                "candidate_id": latest[-1]["candidate_id"], "normalized_email": key[1],
                "checked_at": latest_at, "conflicting_human_labels": ";".join(sorted(labels)),
                "audit_source_files": ";".join(sorted({r["audit_source_file"] for r in latest})),
            })
            continue
        chosen = sorted(latest, key=lambda row: row["audit_source_file"])[-1].copy()
        chosen["observation_count"] = len(observations)
        chosen["audit_source_files"] = ";".join(sorted({r["audit_source_file"] for r in observations}))
        selected.append(chosen)
    return selected, conflicts


def grouped(rows):
    result = {}
    for label, field in GROUP_FIELDS.items():
        buckets = defaultdict(Counter)
        for row in rows:
            human = row["human_identity_label"] or "UNCERTAIN"
            buckets[str(row.get(field) or "UNKNOWN")][(row["auto_decision"], human)] += 1
        result[label] = {
            key: {f"{decision} x HUMAN_{human}": count for (decision, human), count in sorted(counts.items())}
            for key, counts in sorted(buckets.items())
        }
    return result


def write_csv(path: Path, rows):
    fields = []
    for row in rows:
        for key in row:
            if key not in fields: fields.append(key)
    with path.open("x", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def replay(audit_root: Path, output: Path, filenames=DEFAULT_FILES):
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing replay artifact: {output}")
    rows, source_counts = read_labeled_rows(audit_root, filenames=filenames)
    if not rows:
        raise ValueError(f"no labeled candidate rows found under {audit_root}")
    unique_rows, label_conflicts = latest_unique_candidates(rows)
    output.mkdir(parents=True, exist_ok=False)
    fp = [r for r in unique_rows if r["auto_decision"] == "AUTO_TRUE" and r["human_identity_label"] == "FALSE"]
    fn = [r for r in unique_rows if r["auto_decision"] == "AUTO_FALSE" and r["human_identity_label"] == "TRUE"]
    hard_block_identity_true = [r for r in fn if r["hard_block_codes"]]
    overreject = [r for r in fn if not r["hard_block_codes"]]
    review_true = [r for r in unique_rows if r["auto_decision"] == "REVIEW" and r["human_identity_label"] == "TRUE"]
    review_false = [r for r in unique_rows if r["auto_decision"] == "REVIEW" and r["human_identity_label"] == "FALSE"]
    for name, subset in (("all_audit_rows.csv", rows), ("latest_unique_candidates.csv", unique_rows),
                         ("conflicting_human_labels.csv", label_conflicts), ("false_positives.csv", fp),
                         ("false_negatives.csv", fn), ("review_human_true.csv", review_true),
                         ("review_human_false.csv", review_false),
                         ("sales_hard_block_identity_true.csv", hard_block_identity_true),
                         ("potential_overreject_identity_true.csv", overreject)):
        write_csv(output / name, subset)
    counts = matrix(unique_rows)
    raw_counts = matrix(rows)
    true_positive = counts["AUTO_TRUE"]["TRUE"]
    true_total = true_positive + counts["AUTO_TRUE"]["FALSE"]
    false_correct = counts["AUTO_FALSE"]["FALSE"]
    false_total = false_correct + counts["AUTO_FALSE"]["TRUE"]
    review_true_total = counts["REVIEW"]["TRUE"]
    review_labeled_total = review_true_total + counts["REVIEW"]["FALSE"]
    summary = {
        "human_audit_labeled_rows": len(rows),
        "unique_clinic_email_pairs": len({(r["clinic_id"], r["normalized_email"]) for r in rows}),
        "unique_candidates_with_consistent_latest_label": len(unique_rows),
        "candidates_with_conflicting_latest_human_labels": len(label_conflicts),
        "audit_rows_by_file": dict(source_counts),
        "uuid_linkage": {
            "real_uuid_rows": sum(not r["clinic_uuid_missing"] for r in rows),
            "uuid_missing_or_legacy_rows": sum(r["clinic_uuid_missing"] for r in rows),
            "legacy_id_rows": sum(r["clinic_uuid_kind"] == "LEGACY_ID" for r in rows),
            "missing_uuid_rows": sum(r["clinic_uuid_kind"] == "MISSING" for r in rows),
            "database_candidate_id_rows": sum(r["candidate_id_kind"] == "DATABASE" for r in rows),
            "offline_synthetic_candidate_id_rows": sum(r["candidate_id_kind"] == "OFFLINE_SYNTHETIC" for r in rows),
        },
        "auto_decision_counts": dict(Counter(r["auto_decision"] for r in unique_rows)),
        "raw_observation_auto_decision_counts": dict(Counter(r["auto_decision"] for r in rows)),
        "confusion_matrix": counts,
        "raw_observation_confusion_matrix": raw_counts,
        "auto_true_human_precision": true_positive / true_total if true_total else None,
        "auto_true_confidence_precision": {
            confidence: (
                sum(r["auto_decision"] == "AUTO_TRUE" and r["confidence"] == confidence
                    and r["human_identity_label"] == "TRUE" for r in unique_rows)
                / sum(r["auto_decision"] == "AUTO_TRUE" and r["confidence"] == confidence
                      and r["human_identity_label"] in {"TRUE", "FALSE"} for r in unique_rows)
                if any(r["auto_decision"] == "AUTO_TRUE" and r["confidence"] == confidence
                       and r["human_identity_label"] in {"TRUE", "FALSE"} for r in unique_rows) else None
            ) for confidence in ("HIGH", "MEDIUM", "LOW")
        },
        "auto_false_human_agreement": false_correct / false_total if false_total else None,
        "review_human_true_rate": review_true_total / review_labeled_total if review_labeled_total else None,
        "review_reduction_vs_sending_all_labeled_candidates": 1 - (sum(counts["REVIEW"].values()) / len(unique_rows)) if unique_rows else None,
        "grouped_confusion": grouped(unique_rows),
        "false_positive_rows": len(fp), "false_negative_rows": len(fn),
        "auto_false_human_true_rows": len(fn),
        "sales_hard_block_identity_true_rows": len(hard_block_identity_true),
        "potential_overreject_identity_true_rows": len(overreject),
        "review_human_true_rows": len(review_true), "review_human_false_rows": len(review_false),
        "human_uncertain_rows": sum(r["human_identity_label"] == "UNCERTAIN" for r in unique_rows),
        "human_unlabeled_rows": sum(not r["human_identity_label"] for r in unique_rows),
        "identity_inferred_rows": sum(r["identity_inferred_from_verified_status"] for r in unique_rows),
        "identity_ground_truth_only": True,
        "production_rule_changed_by_labels": False,
        "network_calls": 0, "production_db_access": 0,
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(replay(args.audit_root, args.output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
