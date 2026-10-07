"""営業対象scope（既存営業リスト13,970件 / 全国Clinic Master162,258件）の切り替え（synthetic fixture）。

Production DBは使わない。first_seen_atのcutoffは直接SQL UPDATEで境界値を作って検証する。
"""
from datetime import datetime, timedelta, timezone

import pytest
from streamlit.testing.v1 import AppTest

from src.master.comdesk import COMDESK_HEADERS
from src.master.filters import Filters, where
from src.master.samples import sample_records
from src.master.scope import LEGACY_PRE_NATIONAL_CUTOFF, SCOPE_ALL, SCOPE_LEGACY_PRE_NATIONAL
from src.master.store import ClinicStore
from src.utils.config import ROOT

ALL = dict(active_only=False, hp_only=False)
CUTOFF = datetime.fromisoformat(LEGACY_PRE_NATIONAL_CUTOFF)


def _clinic(i, name):
    row = dict(sample_records()[0])
    row.update({"clinic_id": f"scope-{i}", "clinic_name": name, "phone": f"03-9999-{i:04d}",
                "address": f"東京都千代田区scope町{i}-1-1"})
    return row


def _set_first_seen(store, clinic_id, iso_value):
    with store.connect() as c:
        c.execute("UPDATE clinics SET first_seen_at=? WHERE id=?", (iso_value, clinic_id))


# ---- SQL: scope clause -----------------------------------------------------
def test_all_scope_adds_no_first_seen_clause():
    sql, args = where(Filters(**ALL, scope=SCOPE_ALL))
    assert "first_seen_at" not in sql
    assert LEGACY_PRE_NATIONAL_CUTOFF not in args


def test_legacy_scope_adds_first_seen_before_cutoff():
    sql, args = where(Filters(**ALL, scope=SCOPE_LEGACY_PRE_NATIONAL))
    assert "first_seen_at<?" in sql
    assert LEGACY_PRE_NATIONAL_CUTOFF in args


def test_filters_default_scope_is_all():
    assert Filters().scope == SCOPE_ALL


def test_unknown_scope_is_rejected():
    with pytest.raises(ValueError):
        where(Filters(**ALL, scope="BOGUS"))


# ---- Boundary ---------------------------------------------------------------
@pytest.fixture
def boundary_store(tmp_path):
    store = ClinicStore(tmp_path / "boundary.db")
    store.import_master([_clinic(1, "前1マイクロ秒"), _clinic(2, "境界ちょうど"), _clinic(3, "後1マイクロ秒")])
    ids = {r["clinic_name"]: r["id"] for r in store.query(Filters(**ALL), limit=100)}
    _set_first_seen(store, ids["前1マイクロ秒"], (CUTOFF - timedelta(microseconds=1)).isoformat())
    _set_first_seen(store, ids["境界ちょうど"], CUTOFF.isoformat())
    _set_first_seen(store, ids["後1マイクロ秒"], (CUTOFF + timedelta(microseconds=1)).isoformat())
    return store, ids


def test_boundary_before_cutoff_is_legacy_hit(boundary_store):
    store, ids = boundary_store
    names = {r["clinic_name"] for r in store.query(Filters(**ALL, scope=SCOPE_LEGACY_PRE_NATIONAL), limit=100)}
    assert "前1マイクロ秒" in names


def test_boundary_exact_cutoff_is_legacy_miss(boundary_store):
    store, ids = boundary_store
    names = {r["clinic_name"] for r in store.query(Filters(**ALL, scope=SCOPE_LEGACY_PRE_NATIONAL), limit=100)}
    assert "境界ちょうど" not in names


def test_boundary_after_cutoff_is_legacy_miss(boundary_store):
    store, ids = boundary_store
    names = {r["clinic_name"] for r in store.query(Filters(**ALL, scope=SCOPE_LEGACY_PRE_NATIONAL), limit=100)}
    assert "後1マイクロ秒" not in names


def test_boundary_all_scope_sees_all_three(boundary_store):
    store, ids = boundary_store
    names = {r["clinic_name"] for r in store.query(Filters(**ALL, scope=SCOPE_ALL), limit=100)}
    assert names == {"前1マイクロ秒", "境界ちょうど", "後1マイクロ秒"}


# ---- Parity across query/count/funnel/export --------------------------------
@pytest.fixture
def parity_store(tmp_path):
    store = ClinicStore(tmp_path / "parity.db")
    names = [f"parity{i}" for i in range(6)]
    store.import_master([_clinic(i, n) for i, n in enumerate(names)])
    ids = {r["clinic_name"]: r["id"] for r in store.query(Filters(**ALL), limit=100)}
    # 3件をlegacy側（cutoffより前）、3件をnational append側（cutoff以降）にする。
    for n in names[:3]:
        _set_first_seen(store, ids[n], (CUTOFF - timedelta(days=1)).isoformat())
    for n in names[3:]:
        _set_first_seen(store, ids[n], (CUTOFF + timedelta(days=1)).isoformat())
    return store, ids, names


