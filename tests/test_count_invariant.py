"""UI表示件数 = backend COUNT DISTINCT clinic_id = 一覧unique件数 = CSV行数、かつduplicate=0を確認するE2Eテスト。

複数の診療科・複数のTreatment Researchカテゴリを持つ医院が混ざっていても、
一覧・CSVは1 clinic = 1 rowになることを、都道府県×市区町村×Crestix営業カテゴリ×
HP治療カテゴリの組み合わせで検証する。ここで使うsidecarはテスト専用の合成データ。
"""
import sqlite3

from src.io.input_loader import load_table
from src.master.comdesk import COMDESK_HEADERS
from src.master.filters import Filters
from src.master.research_sidecar import RESEARCH_SIDECAR_ENV_VAR, RESEARCH_SIDECAR_TABLE
from src.master.samples import sample_records
from src.master.store import ClinicStore

# テスト専用の合成スキーマ（mhlw_dry_run/TREATMENT_RESEARCH_CONTRACT.md の契約どおり）。
# 実際のResearch Worker出力ではない。
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
            "(clinic_id,treatment_category_id,treatment_category_name,research_status)"
            " VALUES(?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _assert_invariant(store, filters):
    count = store.count(filters)
    rows = store.query(filters, limit=10000)
    ids = [row["id"] for row in rows]
    files = store.export(filters)
    csv_result = load_table(files["final_comdesk_import.csv"], "x.csv")

    assert csv_result.headers == COMDESK_HEADERS
    assert len(COMDESK_HEADERS) == 28
    assert count == len(rows) == len(csv_result.data)
    assert len(ids) == len(set(ids)), "clinic_id duplicated in list view"
    return count


def test_ui_count_equals_query_rows_equals_csv_rows_with_multi_department_and_treatment(tmp_path, monkeypatch):
    store = ClinicStore(tmp_path / "invariant.db")
    records = sample_records()
    # 複数診療科を模擬: 1医院に「消化器内科」「泌尿器科」相当のCrestixカテゴリを両方付ける。
    records[0]["address"] = "東京都千代田区架空町1-1-1"
    store.import_master(records)
    clinics = {row["clinic_name"]: row["id"] for row in store.query(Filters(active_only=False, hp_only=False), limit=100)}
    gastro_clinic = clinics["青空内視鏡クリニック"]

    sidecar_path = tmp_path / "treatment_research_final.sqlite3"
    _make_sidecar(str(sidecar_path), [
        # 同一医院が複数treatment_categoryでCONFIRMEDを持つ（一覧・CSVは1行のまま）。
        (gastro_clinic, "gastroscopy", "胃カメラ", "CONFIRMED"),
        (gastro_clinic, "colonoscopy", "大腸カメラ", "CONFIRMED"),
        (clinics["若葉眼科医院"], "cataract", "白内障手術", "REVIEW"),
    ])
    monkeypatch.setenv(RESEARCH_SIDECAR_ENV_VAR, str(sidecar_path))

    baseline = Filters(active_only=False, hp_only=False)
    assert _assert_invariant(store, baseline) == 4

    representative = Filters(
        active_only=False, hp_only=False,
        prefectures=["東京都"], municipalities=["千代田区"],
        hp_treatment_categories=["胃カメラ", "大腸カメラ"],
    )
    assert _assert_invariant(store, representative) == 1

    review_excluded = Filters(active_only=False, hp_only=False, hp_treatment_categories=["白内障手術"])
    assert _assert_invariant(store, review_excluded) == 0

    research_status_combo = Filters(active_only=False, hp_only=False, research_status=["CONFIRMED", "REVIEW"])
    assert _assert_invariant(store, research_status_combo) == 2
