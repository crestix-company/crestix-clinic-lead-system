"""Sales Tier artifact priority: v3 -> v2 -> final."""
import csv

import src.master.sales_classification as sc_module


def _write_csv(path, tier):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "clinic_id", "clinic_name", "sales_tier", "sales_usable",
                "confidence", "human_review_needed",
            ],
        )
        writer.writeheader()
        writer.writerow({
            "clinic_id": 1,
            "clinic_name": "priority-test",
            "sales_tier": tier,
            "sales_usable": "true",
            "confidence": "HIGH",
            "human_review_needed": "false",
        })


def test_v3_candidate_is_preferred_over_v2_and_final(tmp_path, monkeypatch):
    d = tmp_path / "artifacts" / "sales_target_reclassification"
    v3 = d / "sales_target_classification_v3_candidate.csv"
    v2 = d / "sales_target_classification_final_v2_candidate.csv"
    final = d / "sales_target_classification_final.csv"
    sidecar = d / "sales_classification_sidecar.sqlite3"

    _write_csv(final, "UNKNOWN")
    _write_csv(v2, "LIKELY_TREATMENT")
    _write_csv(v3, "VERIFIED_TREATMENT")

    monkeypatch.setattr(sc_module, "SALES_CLASSIFICATION_CANDIDATES", (v3, v2, final))
    monkeypatch.setattr(sc_module, "SALES_CLASSIFICATION_SIDECAR_PATH", sidecar)

    assert sc_module.sales_classification_source_path() == v3
    summary = sc_module.sales_classification_summary()
    assert summary["source_path"] == v3
    assert summary["by_tier"] == {"A": 1, "B": 0, "C": 0, "D": 0}


def test_priority_falls_back_v3_to_v2_to_final(tmp_path, monkeypatch):
    d = tmp_path / "artifacts" / "sales_target_reclassification"
    v3 = d / "sales_target_classification_v3_candidate.csv"
    v2 = d / "sales_target_classification_final_v2_candidate.csv"
    final = d / "sales_target_classification_final.csv"

    _write_csv(final, "UNKNOWN")
    _write_csv(v2, "LIKELY_TREATMENT")

    monkeypatch.setattr(sc_module, "SALES_CLASSIFICATION_CANDIDATES", (v3, v2, final))
    assert sc_module.sales_classification_source_path() == v2

    v2.unlink()
    assert sc_module.sales_classification_source_path() == final