def test_query_count_funnel_export_share_the_same_legacy_scope(parity_store):
    store, ids, names = parity_store
    f = Filters(**ALL, scope=SCOPE_LEGACY_PRE_NATIONAL)
    queried = {r["clinic_name"] for r in store.query(f, limit=100)}
    assert queried == set(names[:3])
    assert store.count(f) == 3
    funnel = store.funnel(f)
    assert funnel[-1][1] == 3
    files = store.export(f)
    rows = files["final_comdesk_import.csv"].decode("utf-8-sig").splitlines()
    assert len(rows) - 1 == 3  # header + 3 rows


def test_query_count_funnel_export_share_the_same_all_scope(parity_store):
    store, ids, names = parity_store
    f = Filters(**ALL, scope=SCOPE_ALL)
    queried = {r["clinic_name"] for r in store.query(f, limit=100)}
    assert queried == set(names)
    assert store.count(f) == 6
    funnel = store.funnel(f)
    assert funnel[-1][1] == 6
    files = store.export(f)
    rows = files["final_comdesk_import.csv"].decode("utf-8-sig").splitlines()
    assert len(rows) - 1 == 6


def test_comdesk_export_keeps_28_columns_under_scope(parity_store):
    store, ids, names = parity_store
    files = store.export(Filters(**ALL, scope=SCOPE_LEGACY_PRE_NATIONAL))
    header = files["final_comdesk_import.csv"].decode("utf-8-sig").splitlines()[0].split(",")
    assert header == COMDESK_HEADERS
    assert len(header) == 28


# ---- Department multi-select OR + scope AND ----------------------------------
def _dept_clinic(i, name, departments, before_cutoff):
    row = dict(sample_records()[0])
    row.update({"clinic_id": f"scope-dept-{i}", "clinic_name": name, "phone": f"03-8888-{i:04d}",
                "address": f"東京都千代田区scopedept町{i}-1-1", "departments": departments})
    return row


@pytest.fixture
def dept_scope_store(tmp_path):
    store = ClinicStore(tmp_path / "dept_scope.db")
    specs = [
        (1, "あ眼科legacy", "眼", True), (2, "い皮膚科legacy", "皮", True),
        (3, "う眼科national", "眼", False), (4, "え皮膚科national", "皮", False),
    ]
    store.import_master([_dept_clinic(i, n, d, before) for i, n, d, before in specs])
    ids = {r["clinic_name"]: r["id"] for r in store.query(Filters(**ALL), limit=100)}
    for i, n, d, before in specs:
        offset = -timedelta(days=1) if before else timedelta(days=1)
        _set_first_seen(store, ids[n], (CUTOFF + offset).isoformat())
    return store


def test_department_or_is_preserved_under_legacy_scope(dept_scope_store):
    store = dept_scope_store
    names = sorted(r["clinic_name"] for r in store.query(
        Filters(**ALL, scope=SCOPE_LEGACY_PRE_NATIONAL, departments=["眼科", "皮膚科"]), limit=100))
    # scopeで全国append分は除外され、legacy側の2件（OR）だけが残る。
    assert names == ["あ眼科legacy", "い皮膚科legacy"]


def test_department_or_without_scope_restriction_sees_all_four(dept_scope_store):
    store = dept_scope_store
    names = sorted(r["clinic_name"] for r in store.query(
        Filters(**ALL, scope=SCOPE_ALL, departments=["眼科", "皮膚科"]), limit=100))
    assert names == ["あ眼科legacy", "い皮膚科legacy", "う眼科national", "え皮膚科national"]


# ---- Pair filter (PR#21) AND legacy scope, no Cartesian leak -----------------
def _pair_clinic(i, name, departments, before_cutoff):
    row = dict(sample_records()[0])
    row.update({"clinic_id": f"scope-pair-{i}", "clinic_name": name, "phone": f"03-7777-{i:04d}",
                "address": f"東京都千代田区scopepair町{i}-1-1", "departments": departments,
                "facility_type": "診療所", "medical_type": "医科"})
    return row


def _evidence(category, keyword, source="HOME_MENU"):
    return {"category": category, "keyword": keyword, "source": source, "confidence": .98}


