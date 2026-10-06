"""正式Sales Tier分類(SSOT)のsidecar接続（synthetic fixture。本番artifactは使わない）。"""
import csv

import pytest

import src.master.sales_classification as sc_module
import src.master.store as store_module
from src.master.filters import Filters
from src.master.samples import sample_records
from src.master.store import ClinicStore


def _write_csv(path, rows):
    fieldnames = ["clinic_id", "clinic_name", "sales_tier", "sales_usable", "confidence", "human_review_needed"]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _patch_classification_dir(monkeypatch, tmp_path):
    classification_dir = tmp_path / "artifacts" / "sales_target_reclassification"
    classification_dir.mkdir(parents=True)
    v2_path = classification_dir / "sales_target_classification_final_v2_candidate.csv"
    final_path = classification_dir / "sales_target_classification_final.csv"
    sidecar_path = classification_dir / "sales_classification_sidecar.sqlite3"
    monkeypatch.setattr(sc_module, "SALES_CLASSIFICATION_CANDIDATES", (v2_path, final_path))
    monkeypatch.setattr(sc_module, "SALES_CLASSIFICATION_SIDECAR_PATH", sidecar_path)
    monkeypatch.setattr(store_module, "SALES_CLASSIFICATION_SIDECAR_PATH", sidecar_path)
    return v2_path, final_path, sidecar_path


def _store_with_sample_clinics(tmp_path):
    store = ClinicStore(tmp_path / "clinics.db")
    with store.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        for i, record in enumerate(sample_records(), start=1):
            cid = c.execute(
                "INSERT INTO clinics(uuid,base_json,first_seen_at,last_seen_at,source_as_of_date,is_new) "
                "VALUES('','{}','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00','',0)"
            ).lastrowid
            store._project(c, cid)
            assert cid == i
    return store


def test_no_artifact_means_unavailable_and_default_filters_still_work(tmp_path, monkeypatch):
    _patch_classification_dir(monkeypatch, tmp_path)
    assert sc_module.sales_classification_available() is False
    assert sc_module.sales_classification_source_path() is None
    assert sc_module.sales_classification_summary() is None
    store = _store_with_sample_clinics(tmp_path)
    assert store.count(Filters(active_only=False, hp_only=False)) == 4


def test_filters_requiring_sidecar_raise_when_unavailable(tmp_path, monkeypatch):
    _patch_classification_dir(monkeypatch, tmp_path)
    store = _store_with_sample_clinics(tmp_path)
    with pytest.raises(sc_module.SalesClassificationUnavailableError):
        store.count(Filters(active_only=False, hp_only=False, sales_tiers=["A"]))


def test_v2_candidate_is_preferred_over_final(tmp_path, monkeypatch):
    v2_path, final_path, _ = _patch_classification_dir(monkeypatch, tmp_path)
    _write_csv(final_path, [
        {"clinic_id": 1, "clinic_name": "final版", "sales_tier": "VERIFIED_TREATMENT",
         "sales_usable": "true", "confidence": "HIGH", "human_review_needed": "false"},
    ])
    _write_csv(v2_path, [
        {"clinic_id": 1, "clinic_name": "v2候補版", "sales_tier": "LIKELY_TREATMENT",
         "sales_usable": "true", "confidence": "MEDIUM", "human_review_needed": "false"},
    ])
    assert sc_module.sales_classification_source_path() == v2_path
    summary = sc_module.sales_classification_summary()
    assert summary["by_tier"] == {"A": 0, "B": 1, "C": 0, "D": 0}


def test_unknown_sales_tier_value_is_rejected(tmp_path, monkeypatch):
    _, final_path, _ = _patch_classification_dir(monkeypatch, tmp_path)
    _write_csv(final_path, [
        {"clinic_id": 1, "clinic_name": "不正データ", "sales_tier": "NOT_A_REAL_TIER",
         "sales_usable": "true", "confidence": "HIGH", "human_review_needed": "false"},
    ])
    with pytest.raises(sc_module.SalesClassificationUnavailableError):
        sc_module.ensure_sales_classification_sidecar()


