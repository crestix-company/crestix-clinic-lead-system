"""Phase 4-B final quality audit and Production-adoption diff preview.

Read-only against the Phase-4B cache, the Phase-4B CSV artifacts, and the
Production Treatment sidecar. Writes new analysis artifacts under
artifacts/hybrid_phase4b/. Does not touch Production DB, Treatment sidecar,
MHLW DB, Comdesk, or the Phase-4B cache, and never re-crawls.
"""
from __future__ import annotations

import csv
import hashlib
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "hybrid_phase4b"
CACHE = ROOT / "data" / "hybrid_phase4b" / "structured_cache.sqlite3"
SIDECAR = Path("/Users/maekawahiroyuki/CrestixData/clinic-lead/treatment_research_final.sqlite3")
CLINIC_DB = Path("/Users/maekawahiroyuki/CrestixData/clinic-lead/clinics.sqlite3")
MHLW_DB = ROOT / "mhlw_dry_run" / "clinic_mhlw_departments_final.sqlite3"
SEED = "hybrid-treatment-phase4b-full-20261002-v1"

POSITIVE = ("CONFIRMED", "MENTIONED", "REVIEW")
PROJECT = {"CONFIRMED": "CONFIRMED", "MENTIONED": "REVIEW", "REVIEW": "REVIEW",
           "NOT_CONFIRMED": "NOT_CONFIRMED", "CANDIDATE_ONLY": "NOT_CONFIRMED"}
STATUS_RANK = {"NOT_CONFIRMED": 0, "REVIEW": 1, "CONFIRMED": 2}
ARTICLE_PATH = re.compile(r"/(?:blog|column|news|topics?|article|information|notice)(?:/|$)", re.I)
OFFER_PATTERN = re.compile(r"実施して|行って(?:い)?ます|対応(?:して)?(?:い)?ます|承って|提供して|取り扱って|可能です|ております|行っております")
NEGATION_PATTERN = re.compile(r"ていない|していない|しておりません|しておらず|ではありません|されていません|行っておりません|取り扱っておりません|行っていません")
GENERAL_DESC_PATTERN = re.compile(r"とは[、,]|一般的に|とは.{0,8}(?:治療法|検査|手術)です|を指します|について(?:説明|紹介)")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def netloc(url: str) -> str:
    if not url:
        return ""
    return urlsplit(url).netloc.lower().removeprefix("www.")


