"""本番切替前final sidecarの回帰テスト。"""
import csv
import sqlite3
from pathlib import Path

import pytest

from src.master.filters import Filters, clauses
from src.master.store import ClinicStore, MhlwSidecarUnavailableError, mhlw_official_department_options
import src.master.store as store_module

ROOT = Path(__file__).resolve().parents[1]
FINAL = ROOT / "mhlw_dry_run" / "clinic_mhlw_departments_final.sqlite3"
DB = ROOT / "data" / "clinics.sqlite3"
# 2026-09-30: ISリーダー正式回答によりREVIEW 37件をALIASへ確定(mhlw_dry_run/crestix_department_mapping.py)。
# 旧値(ISリーダー回答前): 消化器内科1331,眼科952,糖尿病内科454,泌尿器科402,循環器内科1291,皮膚科1784,歯科32,美容整形外科154,産婦人科293。
# sidecar自体(clinic_mhlw_departments_final)にはclinicsのexclude_reason/名前は無関係なので、この値のまま。
EXPECTED_CATEGORIES = {
    "消化器内科": 1582, "眼科": 953, "糖尿病内科": 627, "泌尿器科": 434,
    "循環器内科": 1329, "皮膚科": 1910, "歯科": 39, "美容整形外科": 395, "産婦人科": 603,
}
# 2026-09-30(同日追記): fixed_export.pyだけにあった病院・センター除外をfilters.py側(where())にも適用し、
# UI count = CSV rowsを一致させた。store.count()経由の値はこの除外を反映して上のsidecar直値より少ない。
EXPECTED_CATEGORIES_VIA_STORE = {
    "消化器内科": 1574, "眼科": 947, "糖尿病内科": 624, "泌尿器科": 429,
    "循環器内科": 1317, "皮膚科": 1900, "歯科": 38, "美容整形外科": 392, "産婦人科": 590,
}

missing = pytest.mark.skipif(not (FINAL.exists() and DB.exists()), reason="final sidecar/Production DB not generated")


@missing
def test_final_sidecar_has_9830_unique_clinics_and_required_columns():
    with sqlite3.connect(f"file:{FINAL}?mode=ro", uri=True) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(clinic_mhlw_departments_final)")}
        assert {"clinic_id", "mhlw_facility_id", "mhlw_department_code", "mhlw_department_name",
                "crestix_department", "match_method", "match_confidence", "identity_status", "source_date"} <= cols
        assert c.execute("SELECT count(DISTINCT clinic_id) FROM clinic_mhlw_departments_final").fetchone()[0] == 9830


@missing
def test_final_sidecar_crestix_filter_counts_and_or_no_duplicates():
    with sqlite3.connect(f"file:{FINAL}?mode=ro", uri=True) as c:
        for category, expected in EXPECTED_CATEGORIES.items():
            actual = c.execute("SELECT count(DISTINCT clinic_id) FROM clinic_mhlw_departments_final WHERE crestix_department=?", (category,)).fetchone()[0]
            assert actual == expected
        ids = [r[0] for r in c.execute("SELECT DISTINCT clinic_id FROM clinic_mhlw_departments_final WHERE crestix_department IN (?,?)",
            ("消化器内科", "循環器内科"))]
        assert len(ids) == len(set(ids))


@missing
def test_existing_filter_sql_can_dry_run_against_final_sidecar(monkeypatch):
    monkeypatch.setattr(store_module, "MHLW_SIDECAR_PATH", FINAL)
    store = ClinicStore(str(DB))
    for category, expected in EXPECTED_CATEGORIES_VIA_STORE.items():
        assert store.count(Filters(active_only=False, hp_only=False, crestix_sales_departments=[category])) == expected
    both = store.count(Filters(active_only=False, hp_only=False,
        crestix_sales_departments=["消化器内科", "循環器内科"]))
    # store.count()と同じ病院・センター除外をJOIN側にも適用し、独立した計算経路で照合する。
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as c:
        c.execute("ATTACH DATABASE ? AS finaldb", (str(FINAL),))
        expected_union = c.execute(
            "SELECT count(DISTINCT d.clinic_id) FROM finaldb.clinic_mhlw_departments_final d "
            "JOIN clinics cl ON cl.id=d.clinic_id "
            "WHERE d.crestix_department IN (?,?) AND NOT ("
            "cl.exclude_reason IN ('hospital','center') "
            "OR COALESCE(json_extract(cl.effective_json,'$.facility_type'),'')='病院' "
            "OR cl.clinic_name LIKE '%病院%' OR cl.clinic_name LIKE '%センター%')",
            ("消化器内科", "循環器内科")).fetchone()[0]
    assert both == expected_union


