"""Finalize the same_specialty_cluster (255) audit and produce the A/B/C
final sales list. Read-only on sidecar/MHLW/clinic DB. Does not overwrite
sales_target_classification.csv, ai_review_resolution.csv, or
sales_target_classification_final_preview.csv.

Key finding from the completed 100-clinic human audit (99% accuracy, 1
confirmed INCORRECT: clinic_id=2094): a mechanical search found 19 clinics
sharing clinic 2094's exact risk signature (single candidate category,
generic taxonomy alias, zero MHLW department data for the clinic). 18 of
those 19 were themselves already inside the audited 100-sample, and 17 of
those 18 were labeled CORRECT by the human reviewer. The hypothesis that
this mechanical signature predicts error is therefore empirically
disconfirmed (17/18 = 94.4% correct on direct inspection) - only the one
directly-audited-and-labeled-INCORRECT clinic (2094) is rejected.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "sales_target_reclassification"
AUDIT_SOURCE = Path(
    "/Users/maekawahiroyuki/Downloads/"
    "same_specialty_cluster_human_audit_100 - same_specialty_cluster_human_audit_100.csv"
)
ALLOWED = {"CORRECT", "INCORRECT", "UNCERTAIN"}


def load(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if any(v.strip() for v in r.values())]


def key_row(r: dict) -> tuple:
    return (r["clinic_id"], r["selected_treatment_category"])


def main() -> None:
    orig = load(OUT / "same_specialty_cluster_human_audit_100.csv")
    audited = load(AUDIT_SOURCE)
    if len(orig) != 100 or len(audited) != 100:
        raise AssertionError(f"row count mismatch: orig={len(orig)} audited={len(audited)}")
    if {r["clinic_id"] for r in orig} != {r["clinic_id"] for r in audited}:
        raise AssertionError("clinic_id set mismatch between original sample and audited file")

    # tamper check on everything except audit_label/audit_comment
    fields = [c for c in orig[0] if c not in ("audit_label", "audit_comment")]
    om = {key_row(r): tuple(r[f] for f in fields) for r in orig}
    nm = {key_row(r): tuple(r[f] for f in fields) for r in audited}
    mismatches = [k for k in om if om[k] != nm.get(k)]
    if mismatches:
        raise AssertionError(f"PHASE 1: semantic field mismatch outside audit_label/comment: {mismatches}")

    labeled = []
    for r in audited:
        raw = r["audit_label"].strip()
        normalized = raw.upper()
        if normalized not in ALLOWED:
            raise AssertionError(f"invalid audit_label {raw!r} for clinic_id={r['clinic_id']}")
        row = dict(r)
        row["audit_label"] = normalized
        row["audit_comment"] = r["audit_comment"].strip()
        labeled.append(row)

    with (OUT / "same_specialty_cluster_human_audit_100_labeled.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as f:
        w = csv.DictWriter(f, fieldnames=list(labeled[0].keys()))
        w.writeheader()
        w.writerows(labeled)

    from collections import Counter
    label_counts = Counter(r["audit_label"] for r in labeled)
    correct_ids = {r["clinic_id"] for r in labeled if r["audit_label"] == "CORRECT"}
    incorrect_ids = {r["clinic_id"] for r in labeled if r["audit_label"] == "INCORRECT"}
    uncertain_ids = {r["clinic_id"] for r in labeled if r["audit_label"] == "UNCERTAIN"}
    print(f"PHASE 1: audit normalized. {dict(label_counts)} "
          f"accuracy={len(correct_ids)/100:.1%}")
    if incorrect_ids != {"2094"}:
        raise AssertionError(f"expected sole INCORRECT=2094, got {incorrect_ids}")

    # ---- PHASE 2: same-signature search over all 255 ----
    risk = load(OUT / "same_specialty_cluster_risk_audit.csv")
    bug_signature_ids = {
        r["clinic_id"] for r in risk
        if len(r["candidate_categories"].split(" / ")) == 1
        and "GENERIC_ALIAS_PRESENT" in r["issues"]
        and "DEPARTMENT_MAPPING_MISSING_FOR_CLINIC" in r["issues"]
    }
    audited_ids = {r["clinic_id"] for r in labeled}
    bug_audited = bug_signature_ids & audited_ids
    bug_audited_correct = bug_audited & correct_ids
    bug_audited_incorrect = bug_audited & incorrect_ids
    bug_not_audited = bug_signature_ids - audited_ids

    print(f"PHASE 2: same-signature search found {len(bug_signature_ids)} clinics "
          f"(clinic_id=2094's exact pattern: single candidate, generic alias, "
          f"zero clinic department data). Of these, {len(bug_audited)} were already "
          f"in the 100-sample: {len(bug_audited_correct)} CORRECT, "
          f"{len(bug_audited_incorrect)} INCORRECT, {len(bug_not_audited)} not audited "
          f"({sorted(bug_not_audited)}).")
    print("  -> Pattern-level rejection hypothesis is DISCONFIRMED by direct evidence "
          f"({len(bug_audited_correct)}/{len(bug_audited)} = "
          f"{len(bug_audited_correct)/len(bug_audited):.1%} correct on human inspection). "
          "Only clinic_id=2094 itself is rejected; the other 18 same-signature clinics "
          "are not penalized for sharing a mechanical pattern that the audit shows is "
          "usually correct.")

    # ---- PHASE 3: FINAL_ACCEPT / FINAL_REVIEW / FINAL_REJECT over all 255 ----
    final_status = {}
    for r in risk:
        cid = r["clinic_id"]
        if cid in incorrect_ids:
            final_status[cid] = "FINAL_REJECT"
        elif cid in uncertain_ids:
            final_status[cid] = "FINAL_REVIEW"
        else:
            final_status[cid] = "FINAL_ACCEPT"
    if len(final_status) != 255:
        raise AssertionError(f"expected 255 cluster clinics, got {len(final_status)}")
    from collections import Counter as C2
    final_counts = C2(final_status.values())
    print(f"PHASE 3: {dict(final_counts)}")

    cluster_final_rows = [
        {"clinic_id": r["clinic_id"], "clinic_name": r["clinic_name"],
         "risk_verdict": r["risk_verdict"], "cluster_final_status": final_status[r["clinic_id"]],
         "human_audited": "true" if r["clinic_id"] in audited_ids else "false",
         "audit_label": next((a["audit_label"] for a in labeled if a["clinic_id"] == r["clinic_id"]), ""),
         "same_bug_signature_as_2094": "true" if r["clinic_id"] in bug_signature_ids else "false"}
        for r in risk
    ]
    with (OUT / "same_specialty_cluster_final_status.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(cluster_final_rows[0].keys()))
        w.writeheader()
        w.writerows(cluster_final_rows)

    # ---- PHASE 5: build sales_target_classification_final.csv ----
    preview = load(OUT / "sales_target_classification_final_preview.csv")
    preview_by_id = {r["clinic_id"]: r for r in preview}

    # Fix a type-mismatch bug from the prior task's PHASE 7 merge: the 7
    # clinics resolved by the second AI pass (human_review_second_pass.csv)
    # were never actually applied to the preview (string/int key mismatch
    # meant the lookup always missed), so they were left showing
    # human_review_needed=true despite being resolved. Correct that here
    # without touching the preview file itself.
    second_pass = load(OUT / "human_review_second_pass.csv")
    second_pass_accepted = {r["clinic_id"]: r for r in second_pass if r["second_pass_decision"] == "AUTO_ACCEPT"}
    if len(second_pass_accepted) != 7:
        raise AssertionError(f"expected 7 second-pass AUTO_ACCEPT clinics, found {len(second_pass_accepted)}")
    for cid, sp in second_pass_accepted.items():
        row = preview_by_id[cid]
        row["sales_category"] = sp["final_sales_category"] or row["sales_category"]
        row["treatment_category"] = sp["final_sales_category"] or row["treatment_category"]
        row["confidence"] = "HIGH"
        row["classification_source"] = "ai_resolution_second_pass_auto_accept"
        row["human_review_needed"] = "false"
        row["decision_reason"] = f"{sp['prior_review_reason']} -> {sp['second_pass_reason']} (second AI pass)"
    reject_ids = {cid for cid, s in final_status.items() if s == "FINAL_REJECT"}
    review_ids = {cid for cid, s in final_status.items() if s == "FINAL_REVIEW"}
    human_verified_ids = correct_ids  # the 99 confirmed-correct audited clinics

    final_rows = []
    for r in preview:
        cid = r["clinic_id"]
        row = dict(r)
        row["cluster_final_status"] = final_status.get(cid, "")
        if cid in reject_ids:
            row["sales_tier"], row["sales_category"], row["treatment_category"] = "UNKNOWN", "", ""
            row["confidence"] = "LOW"
            row["sales_usable"] = "false"
            row["classification_source"] = "cluster_human_audit_reject"
            row["human_verified"] = "true"
            row["decision_reason"] = ("Human Audit(same_specialty_cluster)でINCORRECT確定。"
                                       "下肢静脈瘤血管内治療(alias=レーザー治療)の自己申告なし、department corroborationなし。")
        elif cid in review_ids:
            row["confidence"] = "MEDIUM"
            row["human_review_needed"] = "true"
            row["classification_source"] = "cluster_human_audit_uncertain"
        elif cid in human_verified_ids:
            row["human_verified"] = "true"
            row["confidence"] = "HIGH"
            if row.get("classification_source", "").startswith("ai_resolution_auto_accept"):
                row["classification_source"] = "cluster_human_audit_confirmed"
        final_rows.append(row)

    with (OUT / "sales_target_classification_final.csv").open("w", encoding="utf-8-sig", newline="") as f:
        cols = list(final_rows[0].keys())
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(final_rows)

    from collections import Counter as C3
    tier_counts = C3(r["sales_tier"] for r in final_rows)
    a = tier_counts.get("VERIFIED_TREATMENT", 0)
    b = tier_counts.get("LIKELY_TREATMENT", 0)
    c = tier_counts.get("SPECIALTY_TARGET", 0)
    d = tier_counts.get("UNKNOWN", 0)
    sales_targetable = a + b + c

    summary = {
        "human_audit": {"total": 100, "CORRECT": label_counts.get("CORRECT", 0),
                         "INCORRECT": label_counts.get("INCORRECT", 0),
                         "UNCERTAIN": label_counts.get("UNCERTAIN", 0),
                         "accuracy_pct": round(label_counts.get("CORRECT", 0) / 100 * 100, 1)},
        "incorrect_clinic": {"clinic_id": 2094,
                              "clinic_name": "医療法人社団 ケイクリニック ケイレディースクリニック",
                              "selected_treatment_category": "下肢静脈瘤血管内治療"},
        "same_bug_signature_search": {
            "total_matching_signature": len(bug_signature_ids),
            "already_in_audit_sample": len(bug_audited),
            "audited_correct": len(bug_audited_correct),
            "audited_incorrect": len(bug_audited_incorrect),
            "not_audited": sorted(bug_not_audited),
            "conclusion": "pattern-level rejection hypothesis disconfirmed by direct evidence "
                           f"({len(bug_audited_correct)}/{len(bug_audited)} correct); "
                           "only clinic_id=2094 itself rejected",
        },
        "same_specialty_cluster_final": dict(final_counts),
        "A_VERIFIED_TREATMENT": a,
        "B_LIKELY_TREATMENT": b,
        "C_SPECIALTY_TARGET": c,
        "D_UNKNOWN": d,
        "sales_targetable_A_plus_B_plus_C": sales_targetable,
        "sales_usability_rate_pct": round(sales_targetable / 9097 * 100, 1),
        "human_review_remaining_280_untouched": sum(
            1 for r in final_rows if r.get("human_review_needed") == "true"
            and r.get("classification_source") not in ("cluster_human_audit_uncertain",)
        ),
    }
    (OUT / "phase3_final_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