def main() -> None:
    hashes_before = {"clinics": sha256(CLINIC_DB), "research": sha256(SIDECAR), "mhlw": sha256(MHLW_DB)}

    population = pd.read_csv(OUT / "population.csv", encoding="utf-8-sig", dtype={"clinic_id": int})
    with sqlite3.connect(f"file:{CACHE.resolve()}?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        fetch = pd.read_sql_query(
            "SELECT clinic_id, fetch_status, identity_verified, pages_fetched, http_request_count, error AS fetch_error, final_url FROM clinic_fetch",
            db,
        )
    treatments = pd.read_csv(OUT / "treatment_results.csv", encoding="utf-8-sig", dtype={"clinic_id": int})

    if len(population) != 3290:
        raise AssertionError(f"population.csv row count changed: {len(population)}")
    if len(treatments) != 3631:
        raise AssertionError(f"treatment_results.csv row count changed: {len(treatments)}")

    # ---- 1. Phase4B final artifact (clinic x treatment grain, all 3,290 clinics) ----
    pop_cols = population[["clinic_id", "clinic_name", "sampling_group", "selected_identity_url"]].rename(
        columns={"selected_identity_url": "initial_url"}
    )
    base = pop_cols.merge(fetch, on="clinic_id", how="left")
    treat_cols = treatments[[
        "clinic_id", "treatment_category", "hybrid_status", "signal_source", "evidence_url",
        "evidence_text", "matched_alias", "confidence", "page_title",
    ]].rename(columns={
        "treatment_category": "treatment_category",
        "hybrid_status": "signal_status",
    })
    treat_cols["treatment_name"] = treat_cols["treatment_category"]
    treat_cols["evidence_type"] = treat_cols["signal_source"]
    treat_cols["source_page_type"] = treat_cols["signal_source"]

    final = base.merge(
        treat_cols[["clinic_id", "treatment_name", "treatment_category", "signal_status", "evidence_type",
                    "evidence_url", "evidence_text", "source_page_type", "confidence", "matched_alias"]],
        on="clinic_id", how="left",
    )
    final_cols = ["clinic_id", "clinic_name", "sampling_group", "initial_url",
                  "fetch_status", "identity_verified", "pages_fetched", "http_request_count", "fetch_error",
                  "treatment_name", "treatment_category", "signal_status", "evidence_type",
                  "evidence_url", "evidence_text", "source_page_type", "confidence", "matched_alias"]
    final = final[final_cols]
    final.to_csv(OUT / "phase4b_final_results.csv", index=False, encoding="utf-8-sig")
    if final["clinic_id"].nunique() != 3290:
        raise AssertionError("phase4b_final_results.csv lost or duplicated clinics")

    # ---- 2. Signal candidates (CONFIRMED/MENTIONED/REVIEW, clinic x treatment) ----
    signal_rows = treatments[treatments["hybrid_status"].isin(POSITIVE)].copy()
    if len(signal_rows) != 560:
        raise AssertionError(f"signal row count changed: {len(signal_rows)}")

    # ---- 6. Diff vs existing Treatment sidecar (read-only) ----
    with sqlite3.connect(f"file:{SIDECAR.resolve()}?mode=ro", uri=True) as sdb:
        sdb.execute("PRAGMA query_only=ON")
        sidecar = pd.read_sql_query(
            "SELECT clinic_id, treatment_category_name, research_status FROM clinic_treatment_research_final",
            sdb,
        )
    sidecar_map = {(int(r.clinic_id), r.treatment_category_name): r.research_status for r in sidecar.itertuples()}

    def classify(row):
        key = (int(row["clinic_id"]), row["treatment_category"])
        existing = sidecar_map.get(key)
        new_status = PROJECT[row["hybrid_status"]]
        if existing is None:
            return "NEW", ""
        if existing == new_status:
            return "ALREADY_EXISTS", existing
        if STATUS_RANK[new_status] > STATUS_RANK[existing]:
            return "STATUS_UPGRADE", existing
        return "CONFLICT", existing

    diff = signal_rows.apply(classify, axis=1, result_type="expand")
    diff.columns = ["sidecar_diff", "sidecar_existing_status"]
    signal_rows = pd.concat([signal_rows, diff], axis=1)

    TIER = {"CONFIRMED": "A_CONFIRMED_PENDING_HUMAN_REVIEW",
            "MENTIONED": "B_MENTIONED_HOLD_NOT_FOR_PRODUCTION",
            "REVIEW": "C_REVIEW_PENDING_HUMAN_REVIEW"}
    signal_rows["production_tier"] = signal_rows["hybrid_status"].map(TIER)

    signal_rows = signal_rows.merge(
        pop_cols.rename(columns={"initial_url": "clinic_initial_url"}), on="clinic_id", how="left"
    )
    signal_rows.to_csv(OUT / "phase4b_signal_candidates.csv", index=False, encoding="utf-8-sig")

    clinic_level_counts = {s: treatments.loc[treatments["hybrid_status"] == s, "clinic_id"].nunique() for s in POSITIVE}
    rowlevel_counts = {s: int((treatments["hybrid_status"] == s).sum()) for s in POSITIVE}

    # Per-status breakdown is mandatory: an aggregate NEW/STATUS_UPGRADE/etc. total
    # across CONFIRMED+MENTIONED+REVIEW is easy to misread as CONFIRMED-only.
    diff_by_status_rowlevel = {
        s: signal_rows.loc[signal_rows["hybrid_status"] == s, "sidecar_diff"].value_counts().to_dict()
        for s in POSITIVE
    }
    diff_by_status_cliniclevel = {
        s: signal_rows.loc[signal_rows["hybrid_status"] == s].groupby("clinic_id")["sidecar_diff"]
        .apply(lambda x: x.value_counts().idxmax()).value_counts().to_dict()
        for s in POSITIVE
    }
    diff_counts_rowlevel_total = signal_rows["sidecar_diff"].value_counts().to_dict()
    diff_counts_cliniclevel_total = signal_rows.groupby("clinic_id")["sidecar_diff"].apply(
        lambda s: s.value_counts().idxmax()
    ).value_counts().to_dict()

    # ---- 3. CONFIRMED quality audit (355 rows / 208 clinics) ----
    confirmed = treatments[treatments["hybrid_status"] == "CONFIRMED"].merge(
        pop_cols, on="clinic_id", how="left", suffixes=("", "_pop")
    ).merge(fetch[["clinic_id", "identity_verified"]], on="clinic_id", how="left")

    def audit_row(row):
        url_host = netloc(row["evidence_url"])
        hp_host = netloc(row["initial_url"])
        official_hp_source = row["signal_source"] != "CLINIC_NAME" and bool(url_host) and (url_host == hp_host)
        identity_verified_pass = bool(row["identity_verified"])
        evidence_url_present = bool(str(row["evidence_url"]).strip()) if pd.notna(row["evidence_url"]) else False
        evidence_text_present = bool(str(row["evidence_text"]).strip()) if pd.notna(row["evidence_text"]) else False
        alias_resolved = bool(str(row["matched_alias"]).strip()) if pd.notna(row["matched_alias"]) else False
        not_blog_only = not bool(ARTICLE_PATH.search(str(row["evidence_url"]) if pd.notna(row["evidence_url"]) else ""))
        identity_consistent = identity_verified_pass and (row["signal_source"] == "CLINIC_NAME" or not evidence_url_present or url_host == hp_host)
        core_checks_pass = identity_verified_pass and evidence_text_present and alias_resolved and not_blog_only and identity_consistent
        return pd.Series({
            "official_hp_source": official_hp_source,
            "identity_verified_pass": identity_verified_pass,
            "evidence_url_present": evidence_url_present,
            "evidence_text_present": evidence_text_present,
            "alias_resolved": alias_resolved,
            "not_blog_only": not_blog_only,
            "identity_consistent": identity_consistent,
            "core_checks_pass": core_checks_pass,
        })

    audit_flags = confirmed.apply(audit_row, axis=1)
    confirmed_audited = pd.concat([confirmed, audit_flags], axis=1)

    audit_summary = {
        "rows_total": len(confirmed_audited),
        "core_checks_pass": int(confirmed_audited["core_checks_pass"].sum()),
        "official_hp_source_true": int(confirmed_audited["official_hp_source"].sum()),
        "evidence_url_present_true": int(confirmed_audited["evidence_url_present"].sum()),
        "evidence_text_present_true": int(confirmed_audited["evidence_text_present"].sum()),
        "alias_resolved_true": int(confirmed_audited["alias_resolved"].sum()),
        "not_blog_only_true": int(confirmed_audited["not_blog_only"].sum()),
    }

    # deterministic stratified selection, same scheme as the existing human_audit_sample.csv
    def deterministic_sample(df: pd.DataFrame, quota: int, status_label: str) -> pd.DataFrame:
        pool = df.to_dict("records")
        chosen = []
        source_n: dict[str, int] = {}
        category_n: dict[str, int] = {}
        while pool and len(chosen) < quota:
            pool.sort(key=lambda r: (
                source_n.get(r["signal_source"], 0) + category_n.get(r["treatment_category"], 0),
                hashlib.sha256(f"{SEED}|audit|{status_label}|{r['clinic_id']}|{r['treatment_category']}".encode()).hexdigest(),
            ))
            row = pool.pop(0)
            chosen.append(row)
            source_n[row["signal_source"]] = source_n.get(row["signal_source"], 0) + 1
            category_n[row["treatment_category"]] = category_n.get(row["treatment_category"], 0) + 1
        return pd.DataFrame(chosen)

    confirmed_sample_100 = deterministic_sample(confirmed_audited, 100, "CONFIRMED")
    confirmed_sample_100 = confirmed_sample_100.rename(columns={"hybrid_status": "signal_status"})
    confirmed_sample_100["treatment_name"] = confirmed_sample_100["treatment_category"]
    # audit_label is intentionally left blank: human-entered only, never auto-filled.
    # Allowed values: CORRECT / INCORRECT / UNCERTAIN (enforced by
    # phase4b_human_review_summary.py, not by this generator).
    confirmed_sample_100["audit_label"] = ""
    confirmed_sample_100["audit_comment"] = ""
    REVIEW_READY_COLS = ["clinic_id", "clinic_name", "treatment_name", "treatment_category", "matched_alias",
                         "signal_status", "signal_source", "evidence_url", "evidence_text", "initial_url",
                         "audit_label", "audit_comment"]
    confirmed_sample_100[REVIEW_READY_COLS].to_csv(
        OUT / "confirmed_human_review_100.csv", index=False, encoding="utf-8-sig"
    )

    # ---- 4. MENTIONED classification (all 180 rows / 137 clinics) ----
    mentioned = treatments[treatments["hybrid_status"] == "MENTIONED"].copy()

    def classify_mentioned(row):
        text = str(row["evidence_text"]) if pd.notna(row["evidence_text"]) else ""
        alias = str(row["matched_alias"]) if pd.notna(row["matched_alias"]) else ""
        category = str(row["treatment_category"])
        if OFFER_PATTERN.search(text) and not NEGATION_PATTERN.search(text):
            return "LIKELY_PERFORMED"
        if GENERAL_DESC_PATTERN.search(text):
            return "GENERAL_DESCRIPTION_ONLY"
        if alias and (alias not in category) and (category not in alias):
            return "RELATED_TERM_ONLY"
        return "UNDETERMINABLE"

    mentioned["mentioned_classification"] = mentioned.apply(classify_mentioned, axis=1)
    mentioned_classification_counts = mentioned["mentioned_classification"].value_counts().to_dict()
    mentioned.to_csv(OUT / "mentioned_review.csv", index=False, encoding="utf-8-sig")

    # ---- 5. REVIEW full listing (all 25 rows / 22 clinics), human-review-ready ----
    review_all = treatments[treatments["hybrid_status"] == "REVIEW"].copy()
    # audit_label intentionally blank: human-entered only (CORRECT/INCORRECT/UNCERTAIN).
    review_all["audit_label"] = ""
    review_all["audit_comment"] = ""
    review_all.to_csv(OUT / "review_all_22.csv", index=False, encoding="utf-8-sig")
    if review_all["clinic_id"].nunique() != 22:
        raise AssertionError(f"REVIEW clinic count changed: {review_all['clinic_id'].nunique()}")

    # ---- 9. Final report ----
    hashes_after = {"clinics": sha256(CLINIC_DB), "research": sha256(SIDECAR), "mhlw": sha256(MHLW_DB)}
    if hashes_before != hashes_after:
        raise AssertionError("Production/sidecar/MHLW hash changed during read-only audit")

    fetch_status_counts = fetch["fetch_status"].value_counts().to_dict()

    report = {
        "phase4b_population": int(len(population)),
        "fetch_statuses": fetch_status_counts,
        "clinic_level_signal_counts": clinic_level_counts,
        "rowlevel_signal_counts": rowlevel_counts,
        "clinic_x_treatment_signal_rows_total": int(len(signal_rows)),
        "production_tiers": {
            "A_CONFIRMED_PENDING_HUMAN_REVIEW": {"rows": rowlevel_counts["CONFIRMED"], "clinics": clinic_level_counts["CONFIRMED"]},
            "B_MENTIONED_HOLD_NOT_FOR_PRODUCTION": {"rows": rowlevel_counts["MENTIONED"], "clinics": clinic_level_counts["MENTIONED"]},
            "C_REVIEW_PENDING_HUMAN_REVIEW": {"rows": rowlevel_counts["REVIEW"], "clinics": clinic_level_counts["REVIEW"]},
        },
        "sidecar_diff_by_status_rowlevel": diff_by_status_rowlevel,
        "sidecar_diff_by_status_cliniclevel": diff_by_status_cliniclevel,
        "sidecar_diff_rowlevel_TOTAL_all_statuses_combined": diff_counts_rowlevel_total,
        "sidecar_diff_cliniclevel_TOTAL_all_statuses_combined_distinct_clinics": diff_counts_cliniclevel_total,
        "confirmed_quality_audit_summary": audit_summary,
        "confirmed_human_review_sample_rows": int(len(confirmed_sample_100)),
        "mentioned_classification_counts_REFERENCE_ONLY_NOT_FOR_PROMOTION": mentioned_classification_counts,
        "review_all_rows": int(len(review_all)),
        "review_all_clinics": int(review_all["clinic_id"].nunique()),
        "production_db_sha256_unchanged": hashes_before["clinics"] == hashes_after["clinics"],
        "treatment_sidecar_sha256_unchanged": hashes_before["research"] == hashes_after["research"],
        "mhlw_db_sha256_unchanged": hashes_before["mhlw"] == hashes_after["mhlw"],
    }
    import json
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