@missing
def test_official_department_options_are_loaded_from_final_sidecar():
    options = mhlw_official_department_options()
    assert len(options) == 227
    assert options == sorted(options)
    assert {"内科", "心療内科", "循環器内科", "消化器内科", "糖尿病内科"} <= set(options)


@missing
def test_official_names_are_exact_and_not_substring_matches():
    store = ClinicStore(str(DB))
    shinzou = store.count(Filters(active_only=False, hp_only=False, mhlw_official_departments=["心療内科"]))
    naika = store.count(Filters(active_only=False, hp_only=False, mhlw_official_departments=["内科"]))
    # 2026-09-30: 病院・センター除外をfilters.pyに適用したため、旧値(813/5356)より少ない。
    assert shinzou == 809
    assert naika == 5317


@missing
def test_official_and_crestix_axes_have_or_within_and_between_axes():
    store = ClinicStore(str(DB))
    # 2026-09-30: 病院・センター除外をfilters.pyに適用したため、旧値(1249/2797/1331)より少ない。
    assert store.count(Filters(active_only=False, hp_only=False,
        mhlw_official_departments=["心療内科", "糖尿病内科"])) == 1244
    assert store.count(Filters(active_only=False, hp_only=False,
        crestix_sales_departments=["眼科", "皮膚科"])) == 2784
    assert store.count(Filters(active_only=False, hp_only=False,
        mhlw_official_departments=["消化器内科"], crestix_sales_departments=["消化器内科"])) == 1323


@missing
def test_official_and_crestix_filters_return_unique_final_matched_ids_only():
    store = ClinicStore(str(DB))
    f = Filters(active_only=False, hp_only=False, mhlw_official_departments=["内科", "心療内科"],
        crestix_sales_departments=["循環器内科", "消化器内科"])
    rows = store.query(f, limit=100000)
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids))
    with sqlite3.connect(f"file:{FINAL}?mode=ro", uri=True) as c:
        final_ids = {r[0] for r in c.execute("SELECT DISTINCT clinic_id FROM clinic_mhlw_departments_final")}
    assert set(ids) <= final_ids


def test_missing_sidecar_keeps_legacy_filters_and_gives_clear_mhlw_error(monkeypatch, tmp_path):
    missing_path = tmp_path / "missing.sqlite3"
    monkeypatch.setattr(store_module, "MHLW_SIDECAR_PATH", missing_path)
    # CIにはProduction DBを置かない。空の一時DBでも既存filterが例外なく動くことを確認する。
    store = ClinicStore(str(tmp_path / "clinics.sqlite3"))
    assert store.count(Filters(active_only=False, hp_only=False)) == 0
    with pytest.raises(MhlwSidecarUnavailableError, match="sidecar"):
        store.count(Filters(active_only=False, hp_only=False, mhlw_official_departments=["内科"]))


@missing
def test_hp_unresearched_clinics_are_filterable_from_final_sidecar():
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as c:
        c.execute("ATTACH DATABASE ? AS finaldb", (str(FINAL),))
        count = c.execute("SELECT count(DISTINCT c.id) FROM clinics c JOIN finaldb.clinic_mhlw_departments_final d "
            "ON d.clinic_id=c.id WHERE c.hp_status='UNRESEARCHED' AND d.crestix_department<>''").fetchone()[0]
        assert count > 0


@missing
def test_legacy_department_and_treatment_filters_do_not_use_final_sidecar():
    for f in (Filters(departments=["内科"]), Filters(treatments=["内視鏡検査"])):
        parts = clauses(f)
        sql = " ".join(part[1] for part in parts)
        assert "mhlwdb" not in sql
    # 2026-09-30: 病院・センター除外をfilters.pyに適用したため、legacy全件13970より少ない。
    assert ClinicStore(str(DB)).count(Filters(active_only=False, hp_only=False)) == 13254


@missing
def test_versioned_promotion_decisions_have_expected_audited_counts():
    path = ROOT / "mhlw_dry_run" / "final_hp_promotion_decisions.csv"
    rows = list(csv.DictReader(path.open(encoding="utf-8", newline="")))
    promoted = [row for row in rows if row["promotion_allowed"] == "true"]
    assert len(rows) == 168
    assert len({row["clinic_id"] for row in rows}) == 168
    assert len({(row["clinic_id"], row["mhlw_facility_id"]) for row in rows}) == 168
    assert len(promoted) == 55
    assert sum(row["final_identity_status"] == "HP_IDENTITY_CONFIRMED" for row in promoted) == 3
    assert sum(row["final_identity_status"] == "HP_RENAME_CONFIRMED" for row in promoted) == 52
