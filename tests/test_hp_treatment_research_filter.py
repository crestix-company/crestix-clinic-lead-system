"""Phase7 Treatment Research filterのテスト。

2026-10-01: Research側がsidecarの契約を分離した（research/v4-base commit 90bb9fe）。
  - clinic_treatment_research_final（Treatment単位）: CONFIRMED/REVIEW/NOT_CONFIRMEDのみ。
    HP治療カテゴリfilterはCONFIRMED行だけを見る（契約変更なし）。
  - clinic_research_status（医院単位SSOT・新設）: DONE/FETCH_FAILEDのみ。Research Status filterは
    このテーブルだけを見る。行が無ければNOT_RESEARCHED。
ここで作る2テーブルはいずれもこのテスト専用の合成データであり、本番Research Workerの出力ではない
（mhlw_dry_run/TREATMENT_RESEARCH_CONTRACT.md参照）。
"""
import sqlite3

import pytest

from src.master.filters import Filters
from src.master.research_sidecar import (
    CLINIC_RESEARCH_STATUS_TABLE,
    RESEARCH_SIDECAR_ENV_VAR,
    RESEARCH_SIDECAR_TABLE,
    ResearchSidecarUnavailableError,
)
from src.master.samples import sample_records
from src.master.store import ClinicStore

ALL = Filters(active_only=False, hp_only=False)

# テスト専用の合成スキーマ。mhlw_dry_run/TREATMENT_RESEARCH_CONTRACT.md の契約どおりの列構成。
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

CREATE_STATUS_SQL = f"""
CREATE TABLE {CLINIC_RESEARCH_STATUS_TABLE}(
  clinic_id INTEGER PRIMARY KEY,
  research_status TEXT NOT NULL CHECK(research_status IN ('DONE','FETCH_FAILED')),
  candidate_count INTEGER NOT NULL DEFAULT 0,
  source_url TEXT NOT NULL DEFAULT '',
  final_url TEXT NOT NULL DEFAULT '',
  identity_verified INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  taxonomy_version TEXT NOT NULL DEFAULT '',
  evidence_engine_version TEXT NOT NULL DEFAULT '',
  manifest_id TEXT NOT NULL DEFAULT '',
  git_commit_sha TEXT NOT NULL DEFAULT '',
  researched_at TEXT NOT NULL DEFAULT ''
);
"""


