import pytest

from scripts.phase7b_audit_aggregate import aggregate


def _row(clinic_id, category, status, human_label="", bucket="NEGATIVE_CANDIDATE",
         evidence_text="", matched_alias=""):
    return {
        "clinic_id": str(clinic_id), "clinic_name": f"clinic-{clinic_id}", "crestix_department": "眼科",
        "treatment_category": category, "candidate_bucket": bucket, "research_status": status,
        "evidence_text": evidence_text, "evidence_url": "https://clinic.example/",
        "matched_alias": matched_alias, "negative_context": "",
        "human_label": human_label, "human_comment": "",
        "audit_required": "TRUE", "audit_reason": "ALL_CONFIRMED",
    }


def test_aggregate_refuses_when_every_human_label_is_blank():
    rows = [_row(1, "cat-a", "CONFIRMED"), _row(2, "cat-a", "REVIEW")]
    result = aggregate(rows)
    assert result["labeled_count"] == 0
    assert "message" in result
    assert "confirmed_precision" not in result


def test_aggregate_computes_confirmed_precision_only_from_labeled_rows():
    rows = [
        _row(1, "cat-a", "CONFIRMED", human_label="TRUE_POSITIVE", matched_alias="alias-a"),
        _row(2, "cat-a", "CONFIRMED", human_label="FALSE_POSITIVE", matched_alias="alias-a"),
        _row(3, "cat-a", "CONFIRMED", human_label=""),  # unlabeled, must be excluded
    ]
    result = aggregate(rows)
    assert result["labeled_count"] == 2
    assert result["unlabeled_count"] == 1
    cp = result["confirmed_precision"]
    assert cp["true_positive"] == 1
    assert cp["false_positive"] == 1
    assert cp["precision"] == 0.5
    assert result["alias_false_positive_counts"] == {"alias-a": 1}


def test_aggregate_review_true_positive_rate_and_not_confirmed_false_negative_rate():
    rows = [
        _row(1, "cat-a", "REVIEW", human_label="TRUE_POSITIVE"),
        _row(2, "cat-a", "REVIEW", human_label="UNCERTAIN"),
        _row(3, "cat-b", "NOT_CONFIRMED", human_label="FALSE_NEGATIVE"),
        _row(4, "cat-b", "NOT_CONFIRMED", human_label="TRUE_NEGATIVE"),
    ]
    result = aggregate(rows)
    review_stats = result["review_true_positive_rate"]
    assert review_stats["true_positive"] == 1
    assert review_stats["labeled_decided_total"] == 1  # UNCERTAIN excluded from the decided denominator
    nc_stats = result["not_confirmed_sample_false_negative_rate"]
    assert nc_stats["false_negative"] == 1
    assert nc_stats["labeled_decided_total"] == 2
    assert nc_stats["rate"] == 0.5


def test_aggregate_flags_severe_false_positive_with_negative_phrase_in_evidence():
    rows = [_row(1, "cat-a", "CONFIRMED", human_label="FALSE_POSITIVE",
                 evidence_text="当院では実施していません。他院をご紹介します。")]
    result = aggregate(rows)
    assert result["severe_false_positive_negative_sentence_count"] == 1


def test_aggregate_rejects_unknown_human_label_value():
    rows = [_row(1, "cat-a", "CONFIRMED", human_label="MAYBE")]
    with pytest.raises(ValueError):
        aggregate(rows)
