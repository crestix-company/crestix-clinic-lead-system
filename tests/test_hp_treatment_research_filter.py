"""Phase7 Treatment Research（CONFIRMED根拠）filterのテスト。

ここで作る clinic_treatment_research_final テーブルは、このテスト専用の合成データであり、
本番Research Workerの出力ではない（mhlw_dry_run/TREATMENT_RESEARCH_CONTRACT.md参照）。
"""
import sqlite3

import pytest

from src.master.filters import Filters
from src.master.research_sidecar import (
    RESEARCH_SIDECAR_ENV_VAR,
    RESEARCH_SIDECAR_TABLE,
    ResearchSidecarUnavailableError,
)
from src.master.samples import sample_records
from src.master.store import ClinicStore

ALL = Filters(active_only=False, hp_only=False)

# テスト専用の合成スキーマ。mhlw_dry_run/TREATMENT_RESEARCH_CONTRACT.md の契約どおりの列構成。
CREATE_SQL = f"""
CREATE TABLE {RESEARCH_SIDECAR_TABLE}(
  clinic_id INTEGER NOT NULL,
  treatment_category_id TEXT NOT NULL,
  treatment_category_name TEXT NOT NULL,
  research_status TEXT NOT NULL,
  matched_alias TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  page_title TEXT NOT NULL DEFAULT '',
  provider_context TEXT NOT NULL DEFAULT '',
  exclusion_context TEXT NOT NULL DEFAULT '',
  evidence_engine_version TEXT NOT NULL DEFAULT '',
  taxonomy_version TEXT NOT NULL DEFAULT '',
  researched_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(clinic_id, treatment_category_id)
);
"""