def test_missing_required_column_is_rejected(tmp_path, monkeypatch):
    classification_dir = tmp_path / "artifacts" / "sales_target_reclassification"
    classification_dir.mkdir(parents=True)
    final_path = classification_dir / "sales_target_classification_final.csv"
    sidecar_path = classification_dir / "sales_classification_sidecar.sqlite3"
    monkeypatch.setattr(sc_module, "SALES_CLASSIFICATION_CANDIDATES", (final_path,))
    monkeypatch.setattr(sc_module, "SALES_CLASSIFICATION_SIDECAR_PATH", sidecar_path)
    with final_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["clinic_id", "clinic_name", "sales_tier"])
        writer.writeheader()
        writer.writerow({"clinic_id": 1, "clinic_name": "欠損列", "sales_tier": "VERIFIED_TREATMENT"})
    with pytest.raises(sc_module.SalesClassificationUnavailableError):
        sc_module.ensure_sales_classification_sidecar()


def test_tier_confidence_and_human_review_filters_combine_with_and(tmp_path, monkeypatch):
    _, final_path, _ = _patch_classification_dir(monkeypatch, tmp_path)
    _write_csv(final_path, [
        {"clinic_id": 1, "clinic_name": "A1", "sales_tier": "VERIFIED_TREATMENT",
         "sales_usable": "true", "confidence": "HIGH", "human_review_needed": "false"},
        {"clinic_id": 2, "clinic_name": "B-review", "sales_tier": "LIKELY_TREATMENT",
         "sales_usable": "true", "confidence": "MEDIUM", "human_review_needed": "true"},
        {"clinic_id": 3, "clinic_name": "B-clean", "sales_tier": "LIKELY_TREATMENT",
         "sales_usable": "true", "confidence": "HIGH", "human_review_needed": "false"},
        {"clinic_id": 4, "clinic_name": "D", "sales_tier": "UNKNOWN",
         "sales_usable": "false", "confidence": "LOW", "human_review_needed": "false"},
    ])
    store = _store_with_sample_clinics(tmp_path)
    base = dict(active_only=False, hp_only=False)

    assert store.count(Filters(**base, sales_tiers=["A"])) == 1
    assert store.count(Filters(**base, sales_tiers=["A", "B"])) == 3
    assert store.count(Filters(**base, sales_tiers=["D"])) == 1
    assert store.count(Filters(**base)) == 4  # Sales Tier指定なしは全件(他filterなし)

    assert store.count(Filters(**base, sales_tiers=["A", "B"], exclude_human_review=True)) == 2
    assert store.count(Filters(**base, sales_tiers=["A", "B"], sales_confidence=["HIGH"])) == 2
    assert store.count(Filters(**base, sales_tiers=["A", "B"], sales_confidence=["MEDIUM"])) == 1

    summary = sc_module.sales_classification_summary()
    assert summary == {
        "source_path": final_path, "total": 4, "sales_usable": 3,
        "by_tier": {"A": 1, "B": 2, "C": 0, "D": 1},
    }


def test_sidecar_rebuild_is_skipped_when_source_unchanged_but_rebuilds_on_change(tmp_path, monkeypatch):
    _, final_path, sidecar_path = _patch_classification_dir(monkeypatch, tmp_path)
    _write_csv(final_path, [
        {"clinic_id": 1, "clinic_name": "A1", "sales_tier": "VERIFIED_TREATMENT",
         "sales_usable": "true", "confidence": "HIGH", "human_review_needed": "false"},
    ])
    sc_module.ensure_sales_classification_sidecar()
    first_mtime = sidecar_path.stat().st_mtime_ns
    sc_module.ensure_sales_classification_sidecar()
    assert sidecar_path.stat().st_mtime_ns == first_mtime  # 変更なしなら再生成しない

    _write_csv(final_path, [
        {"clinic_id": 1, "clinic_name": "A1", "sales_tier": "VERIFIED_TREATMENT",
         "sales_usable": "true", "confidence": "HIGH", "human_review_needed": "false"},
        {"clinic_id": 2, "clinic_name": "B1", "sales_tier": "LIKELY_TREATMENT",
         "sales_usable": "true", "confidence": "MEDIUM", "human_review_needed": "false"},
    ])
    sc_module.ensure_sales_classification_sidecar()
    assert sc_module.sales_classification_summary()["total"] == 2