def _make_sidecar(path, treatment_rows=(), status_rows=()):
    """テスト用合成sidecar（clinic_treatment_research_final + clinic_research_status）。
    実際のResearch Worker出力ではない。
    """
    conn = sqlite3.connect(path)
    try:
        conn.executescript(CREATE_TREATMENT_SQL + CREATE_STATUS_SQL)
        if treatment_rows:
            conn.executemany(
                f"INSERT INTO {RESEARCH_SIDECAR_TABLE}"
                "(clinic_id,treatment_category_id,treatment_category_name,research_status,evidence_engine_version,taxonomy_version,researched_at)"
                " VALUES(?,?,?,?,?,?,?)",
                treatment_rows,
            )
        if status_rows:
            conn.executemany(
                f"INSERT INTO {CLINIC_RESEARCH_STATUS_TABLE}"
                "(clinic_id,research_status,candidate_count,taxonomy_version,evidence_engine_version,manifest_id,git_commit_sha,researched_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                status_rows,
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
        store.count(Filters(active_only=False, hp_only=False, research_status=["DONE"]))


@pytest.fixture
def wired_sidecar(tmp_path, monkeypatch, store_with_clinics):
    """4医院に、Treatment単位(clinic_treatment_research_final)とは独立した
    医院単位(clinic_research_status)を割り当てる。

    - 青空内視鏡クリニック: DONE（胃カメラCONFIRMED）
    - 若葉眼科医院: DONE（白内障手術REVIEW＝候補はあるが未確定）
    - 日向皮膚科: FETCH_FAILED（ニキビ治療NOT_CONFIRMED＝過去の調査結果）
    - 月見内科: clinic_research_statusに行なし＝NOT_RESEARCHED、治療行もなし
    """
    store, clinics = store_with_clinics
    gastro = clinics["青空内視鏡クリニック"]
    ganka = clinics["若葉眼科医院"]
    hifuka = clinics["日向皮膚科"]
    sidecar_path = tmp_path / "treatment_research_final.sqlite3"
    _make_sidecar(
        str(sidecar_path),
        treatment_rows=[
            (gastro, "gastroscopy", "胃カメラ", "CONFIRMED", "engine-v1", "7A-v2", "2026-09-01T00:00:00Z"),
            (ganka, "cataract", "白内障手術", "REVIEW", "engine-v1", "7A-v2", "2026-09-01T00:00:00Z"),
            (hifuka, "acne", "ニキビ治療", "NOT_CONFIRMED", "engine-v1", "7A-v2", "2026-09-01T00:00:00Z"),
        ],
        status_rows=[
            (gastro, "DONE", 1, "7A-v2", "engine-v1", "manifest-1", "deadbeef", "2026-09-01T00:00:00Z"),
            (ganka, "DONE", 1, "7A-v2", "engine-v1", "manifest-1", "deadbeef", "2026-09-01T00:00:00Z"),
            (hifuka, "FETCH_FAILED", 0, "7A-v2", "engine-v1", "manifest-1", "deadbeef", "2026-09-01T00:00:00Z"),
            # 月見内科: 行なし -> NOT_RESEARCHED
        ],
    )
    monkeypatch.setenv(RESEARCH_SIDECAR_ENV_VAR, str(sidecar_path))
    return store, clinics


# ---- HP治療カテゴリ filter（CONFIRMED-only、clinic_treatment_research_final。契約変更なし） ----

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


def test_multiple_treatment_categories_per_clinic_no_duplicate_rows(tmp_path, monkeypatch, store_with_clinics):
    store, clinics = store_with_clinics
    gastro = clinics["青空内視鏡クリニック"]
    sidecar_path = tmp_path / "treatment_research_final_multi.sqlite3"
    _make_sidecar(
        str(sidecar_path),
        treatment_rows=[
            (gastro, "gastroscopy", "胃カメラ", "CONFIRMED", "engine-v1", "7A-v2", "2026-09-01T00:00:00Z"),
            (gastro, "colonoscopy", "大腸カメラ", "CONFIRMED", "engine-v1", "7A-v2", "2026-09-01T00:00:00Z"),
        ],
    )
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


# ---- Research Status filter（医院単位SSOT、clinic_research_status。新契約） ----

def test_research_status_filter_finds_each_status(wired_sidecar):
    store, clinics = wired_sidecar
    assert store.count(Filters(active_only=False, hp_only=False, research_status=["DONE"])) == 2
    assert store.count(Filters(active_only=False, hp_only=False, research_status=["FETCH_FAILED"])) == 1


def test_research_status_not_researched_means_no_row_in_clinic_research_status(wired_sidecar):
    store, clinics = wired_sidecar
    # 月見内科はclinic_research_statusに行が無いのでNOT_RESEARCHED。
    # (旧契約ではTreatment行の有無で判定していたため、ここが誤って0件になっていた)
    rows = store.query(Filters(active_only=False, hp_only=False, research_status=["NOT_RESEARCHED"]), limit=100)
    assert {r["id"] for r in rows} == {clinics["月見内科"]}


def test_research_status_or_within_dimension(wired_sidecar):
    store, clinics = wired_sidecar
    f = Filters(active_only=False, hp_only=False, research_status=["DONE", "FETCH_FAILED"])
    assert store.count(f) == 3


def test_done_with_zero_candidates_is_done_not_not_researched(tmp_path, monkeypatch, store_with_clinics):
    """仕様書 5-1: DONE + candidate_count=0 の医院はDONEに該当し、NOT_RESEARCHEDには該当しない。
    HP治療カテゴリfilterには、CONFIRMED行が存在しないため自然にヒットしない。
    """
    store, clinics = store_with_clinics
    ganka = clinics["若葉眼科医院"]
    sidecar_path = tmp_path / "treatment_research_final.sqlite3"
    _make_sidecar(
        str(sidecar_path),
        status_rows=[(ganka, "DONE", 0, "7A-v2", "engine-v1", "manifest-1", "deadbeef", "2026-09-01T00:00:00Z")],
    )
    monkeypatch.setenv(RESEARCH_SIDECAR_ENV_VAR, str(sidecar_path))

    assert ganka in {r["id"] for r in store.query(Filters(active_only=False, hp_only=False, research_status=["DONE"]), limit=100)}
    assert ganka not in {r["id"] for r in store.query(Filters(active_only=False, hp_only=False, research_status=["NOT_RESEARCHED"]), limit=100)}
    assert store.count(Filters(active_only=False, hp_only=False, hp_treatment_categories=["白内障手術"])) == 0


def test_fetch_failed_does_not_hide_past_confirmed_treatment_row(tmp_path, monkeypatch, store_with_clinics):
    """仕様書 5-2: clinic_research_status=FETCH_FAILEDでも、過去に成功したConfirmed Treatment行は
    HP治療カテゴリfilterの対象から除外しない(研究Status filterとHP治療カテゴリfilterは独立軸)。
    """
    store, clinics = store_with_clinics
    hifuka = clinics["日向皮膚科"]
    sidecar_path = tmp_path / "treatment_research_final.sqlite3"
    _make_sidecar(
        str(sidecar_path),
        treatment_rows=[(hifuka, "acne", "ニキビ治療", "CONFIRMED", "engine-v1", "7A-v2", "2026-08-01T00:00:00Z")],
        status_rows=[(hifuka, "FETCH_FAILED", 0, "7A-v2", "engine-v1", "manifest-1", "deadbeef", "2026-09-15T00:00:00Z")],
    )
    monkeypatch.setenv(RESEARCH_SIDECAR_ENV_VAR, str(sidecar_path))

    assert store.count(Filters(active_only=False, hp_only=False, research_status=["FETCH_FAILED"])) == 1
    assert store.count(Filters(active_only=False, hp_only=False, hp_treatment_categories=["ニキビ治療"])) == 1


def test_research_status_and_hp_treatment_filters_are_independent_axes(wired_sidecar):
    """仕様書 §8 item 7: 一方の条件が他方に影響しない。
    青空内視鏡クリニック(DONE・CONFIRMED胃カメラ)と若葉眼科医院(DONE・REVIEW白内障手術)は
    どちらもDONEだが、HP治療カテゴリ(胃カメラ)を同時に指定すると青空だけが残る。
    """
    store, clinics = wired_sidecar
    combo = Filters(active_only=False, hp_only=False, research_status=["DONE"], hp_treatment_categories=["胃カメラ"])
    assert store.count(combo) == 1
    rows = store.query(combo, limit=100)
    assert {r["id"] for r in rows} == {clinics["青空内視鏡クリニック"]}

    # DONEだけなら2件(青空+若葉)のまま、HP治療カテゴリ条件が無関係なクリニックを減らさない
    assert store.count(Filters(active_only=False, hp_only=False, research_status=["DONE"])) == 2