def test_pair_filter_ands_with_legacy_scope_without_cartesian_leak(tmp_path):
    store = ClinicStore(tmp_path / "pair_scope.db")
    specs = [
        (1, "両方一致legacy", "皮 美容整形外科", True),
        (2, "両方一致national", "皮 美容整形外科", False),
        (3, "片方だけlegacy", "皮", True),
    ]
    store.import_master([_pair_clinic(i, n, d, before) for i, n, d, before in specs])
    ids = {r["clinic_name"]: r["id"] for r in store.query(Filters(**ALL), limit=100)}
    for i, n, d, before in specs:
        offset = -timedelta(days=1) if before else timedelta(days=1)
        _set_first_seen(store, ids[n], (CUTOFF + offset).isoformat())
    store.save_research(ids["両方一致legacy"], {
        "hp_status": "VERIFIED", "hp_url": "https://one.example/",
        "treatment_categories": ["日帰り手術"], "treatment_evidence": [_evidence("日帰り手術", "眼瞼下垂手術")],
    })
    store.save_research(ids["両方一致national"], {
        "hp_status": "VERIFIED", "hp_url": "https://two.example/",
        "treatment_categories": ["日帰り手術"], "treatment_evidence": [_evidence("日帰り手術", "眼瞼下垂手術")],
    })
    store.save_research(ids["片方だけlegacy"], {
        "hp_status": "VERIFIED", "hp_url": "https://three.example/",
        "treatment_categories": [], "treatment_evidence": [],
    })

    overlap = [("皮膚科", "眼瞼下垂手術"), ("美容整形外科", "眼瞼下垂手術")]
    result = store.query(Filters(**ALL, scope=SCOPE_LEGACY_PRE_NATIONAL, sales_pairs=overlap), limit=100)
    # national appendの同条件医院はscopeで除外される（Cartesian leakでも復活しない）。
    assert [r["clinic_name"] for r in result] == ["両方一致legacy"]

    result_all_scope = store.query(Filters(**ALL, scope=SCOPE_ALL, sales_pairs=overlap), limit=100)
    assert sorted(r["clinic_name"] for r in result_all_scope) == ["両方一致legacy", "両方一致national"]

    cartesian = [("皮膚科", "ポテンツァ"), ("消化器内科", "胃カメラ")]
    assert store.query(Filters(**ALL, scope=SCOPE_LEGACY_PRE_NATIONAL, sales_pairs=cartesian), limit=100) == []


# ---- UI defaults --------------------------------------------------------------
def _app_at(tmp_path, monkeypatch, db_path):
    # See tests/test_hp_abc_ui_export.py:_app -- pin READ backend to this isolated fixture.
    monkeypatch.setenv("CLINIC_DATA_BACKEND", "sqlite")
    monkeypatch.setenv("CLINIC_DB_PATH", str(db_path))
    monkeypatch.setenv("CLINIC_DEMO_DB_PATH", str(tmp_path / "demo.db"))
    return AppTest.from_file(str(ROOT / "app_v2.py"), default_timeout=30).run()


def test_simple_sales_screen_defaults_to_all_scope(tmp_path, monkeypatch):
    # 2026-10-05: 正式Sales Tier分類(SSOT)のcohortを旧13,970件のlegacy scopeへ
    # 誤って取りこぼさないよう、簡易営業UIの既定値だけをSCOPE_ALLへ変更した
    # （選択肢の並び順はUI/UXを変えないため元のまま）。
    store = ClinicStore(tmp_path / "ui.db")
    at = _app_at(tmp_path, monkeypatch, store.path)
    at.session_state["navigation"] = "営業対象・出力"
    at.run()
    scope_select = next(s for s in at.selectbox if s.label == "対象データ")
    assert scope_select.value == SCOPE_ALL
    assert scope_select.options == ["既存営業リスト", "全国Clinic Master"]


def test_detail_sales_filter_screen_defaults_to_legacy_scope(tmp_path, monkeypatch):
    store = ClinicStore(tmp_path / "ui2.db")
    at = _app_at(tmp_path, monkeypatch, store.path)
    at.session_state["navigation"] = "詳細設定"
    at.run()
    sel = next(s for s in at.selectbox if s.options and s.options[0] == "マスター管理")
    sel.select("営業対象フィルター（詳細）").run()
    scope_select = next(s for s in at.selectbox if s.label == "対象データ")
    assert scope_select.value == SCOPE_LEGACY_PRE_NATIONAL


def test_research_detail_screen_default_is_unchanged_all_scope(tmp_path, monkeypatch):
    # Filtersの汎用dataclass自体のdefaultはALLのまま。research画面はscopeを明示していないので
    # 従来どおり全国母集団のまま、というregressionなしを確認する。
    store = ClinicStore(tmp_path / "ui3.db")
    at = _app_at(tmp_path, monkeypatch, store.path)
    at.session_state["navigation"] = "詳細設定"
    at.run()
    sel = next(s for s in at.selectbox if s.options and s.options[0] == "マスター管理")
    sel.select("自動情報収集（詳細）").run()
    scope_select = next(s for s in at.selectbox if s.label == "対象データ")
    assert scope_select.value == SCOPE_ALL