def _make_sidecar(path, rows):
    """テスト用合成sidecar。実際のResearch Worker出力ではない。"""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(CREATE_SQL)
        conn.executemany(
            f"INSERT INTO {RESEARCH_SIDECAR_TABLE}"
            "(clinic_id,treatment_category_id,treatment_category_name,research_status,evidence_engine_version,taxonomy_version,researched_at)"
            " VALUES(?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def store_with_clinics(tmp_path):
    store = ClinicStore(tmp_path / "hp_treatment.db")
    store.import_master(sample_records())
    clinics = {row["clinic_name"]: row["id"] for row in store.query(ALL, limit=100)}
    return store, clinics


def test_sidecar_absent_existing_filters_still_work(tmp_path, monkeypatch, store_with_clinics):
    monkeypatch.setenv(RESEARCH_SIDECAR_ENV_VAR, str(tmp_path / "missing.sqlite3"))
    store, _ = store_with_clinics
    assert store.count(ALL) == 4


def test_sidecar_absent_raises_when_hp_treatment_filter_used(tmp_path, monkeypatch, store_with_clinics):
    monkeypatch.setenv(RESEARCH_SIDECAR_ENV_VAR, str(tmp_path / "missing.sqlite3"))
    store, _ = store_with_clinics
    with pytest.raises(ResearchSidecarUnavailableError):
        store.count(Filters(active_only=False, hp_only=False, hp_treatment_categories=["胃カメラ"]))


def test_sidecar_absent_raises_when_research_status_filter_used(tmp_path, monkeypatch, store_with_clinics):
    monkeypatch.setenv(RESEARCH_SIDECAR_ENV_VAR, str(tmp_path / "missing.sqlite3"))
    store, _ = store_with_clinics
    with pytest.raises(ResearchSidecarUnavailableError):
        store.count(Filters(active_only=False, hp_only=False, research_status=["CONFIRMED"]))


@pytest.fixture
def wired_sidecar(tmp_path, monkeypatch, store_with_clinics):
    store, clinics = store_with_clinics
    gastro = clinics["青空内視鏡クリニック"]  # CONFIRMED 胃カメラ
    ganka = clinics["若葉眼科医院"]  # REVIEW 白内障手術
    hifuka = clinics["日向皮膚科"]  # NOT_CONFIRMED ニキビ治療
    naika = clinics["月見内科"]  # FETCH_FAILED 胃カメラ、行なしのtreatmentはNOT_RESEARCHED相当
    sidecar_path = tmp_path / "treatment_research_final.sqlite3"
    _make_sidecar(str(sidecar_path), [
        (gastro, "gastroscopy", "胃カメラ", "CONFIRMED", "engine-v1", "7A-v2", "2026-09-01T00:00:00Z"),
        (ganka, "cataract", "白内障手術", "REVIEW", "engine-v1", "7A-v2", "2026-09-01T00:00:00Z"),
        (hifuka, "acne", "ニキビ治療", "NOT_CONFIRMED", "engine-v1", "7A-v2", "2026-09-01T00:00:00Z"),
        (naika, "gastroscopy", "胃カメラ", "FETCH_FAILED", "engine-v1", "7A-v2", "2026-09-01T00:00:00Z"),
    ])
    monkeypatch.setenv(RESEARCH_SIDECAR_ENV_VAR, str(sidecar_path))
    return store, clinics


def test_clinic_id_join_hits_correct_clinic_only(wired_sidecar):
    store, clinics = wired_sidecar
    rows = store.query(Filters(active_only=False, hp_only=False, hp_treatment_categories=["胃カメラ"]), limit=100)
    assert {r["id"] for r in rows} == {clinics["青空内視鏡クリニック"]}


def test_confirmed_only_is_included(wired_sidecar):
    store, clinics = wired_sidecar
    f = Filters(active_only=False, hp_only=False, hp_treatment_categories=["胃カメラ"])
    assert store.count(f) == 1


def test_review_is_excluded_from_hp_treatment_filter(wired_sidecar):
    store, clinics = wired_sidecar
    f = Filters(active_only=False, hp_only=False, hp_treatment_categories=["白内障手術"])
    assert store.count(f) == 0


def test_not_confirmed_is_excluded_from_hp_treatment_filter(wired_sidecar):
    store, clinics = wired_sidecar
    f = Filters(active_only=False, hp_only=False, hp_treatment_categories=["ニキビ治療"])
    assert store.count(f) == 0


def test_fetch_failed_is_excluded_from_hp_treatment_filter(wired_sidecar):
    store, clinics = wired_sidecar
    # 月見内科の胃カメラはFETCH_FAILEDなので、CONFIRMEDの青空内視鏡クリニックだけがヒットする。
    f = Filters(active_only=False, hp_only=False, hp_treatment_categories=["胃カメラ"])
    rows = store.query(f, limit=100)
    assert clinics["月見内科"] not in {r["id"] for r in rows}


def test_research_status_filter_finds_each_status(wired_sidecar):
    store, clinics = wired_sidecar
    assert store.count(Filters(active_only=False, hp_only=False, research_status=["CONFIRMED"])) == 1
    assert store.count(Filters(active_only=False, hp_only=False, research_status=["REVIEW"])) == 1
    assert store.count(Filters(active_only=False, hp_only=False, research_status=["NOT_CONFIRMED"])) == 1
    assert store.count(Filters(active_only=False, hp_only=False, research_status=["FETCH_FAILED"])) == 1


def test_research_status_not_researched_means_no_rows_at_all(wired_sidecar):
    store, clinics = wired_sidecar
    # 4医院すべて何らかの行を持つ合成データなので、NOT_RESEARCHEDは0件になるはず。
    assert store.count(Filters(active_only=False, hp_only=False, research_status=["NOT_RESEARCHED"])) == 0


def test_research_status_or_within_dimension(wired_sidecar):
    store, clinics = wired_sidecar
    f = Filters(active_only=False, hp_only=False, research_status=["CONFIRMED", "REVIEW"])
    assert store.count(f) == 2


def test_multiple_treatment_categories_per_clinic_no_duplicate_rows(tmp_path, monkeypatch, store_with_clinics):
    store, clinics = store_with_clinics
    gastro = clinics["青空内視鏡クリニック"]
    sidecar_path = tmp_path / "treatment_research_final_multi.sqlite3"
    _make_sidecar(str(sidecar_path), [
        (gastro, "gastroscopy", "胃カメラ", "CONFIRMED", "engine-v1", "7A-v2", "2026-09-01T00:00:00Z"),
        (gastro, "colonoscopy", "大腸カメラ", "CONFIRMED", "engine-v1", "7A-v2", "2026-09-01T00:00:00Z"),
    ])
    monkeypatch.setenv(RESEARCH_SIDECAR_ENV_VAR, str(sidecar_path))

    f = Filters(active_only=False, hp_only=False, hp_treatment_categories=["胃カメラ", "大腸カメラ"])
    rows = store.query(f, limit=100)
    ids = [r["id"] for r in rows]
    assert ids == [gastro]
    assert len(ids) == len(set(ids))
    assert store.count(f) == 1

    files = store.export(f)
    from src.io.input_loader import load_table
    csv_result = load_table(files["final_comdesk_import.csv"], "x.csv")
    assert len(csv_result.data) == 1
