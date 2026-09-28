"""normalized departmentのruntime SSOTをclinics.departments_jsonへ統一したこと（Option A）の回帰test。

reprojectがdepartments_json列だけを更新し、effective_json.normalized_departmentsが
古いまま残っても、department filter・matched_pairs・sales_pairs表示が一致することを検証する。
Production DBは使わない。synthetic fixtureのみ。
"""
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from src.master.filters import Filters
from src.master.samples import sample_records
from src.master.scope import LEGACY_PRE_NATIONAL_CUTOFF, SCOPE_LEGACY_PRE_NATIONAL
from src.master.store import ClinicStore

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.reproject_legacy_departments import run_apply  # noqa: E402

ALL = dict(active_only=False, hp_only=False)
CUTOFF_DT = datetime.fromisoformat(LEGACY_PRE_NATIONAL_CUTOFF)


def _clinic(i, name, departments=""):
    row = dict(sample_records()[0])
    row.update({"clinic_id": f"ssot-{i}", "clinic_name": name, "phone": f"03-5555-{i:04d}",
                "address": f"東京都千代田区ssot町{i}-1-1", "departments": departments,
                "facility_type": "診療所", "medical_type": "医科"})
    return row


def _evidence(category, keyword, source="HOME_MENU"):
    return {"category": category, "keyword": keyword, "source": source, "confidence": .98}


def _force_stale_departments(store, clinic_id, old_departments):
    """departments_json列とeffective_json.normalized_departmentsを両方、同じ古い値へ強制する。

    _project()は常に両方を同じ値で書くため、これは「reproject前」の状態（両者が一致した
    古い値のまま）を再現するためのtest専用ヘルパー。
    """
    with store.connect() as c:
        row = c.execute("SELECT effective_json FROM clinics WHERE id=?", (clinic_id,)).fetchone()
        effective = json.loads(row[0])
        effective["normalized_departments"] = old_departments
        c.execute(
            "UPDATE clinics SET departments_json=?,effective_json=? WHERE id=?",
            (json.dumps(old_departments, ensure_ascii=False), json.dumps(effective, ensure_ascii=False), clinic_id),
        )


def _set_first_seen(store, clinic_id, iso_value):
    with store.connect() as c:
        c.execute("UPDATE clinics SET first_seen_at=? WHERE id=?", (iso_value, clinic_id))


def _force_departments_json_only(store, clinic_id, new_departments):
    """reprojectのrun_apply()と同じ操作：departments_json列だけを更新し、effective_jsonは触らない。"""
    with store.connect() as c:
        c.execute(
            "UPDATE clinics SET departments_json=? WHERE id=?",
            (json.dumps(new_departments, ensure_ascii=False), clinic_id),
        )


# ---- 6. department filterはdepartments_jsonだけを見る（元々そう） ------------------
def test_department_filter_hits_on_departments_json_even_if_effective_json_is_empty(tmp_path):
    store = ClinicStore(tmp_path / "dept.db")
    store.import_master([_clinic(1, "美容外科clinic", "")])  # raw空 → departments_json=[]
    cid = store.query(Filters(**ALL))[0]["id"]
    _force_stale_departments(store, cid, [])  # 明示的に古い状態にそろえる
    _force_departments_json_only(store, cid, ["美容外科"])  # reproject後を模擬：departments_jsonだけ更新

    names = [r["clinic_name"] for r in store.query(Filters(**ALL, departments=["美容外科"]))]
    assert names == ["美容外科clinic"]


# ---- 6/7. matched_pairs・sales_pairsはeffective_jsonが古くてもdepartments_jsonに追随する -------
@pytest.fixture
def stale_pair_store(tmp_path):
    store = ClinicStore(tmp_path / "pair_ssot.db")
    store.import_master([_clinic(1, "美容整形外科clinic", "")])  # raw空 → 最初はdepartments_json=[]
    cid = store.query(Filters(**ALL))[0]["id"]
    store.save_research(cid, {
        "hp_status": "VERIFIED", "hp_url": "https://ssot.example/",
        "treatment_categories": ["日帰り手術"],
        "treatment_evidence": [_evidence("日帰り手術", "眼瞼下垂手術")],
    })
    # ここまでは_project()がdepartments_json/effective_json双方を[]のまま一致させている。
    _force_departments_json_only(store, cid, ["美容整形外科"])  # reprojectが行うのはこの1列だけの更新
    return store, cid


def test_matched_pairs_follows_departments_json_when_effective_json_is_stale(stale_pair_store):
    store, cid = stale_pair_store
    with store.connect() as c:
        effective = json.loads(c.execute("SELECT effective_json FROM clinics WHERE id=?", (cid,)).fetchone()[0])
    assert effective.get("normalized_departments") == []  # effective_json側はreproject後も古いまま

    record = store.get(cid)
    assert record["normalized_departments"] == ["美容整形外科"]  # _get()はdepartments_json列を見る

    results = store.query(Filters(**ALL, sales_pairs=[("美容整形外科", "眼瞼下垂手術")]))
    assert len(results) == 1
    assert results[0]["id"] == cid
    assert [(p["department"], p["treatment"]) for p in results[0]["matched_pairs"]] == [("美容整形外科", "眼瞼下垂手術")]


