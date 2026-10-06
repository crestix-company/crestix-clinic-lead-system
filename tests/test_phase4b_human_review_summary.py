import csv

import pytest

from scripts.phase4b_human_review_summary import (
    ALLOWED_LABELS,
    accuracy,
    grouped_summary,
    label_counts,
    read_rows,
    summarize_confirmed,
    summarize_review,
    validate_labels,
)


def write_csv(path, rows, fieldnames):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def test_label_counts_and_accuracy_basic():
    rows = [{"audit_label": "CORRECT"}, {"audit_label": "CORRECT"},
            {"audit_label": "INCORRECT"}, {"audit_label": "UNCERTAIN"}, {"audit_label": ""}]
    counts = label_counts(rows)
    assert counts == {"CORRECT": 2, "INCORRECT": 1, "UNCERTAIN": 1, "UNLABELED": 1, "total": 5}
    assert accuracy(counts) == round(2 / 3, 4)


def test_accuracy_none_when_no_definitive_labels():
    rows = [{"audit_label": "UNCERTAIN"}, {"audit_label": ""}]
    counts = label_counts(rows)
    assert accuracy(counts) is None


def test_validate_labels_rejects_unknown_value():
    rows = [{"audit_label": "MAYBE"}]
    with pytest.raises(ValueError, match="invalid audit_label"):
        validate_labels(rows, "fake.csv")


def test_validate_labels_accepts_all_allowed_values():
    rows = [{"audit_label": v} for v in ALLOWED_LABELS]
    validate_labels(rows, "fake.csv")  # must not raise


def test_grouped_summary_by_signal_source():
    rows = [
        {"audit_label": "CORRECT", "signal_source": "HOME_MENU"},
        {"audit_label": "INCORRECT", "signal_source": "HOME_MENU"},
        {"audit_label": "CORRECT", "signal_source": "OFFICIAL_HP_TEXT"},
    ]
    grouped = grouped_summary(rows, "signal_source")
    assert grouped["HOME_MENU"]["CORRECT"] == 1
    assert grouped["HOME_MENU"]["INCORRECT"] == 1
    assert grouped["HOME_MENU"]["accuracy"] == pytest.approx(0.5)
    assert grouped["OFFICIAL_HP_TEXT"]["CORRECT"] == 1
    assert grouped["OFFICIAL_HP_TEXT"]["accuracy"] == 1.0


def test_summarize_confirmed_structure():
    rows = [
        {"audit_label": "CORRECT", "signal_source": "HOME_MENU", "treatment_category": "ICL手術"},
        {"audit_label": "INCORRECT", "signal_source": "HOME_MENU", "treatment_category": "ICL手術"},
    ]
    summary = summarize_confirmed(rows)
    assert summary["overall"]["total"] == 2
    assert summary["overall"]["accuracy"] == pytest.approx(0.5)
    assert "HOME_MENU" in summary["by_signal_source"]
    assert "ICL手術" in summary["by_treatment_category"]


def test_summarize_review_overall_only():
    rows = [{"audit_label": "CORRECT"}, {"audit_label": "UNCERTAIN"}]
    summary = summarize_review(rows)
    assert summary["overall"]["CORRECT"] == 1
    assert summary["overall"]["UNCERTAIN"] == 1
    assert summary["overall"]["accuracy"] == 1.0  # 1 CORRECT, 0 INCORRECT -> 1/1


def test_read_rows_round_trip(tmp_path):
    path = tmp_path / "sample.csv"
    write_csv(path, [{"clinic_id": "1", "audit_label": "CORRECT"}], ["clinic_id", "audit_label"])
    rows = read_rows(path)
    assert rows == [{"clinic_id": "1", "audit_label": "CORRECT"}]


def test_blank_audit_label_counts_as_unlabeled_not_crash(tmp_path):
    path = tmp_path / "sample.csv"
    write_csv(path, [{"clinic_id": "1", "audit_label": ""}], ["clinic_id", "audit_label"])
    rows = read_rows(path)
    validate_labels(rows, "sample.csv")
    counts = label_counts(rows)
    assert counts["UNLABELED"] == 1
    assert accuracy(counts) is None
