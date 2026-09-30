from scripts.phase7b_audit_queue import build_audit_queue, select_not_confirmed_sample, QUEUE_FIELDS


def _row(clinic_id, category, status, bucket="NEGATIVE_CANDIDATE", evidence_text="", matched_alias=""):
    return {
        "clinic_id": str(clinic_id), "clinic_name": f"clinic-{clinic_id}", "crestix_department": "眼科",
        "treatment_category": category, "candidate_bucket": bucket, "research_status": status,
        "evidence_text": evidence_text, "evidence_url": "https://clinic.example/",
        "evidence_page_title": "", "evidence_source_type": "OFFICIAL_HP", "matched_alias": matched_alias,
        "negative_context": "", "rule_version": "7A-v2", "researched_at": "2026-09-30T00:00:00Z",
        "selected_identity_url": "https://clinic.example/", "final_url": "https://clinic.example/",
        "fetch_status": "OK", "fetch_attempts": "1", "fetch_successes": "1",
        "processing_seconds": "0.1", "error": "",
    }


def test_queue_includes_all_confirmed_and_review_rows():
    rows = ([_row(i, "cat-a", "CONFIRMED") for i in range(1, 4)]
            + [_row(i, "cat-a", "REVIEW") for i in range(4, 7)]
            + [_row(i, "cat-a", "NOT_CONFIRMED") for i in range(7, 30)])
    queue = build_audit_queue(rows)
    confirmed_in_queue = [r for r in queue if r["research_status"] == "CONFIRMED"]
    review_in_queue = [r for r in queue if r["research_status"] == "REVIEW"]
    assert len(confirmed_in_queue) == 3
    assert len(review_in_queue) == 3
    assert all(r["audit_reason"] == "ALL_CONFIRMED" for r in confirmed_in_queue)
    assert all(r["audit_reason"] == "ALL_REVIEW" for r in review_in_queue)


def test_not_confirmed_sample_is_stratified_per_category_and_deterministic():
    rows = []
    for cat in ("cat-a", "cat-b", "cat-c"):
        rows += [_row(i, cat, "NOT_CONFIRMED") for i in range(100, 120)]
    first = select_not_confirmed_sample(rows, per_category=2)
    second = select_not_confirmed_sample(rows, per_category=2)
    assert first == second
    categories_covered = {r["treatment_category"] for r in first}
    assert categories_covered == {"cat-a", "cat-b", "cat-c"}
    for cat in categories_covered:
        assert sum(r["treatment_category"] == cat for r in first) == 2


def test_not_confirmed_sample_prefers_positive_candidate_and_evidence_rows():
    rows = [_row(i, "cat-a", "NOT_CONFIRMED") for i in range(1, 20)]
    rows.append(_row(999, "cat-a", "NOT_CONFIRMED", bucket="POSITIVE_CANDIDATE"))
    rows.append(_row(998, "cat-a", "NOT_CONFIRMED", evidence_text="他院へ紹介します。"))
    sample = select_not_confirmed_sample(rows, per_category=2)
    selected_ids = {r["clinic_id"] for r in sample}
    assert "999" in selected_ids
    assert "998" in selected_ids


def test_queue_has_required_columns_and_blank_human_fields():
    rows = [_row(1, "cat-a", "CONFIRMED"), _row(2, "cat-a", "NOT_CONFIRMED")]
    queue = build_audit_queue(rows)
    for row in queue:
        assert set(QUEUE_FIELDS) <= set(row.keys())
        assert row["human_label"] == ""
        assert row["human_comment"] == ""
        assert row["audit_required"] == "TRUE"