def test_sales_pairs_department_filter_and_matched_pairs_agree_under_staleness(stale_pair_store):
    store, cid = stale_pair_store
    dept_filtered = {r["id"] for r in store.query(Filters(**ALL, departments=["美容整形外科"]))}
    pair_filtered = {r["id"] for r in store.query(Filters(**ALL, sales_pairs=[("美容整形外科", "眼瞼下垂手術")]))}
    assert dept_filtered == pair_filtered == {cid}


# ---- 7. Cartesian leakは維持（staleでも復活しない） --------------------------------
def test_cartesian_leak_not_reintroduced_when_effective_json_is_stale(tmp_path):
    store = ClinicStore(tmp_path / "cartesian_ssot.db")
    store.import_master([
        _clinic(1, "皮膚科だけ胃", ""),
        _clinic(2, "消化器だけポテンツァ", ""),
    ])
    ids = {r["clinic_name"]: r["id"] for r in store.query(Filters(**ALL))}
    store.save_research(ids["皮膚科だけ胃"], {
        "hp_status": "VERIFIED", "hp_url": "https://one.example/",
        "treatment_categories": ["内視鏡"], "treatment_evidence": [_evidence("内視鏡", "胃カメラ")],
    })
    store.save_research(ids["消化器だけポテンツァ"], {
        "hp_status": "VERIFIED", "hp_url": "https://two.example/",
        "treatment_categories": ["ニキビ・ニキビ跡"], "treatment_evidence": [_evidence("ニキビ・ニキビ跡", "ポテンツァ")],
    })
    # 片方は皮膚科、もう片方は消化器内科（departments_jsonだけ後からreproject的に設定）。
    _force_departments_json_only(store, ids["皮膚科だけ胃"], ["皮膚科"])
    _force_departments_json_only(store, ids["消化器だけポテンツァ"], ["消化器内科"])

    cartesian = [("皮膚科", "ポテンツァ"), ("消化器内科", "胃カメラ")]
    assert store.query(Filters(**ALL, sales_pairs=cartesian)) == []


# ---- Legacy scope AND pair、staleでも一致 -------------------------------------------
def test_legacy_scope_and_pair_consistent_with_stale_effective_json(tmp_path):
    store = ClinicStore(tmp_path / "scope_pair_ssot.db")
    store.import_master([
        _clinic(1, "legacy美容整形", ""),
        _clinic(2, "national美容整形", ""),
    ])
    ids = {r["clinic_name"]: r["id"] for r in store.query(Filters(**ALL))}
    _set_first_seen(store, ids["legacy美容整形"], (CUTOFF_DT - timedelta(days=1)).isoformat())
    _set_first_seen(store, ids["national美容整形"], (CUTOFF_DT + timedelta(days=1)).isoformat())
    for name in ids:
        store.save_research(ids[name], {
            "hp_status": "VERIFIED", "hp_url": f"https://{name}.example/",
            "treatment_categories": ["日帰り手術"], "treatment_evidence": [_evidence("日帰り手術", "眼瞼下垂手術")],
        })
        _force_departments_json_only(store, ids[name], ["美容整形外科"])

    result = store.query(Filters(**ALL, scope=SCOPE_LEGACY_PRE_NATIONAL, sales_pairs=[("美容整形外科", "眼瞼下垂手術")]))
    assert [r["clinic_name"] for r in result] == ["legacy美容整形"]
    assert result[0]["matched_pairs"]


# ---- End-to-end: 実際のreproject run_applyを通した後もmatched_pairsが一致する ----------
def test_reproject_apply_keeps_matched_pairs_consistent_with_fresh_departments_json(tmp_path):
    store = ClinicStore(tmp_path / "reproject_ssot.db")
    store.import_master([_clinic(1, "reproject美容整形", "美容整形外科")])  # raw非空、現行normalizerと一致する状態で開始
    cid = store.query(Filters(**ALL))[0]["id"]
    _set_first_seen(store, cid, (CUTOFF_DT - timedelta(days=1)).isoformat())
    store.save_research(cid, {
        "hp_status": "VERIFIED", "hp_url": "https://reproject.example/",
        "treatment_categories": ["日帰り手術"], "treatment_evidence": [_evidence("日帰り手術", "眼瞼下垂手術")],
    })
    # ここで人為的に「古いnormalizerで計算されたstale値」を再現する：
    # departments_json・effective_json双方を同じ古い値（[]）へ戻す。
    _force_stale_departments(store, cid, [])

    classification = run_apply(store.path, tmp_path / "out", expected_candidates=1)
    assert classification.candidate_updates == 1

    with store.connect() as c:
        effective = json.loads(c.execute("SELECT effective_json FROM clinics WHERE id=?", (cid,)).fetchone()[0])
    assert effective.get("normalized_departments") == []  # apply後もeffective_json側は古いまま（想定どおり）

    record = store.get(cid)
    assert record["normalized_departments"] == ["美容整形外科"]  # departments_json列（reproject後の新しい値）を反映

    result = store.query(Filters(**ALL, sales_pairs=[("美容整形外科", "眼瞼下垂手術")]))
    assert len(result) == 1 and result[0]["id"] == cid
    assert [(p["department"], p["treatment"]) for p in result[0]["matched_pairs"]] == [("美容整形外科", "眼瞼下垂手術")]

    # idempotency: 同じ状態でもう一度分類すればcandidateは0。
    conn = sqlite3.connect(store.path)
    try:
        from scripts.reproject_legacy_departments import classify, fetch_legacy_rows
        rows = fetch_legacy_rows(conn)
    finally:
        conn.close()
    assert classify(rows).candidate_updates == 0
