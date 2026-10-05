"""営業用Treatment Mapping: 標榜診療科×治療(sales_pairs)を、
legacy evidence(research_results.treatment_evidence)とTreatment Research sidecar
(clinic_treatment_research_final.research_status='CONFIRMED')のOR(UNION DISTINCT、
clinic_id基準で二重カウントなし)で評価する(synthetic fixture。本番sidecar/artifactは使わない)。

旧taxonomyの広義カテゴリは営業UI上「内視鏡」と表示し、このUNIONの対象外
(胃カメラ/大腸カメラへ無条件に振り分けない)であることも確認する。
"""
import sqlite3

import pytest

from src.master.filters import Filters
from src.master.research_sidecar import RESEARCH_SIDECAR_ENV_VAR, RESEARCH_SIDECAR_TABLE
from src.master.samples import sample_records
from src.master.store import ClinicStore

ALL = dict(active_only=False, hp_only=False)

CREATE_TREATMENT_SQL = f"""
CREATE TABLE {RESEARCH_SIDECAR_TABLE}(
  clinic_id INTEGER NOT NULL,
  treatment_category_id TEXT NOT NULL,
  treatment_category_name TEXT NOT NULL,
  research_status TEXT NOT NULL CHECK(research_status IN ('CONFIRMED','REVIEW','NOT_CONFIRMED')),
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
    conn = sqlite3.connect(path)
    try:
        conn.executescript(CREATE_TREATMENT_SQL)
        conn.executemany(
            f"INSERT INTO {RESEARCH_SIDECAR_TABLE}"
            "(clinic_id,treatment_category_id,treatment_category_name,research_status,"
            "evidence_engine_version,taxonomy_version,researched_at) VALUES(?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _evidence(category, keyword, source="HOME_MENU"):
    return {"category": category, "keyword": keyword, "source": source, "confidence": .98}


@pytest.fixture
def store_with_gastro_clinic(tmp_path):
    """青空内視鏡クリニック(sample_records()[0])は標榜診療科=消化器内科。"""
    store = ClinicStore(tmp_path / "sidecar_union.db")
    store.import_master(sample_records())
    cid = next(r["id"] for r in store.query(Filters(**ALL), limit=100) if r["clinic_name"] == "青空内視鏡クリニック")
    return store, cid


def test_sidecar_only_confirmed_match_without_any_legacy_evidence(tmp_path, monkeypatch, store_with_gastro_clinic):
    store, cid = store_with_gastro_clinic
    sidecar_path = tmp_path / "treatment_research_final.sqlite3"
    _make_sidecar(sidecar_path, [(cid, "gastroscopy", "胃カメラ検査", "CONFIRMED", "v1", "7A-v2", "2026-10-01T00:00:00Z")])
    monkeypatch.setenv(RESEARCH_SIDECAR_ENV_VAR, str(sidecar_path))
    # legacy側のtreatment_evidenceは一切保存していない(research_resultsが空)。
    f = Filters(**ALL, sales_pairs=[("消化器内科", "胃カメラ")])
    assert store.count(f) == 1


def test_legacy_only_match_still_works_when_sidecar_absent(tmp_path, monkeypatch, store_with_gastro_clinic):
    store, cid = store_with_gastro_clinic
    monkeypatch.setenv(RESEARCH_SIDECAR_ENV_VAR, str(tmp_path / "missing.sqlite3"))
    store.save_research(cid, {
        "treatment_categories": ["内視鏡"],
        "treatment_evidence": [_evidence("内視鏡", "胃カメラ")],
    })
    f = Filters(**ALL, sales_pairs=[("消化器内科", "胃カメラ")])
    assert store.count(f) == 1


def test_legacy_and_sidecar_both_present_does_not_double_count(tmp_path, monkeypatch, store_with_gastro_clinic):
    store, cid = store_with_gastro_clinic
    store.save_research(cid, {
        "treatment_categories": ["内視鏡"],
        "treatment_evidence": [_evidence("内視鏡", "胃カメラ")],
    })
    sidecar_path = tmp_path / "treatment_research_final.sqlite3"
    _make_sidecar(sidecar_path, [(cid, "gastroscopy", "胃カメラ検査", "CONFIRMED", "v1", "7A-v2", "2026-10-01T00:00:00Z")])
    monkeypatch.setenv(RESEARCH_SIDECAR_ENV_VAR, str(sidecar_path))
    f = Filters(**ALL, sales_pairs=[("消化器内科", "胃カメラ")])
    assert store.count(f) == 1  # UNION DISTINCT: 二重カウントしない


def test_sidecar_review_or_not_confirmed_is_not_counted(tmp_path, monkeypatch, store_with_gastro_clinic):
    store, cid = store_with_gastro_clinic
    sidecar_path = tmp_path / "treatment_research_final.sqlite3"
    _make_sidecar(sidecar_path, [
        (cid, "gastroscopy", "胃カメラ検査", "REVIEW", "v1", "7A-v2", "2026-10-01T00:00:00Z"),
    ])
    monkeypatch.setenv(RESEARCH_SIDECAR_ENV_VAR, str(sidecar_path))
    f = Filters(**ALL, sales_pairs=[("消化器内科", "胃カメラ")])
    assert store.count(f) == 0


def test_bare_endoscopy_category_is_not_mapped_to_sidecar_gastroscopy(tmp_path, monkeypatch, store_with_gastro_clinic):
    """営業UIの「内視鏡」はsidecar UNIONの対象外: sidecarのCONFIRMEDだけでは成立しない。"""
    store, cid = store_with_gastro_clinic
    sidecar_path = tmp_path / "treatment_research_final.sqlite3"
    _make_sidecar(sidecar_path, [(cid, "gastroscopy", "胃カメラ検査", "CONFIRMED", "v1", "7A-v2", "2026-10-01T00:00:00Z")])
    monkeypatch.setenv(RESEARCH_SIDECAR_ENV_VAR, str(sidecar_path))
    f = Filters(**ALL, sales_pairs=[("消化器内科", "内視鏡")])
    assert store.count(f) == 0


def test_bare_endoscopy_category_still_matches_via_legacy_category_evidence(tmp_path, store_with_gastro_clinic):
    store, cid = store_with_gastro_clinic
    store.save_research(cid, {
        "treatment_categories": ["内視鏡"],
        "treatment_evidence": [_evidence("内視鏡", "内視鏡")],
    })
    f = Filters(**ALL, sales_pairs=[("消化器内科", "内視鏡")])
    assert store.count(f) == 1


def test_colon_sidecar_category_covers_both_camera_and_endoscope_wording(tmp_path, monkeypatch, store_with_gastro_clinic):
    """大腸カメラ・大腸内視鏡はどちらもsidecarの「大腸カメラ検査」カテゴリへ対応付けられている。"""
    store, cid = store_with_gastro_clinic
    sidecar_path = tmp_path / "treatment_research_final.sqlite3"
    _make_sidecar(sidecar_path, [(cid, "colonoscopy", "大腸カメラ検査", "CONFIRMED", "v1", "7A-v2", "2026-10-01T00:00:00Z")])
    monkeypatch.setenv(RESEARCH_SIDECAR_ENV_VAR, str(sidecar_path))
    assert store.count(Filters(**ALL, sales_pairs=[("消化器内科", "大腸カメラ")])) == 1
    assert store.count(Filters(**ALL, sales_pairs=[("消化器内科", "大腸内視鏡")])) == 1


def test_sales_pairs_still_works_when_sidecar_missing_entirely(tmp_path, monkeypatch, store_with_gastro_clinic):
    """sidecarが一時的に無くてもsales_pairs自体は例外にならず、legacy単独で評価される。"""
    store, cid = store_with_gastro_clinic
    monkeypatch.setenv(RESEARCH_SIDECAR_ENV_VAR, str(tmp_path / "does_not_exist.sqlite3"))
    f = Filters(**ALL, sales_pairs=[("消化器内科", "胃カメラ")])
    assert store.count(f) == 0  # legacy evidenceも無いので0件だが、例外は発生しない
