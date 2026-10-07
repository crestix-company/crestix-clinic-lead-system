"""Stage2 1.A/1.B: UI件数=Comdesk出力件数（UUID有無で差が出ない）、Treatment調査状態の
4分類表示（取得済み/調査済み・該当カテゴリなし/HP取得失敗/Treatment未調査）を確認する。
HP ABC判定（hp_rank）はcandidate v1/v2に未接続、既存rank_hp()の出力をそのまま使う。
sidecarはテスト専用の合成データ（本番artifactは使わない）。
"""
import sqlite3

from streamlit.testing.v1 import AppTest

from src.io.input_loader import load_table
from src.master.comdesk import COMDESK_HEADERS
from src.master.filters import Filters
from src.master.hp_effective_rank import HP_BATCH_ENV_VAR
from src.master.research_sidecar import (
    CLINIC_RESEARCH_STATUS_TABLE, RESEARCH_SIDECAR_ENV_VAR, RESEARCH_SIDECAR_TABLE,
)
from src.master.samples import sample_records
from src.master.scope import SCOPE_ALL
from src.master.store import ClinicStore
from src.utils.config import ROOT

CREATE_SQL = f"""
CREATE TABLE {RESEARCH_SIDECAR_TABLE}(
  clinic_id INTEGER NOT NULL,
  treatment_category_id TEXT NOT NULL,
  treatment_category_name TEXT NOT NULL,
  research_status TEXT NOT NULL CHECK(research_status IN ('CONFIRMED','REVIEW','NOT_CONFIRMED')),
  PRIMARY KEY(clinic_id, treatment_category_id)
);
CREATE TABLE {CLINIC_RESEARCH_STATUS_TABLE}(
  clinic_id INTEGER PRIMARY KEY,
  research_status TEXT NOT NULL CHECK(research_status IN ('DONE','FETCH_FAILED'))
);
"""


def _make_sidecar(path, treatment_rows=(), status_rows=()):
    conn = sqlite3.connect(path)
    try:
        conn.executescript(CREATE_SQL)
        if treatment_rows:
            conn.executemany(
                f"INSERT INTO {RESEARCH_SIDECAR_TABLE}(clinic_id,treatment_category_id,treatment_category_name,research_status) VALUES(?,?,?,?)",
                treatment_rows,
            )
        if status_rows:
            conn.executemany(f"INSERT INTO {CLINIC_RESEARCH_STATUS_TABLE}(clinic_id,research_status) VALUES(?,?)", status_rows)
        conn.commit()
    finally:
        conn.close()


def _setup_store(tmp_path, overrides):
    """overrides: {clinic_name: (hp_rank, uuid)}"""
    store = ClinicStore(tmp_path / "clinics.db")
    store.import_master(sample_records())
    clinics = {r["clinic_name"]: r["id"] for r in store.query(Filters(active_only=False, hp_only=False), limit=100)}
    with store.connect() as c:
        for name, (rank, uuid) in overrides.items():
            c.execute("UPDATE clinics SET hp_rank=?, uuid=? WHERE id=?", (rank, uuid, clinics[name]))
    return store, clinics


def _make_hp_batch(path, rows):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE hp_research_batch_results(clinic_id INTEGER PRIMARY KEY, fetch_status TEXT, hp_abc_candidate TEXT, treatment_categories TEXT NOT NULL DEFAULT '[]', hp_url TEXT NOT NULL DEFAULT '', final_url TEXT NOT NULL DEFAULT '')")
        normalized = [tuple(row) + ('[]',) if len(row) == 3 else tuple(row) for row in rows]
        conn.executemany("INSERT INTO hp_research_batch_results(clinic_id,fetch_status,hp_abc_candidate,treatment_categories) VALUES(?,?,?,?)", normalized)


# ---- store-level: UUIDの有無で件数がぶれないこと ----

def test_uuid_present_and_absent_both_count_as_sales_target(tmp_path):
    store, _ = _setup_store(tmp_path, {
        "青空内視鏡クリニック": ("A", "existing-uuid-1"),   # 既存案件(UUIDあり)
        "若葉眼科医院": ("B", ""),                          # 新規案件(UUIDなし)
        "日向皮膚科": ("C", ""),                            # 営業対象外
        "月見内科": ("UNKNOWN", ""),                        # 営業対象外
    })
    filters = Filters(active_only=False, hp_only=False, effective_ranks=["A", "B"])
    assert store.count(filters) == 2
    assert store.count(filters, ) == store.count(filters)  # 冪等性

    yes = store.count(Filters(active_only=False, hp_only=False, effective_ranks=["A", "B"], uuid_mode="あり"))
    no = store.count(Filters(active_only=False, hp_only=False, effective_ranks=["A", "B"], uuid_mode="なし"))
    assert yes == 1 and no == 1 and yes + no == store.count(filters)


def test_export_includes_uuid_less_clinics(tmp_path):
    store, _ = _setup_store(tmp_path, {
        "青空内視鏡クリニック": ("A", "existing-uuid-1"),
        "若葉眼科医院": ("B", ""),
        "日向皮膚科": ("C", ""),
        "月見内科": ("D", ""),
    })
    filters = Filters(active_only=False, hp_only=False, effective_ranks=["A", "B"])
    files = store.export(filters)  # Stage2で uuid_mode="あり" の強制を撤廃済み
    result = load_table(files["final_comdesk_import.csv"], "x.csv")
    assert result.headers == COMDESK_HEADERS
    assert len(result.data) == 2  # A/Bの2件。UUID有無では除外されない。
    uuid_col = COMDESK_HEADERS.index("UUID")
    uuids = sorted(str(row[uuid_col]) for row in result.data.values.tolist())
    assert uuids == ["", "existing-uuid-1"]  # UUIDなし医院は空欄のまま出力される


def test_v2_machine_rank_drives_ui_filter_and_export_without_overwriting_old_rank(tmp_path, monkeypatch):
    store, clinics = _setup_store(tmp_path, {
        "青空内視鏡クリニック": ("A", ""),
        "若葉眼科医院": ("D", ""),
    })
    batch = tmp_path / "hp_batch.sqlite3"
    _make_hp_batch(batch, [
        (clinics["青空内視鏡クリニック"], "OK", "C"),
        (clinics["若葉眼科医院"], "OK", "B"),
    ])
    monkeypatch.setenv(HP_BATCH_ENV_VAR, str(batch))
    sales = Filters(active_only=False, hp_only=False, effective_ranks=["A", "B"])
    assert store.count(sales) == 1
    records = store.query(sales)
    assert records[0]["machine_rank"] == records[0]["effective_hp_rank"] == "B"
    assert records[0]["old_hp_rank_db"] == "D"
    assert len(load_table(store.export(sales)["final_comdesk_import.csv"], "x.csv").data) == 1
    with store.connect() as conn:
        assert dict(conn.execute("SELECT clinic_name,hp_rank FROM clinics"))["青空内視鏡クリニック"] == "A"


def test_c_and_unknown_never_exported_even_if_selected(tmp_path, monkeypatch):
    """store.export()自体はranksで指定された通りに出力する汎用APIであり、
    「営業対象はA+Bのみ」はapp_v2.py（UI）側がexport_ranksをA/Bへ絞り込むことで
    保証している（旧D UNKNOWN除外ロジックと同じ場所・同じ仕組み）。ここではUI経由で確認する。
    """
    store, _ = _setup_store(tmp_path, {
        "青空内視鏡クリニック": ("C", ""),
        "若葉眼科医院": ("UNKNOWN", ""),
        "日向皮膚科": ("D", ""),
        "月見内科": ("NO_HP", ""),
    })
    at = _app(tmp_path, monkeypatch, store.path)
    next(s for s in at.selectbox if s.label == "対象データ").set_value(SCOPE_ALL).run()
    for rank_label in ("C", "D"):
        next(s for s in at.selectbox if s.label == "HP ABC判定").set_value(rank_label).run()
        assert not at.exception
        export_count = next(m.value for m in at.metric if m.label == "Comdesk出力対象")
        assert export_count == "0件", f"{rank_label} must never be exported to Comdesk"
        assert any("全期間の出力は詳細設定" in x.value for x in at.info)


# ---- Treatment調査状態: HP ABC判定・出力対象に影響しないこと ----

def test_treatment_not_researched_still_exported(tmp_path, monkeypatch):
    store, clinics = _setup_store(tmp_path, {
        "青空内視鏡クリニック": ("A", ""),
        "若葉眼科医院": ("B", ""),
        "日向皮膚科": ("C", ""),
        "月見内科": ("D", ""),
    })
    sidecar_path = tmp_path / "treatment_research_final.sqlite3"
    _make_sidecar(str(sidecar_path))  # sidecarは存在するがどの医院にも行が無い＝全件Treatment未調査
    monkeypatch.setenv(RESEARCH_SIDECAR_ENV_VAR, str(sidecar_path))

    filters = Filters(active_only=False, hp_only=False, effective_ranks=["A", "B"])
    assert store.count(filters) == 2  # Treatment未調査でも自動除外されない

    statuses = store.treatment_status_for_ids([clinics["青空内視鏡クリニック"], clinics["若葉眼科医院"]])
    assert set(statuses.values()) == {"NOT_RESEARCHED"}


def test_treatment_fetch_failed_still_exported(tmp_path, monkeypatch):
    store, clinics = _setup_store(tmp_path, {
        "青空内視鏡クリニック": ("A", ""),
        "若葉眼科医院": ("B", ""),
        "日向皮膚科": ("C", ""),
        "月見内科": ("D", ""),
    })
    sidecar_path = tmp_path / "treatment_research_final.sqlite3"
    _make_sidecar(str(sidecar_path), status_rows=[(clinics["青空内視鏡クリニック"], "FETCH_FAILED")])
    monkeypatch.setenv(RESEARCH_SIDECAR_ENV_VAR, str(sidecar_path))

    filters = Filters(active_only=False, hp_only=False, effective_ranks=["A", "B"])
    assert store.count(filters) == 2  # HP取得失敗でも自動除外されない
    files = store.export(filters)
    result = load_table(files["final_comdesk_import.csv"], "x.csv")
    assert len(result.data) == 2

    status = store.treatment_status_for_ids([clinics["青空内視鏡クリニック"]])
    assert status[clinics["青空内視鏡クリニック"]] == "FETCH_FAILED"


def test_treatment_status_counts_four_buckets(tmp_path, monkeypatch):
    store, clinics = _setup_store(tmp_path, {
        "青空内視鏡クリニック": ("A", ""),
        "若葉眼科医院": ("B", ""),
        "日向皮膚科": ("C", ""),
        "月見内科": ("D", ""),
    })
    sidecar_path = tmp_path / "treatment_research_final.sqlite3"
    _make_sidecar(
        str(sidecar_path),
        treatment_rows=[(clinics["青空内視鏡クリニック"], "gastroscopy", "胃カメラ", "CONFIRMED")],
        status_rows=[
            (clinics["青空内視鏡クリニック"], "DONE"),   # CONFIRMED行があるので「取得済み」が優先
            (clinics["若葉眼科医院"], "DONE"),            # CONFIRMEDなし+DONE=「調査済み・該当カテゴリなし」
            (clinics["日向皮膚科"], "FETCH_FAILED"),
            # 月見内科: clinic_research_statusに行なし＝Treatment未調査
        ],
    )
    monkeypatch.setenv(RESEARCH_SIDECAR_ENV_VAR, str(sidecar_path))

    counts = store.treatment_status_counts(Filters(active_only=False, hp_only=False))
    assert counts == {
        "FETCHED": 1,
        "DONE_NO_CATEGORY": 1,
        "FETCH_FAILED": 1,
        "NOT_RESEARCHED": 1,
    }


def test_treatment_status_counts_none_when_sidecar_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv(RESEARCH_SIDECAR_ENV_VAR, str(tmp_path / "does-not-exist.sqlite3"))
    store, _ = _setup_store(tmp_path, {"青空内視鏡クリニック": ("A", "")})
    assert store.treatment_status_counts(Filters(active_only=False, hp_only=False)) is None


# ---- UI (AppTest): 営業対象件数 = Comdesk出力対象件数 ----

def _app(tmp_path, monkeypatch, db_path):
    # Stage4-C made Supabase the default READ backend; this fixture builds an isolated SQLite
    # DB with controlled test data, so it must pin the backend back to sqlite or it silently
    # reads live Supabase production data instead (see docs/supabase_migration/23_...: the
    # four tests this masked were all count assertions against this tmp fixture).
    monkeypatch.setenv("CLINIC_DATA_BACKEND", "sqlite")
    monkeypatch.setenv("CLINIC_DB_PATH", str(db_path))
    monkeypatch.setenv("CLINIC_DEMO_DB_PATH", str(tmp_path / "demo.db"))
    at = AppTest.from_file(str(ROOT / "app_v2.py"), default_timeout=30).run()
    at.session_state["navigation"] = "営業対象・出力"
    return at.run()


def test_ui_sales_target_count_equals_comdesk_export_count(tmp_path, monkeypatch):
    store, _ = _setup_store(tmp_path, {
        "青空内視鏡クリニック": ("A", "existing-uuid-1"),
        "若葉眼科医院": ("B", ""),
        "日向皮膚科": ("C", ""),
        "月見内科": ("A", ""),
    })
    at = _app(tmp_path, monkeypatch, store.path)
    next(s for s in at.selectbox if s.label == "対象データ").set_value(SCOPE_ALL).run()
    assert not at.exception

    sales_target = next(m.value for m in at.metric if m.label == "営業対象")
    export_target = next(m.value for m in at.metric if m.label == "Comdesk出力対象")
    assert sales_target == "3件"
    assert export_target == "0件"  # no current HP job: never export historical accumulation
    assert any("全期間の出力は詳細設定" in x.value for x in at.info)


def test_ui_web_research_six_metrics_displayed(tmp_path, monkeypatch):
    store, clinics = _setup_store(tmp_path, {
        "青空内視鏡クリニック": ("A", ""),
        "若葉眼科医院": ("B", ""),
        "日向皮膚科": ("C", ""),
        "月見内科": ("D", ""),
    })
    with store.connect() as conn:
        conn.execute("UPDATE clinics SET maps_website_url='https://example.test/' || id")
    batch_path = tmp_path / "hp_batch.sqlite3"
    _make_hp_batch(batch_path, [
        (clinics["青空内視鏡クリニック"], "OK", "A", '["内視鏡"]'),
        (clinics["若葉眼科医院"], "OK", "B", '[]'),
        (clinics["日向皮膚科"], "ERROR", None, '[]'),
        # 月見内科にはURLがあるがbatch結果がないため未調査。
    ])
    monkeypatch.setenv(HP_BATCH_ENV_VAR, str(batch_path))

    at = _app(tmp_path, monkeypatch, store.path)
    next(s for s in at.selectbox if s.label == "対象データ").set_value(SCOPE_ALL).run()
    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["WebサイトURL取得済み"] == "4件"
    assert metrics["Webサイト調査完了"] == "2件"
    assert metrics["Webサイト調査失敗"] == "1件"
    assert metrics["Webサイト未調査"] == "1件"
    assert metrics["治療カテゴリ検出あり"] == "1件"
    assert metrics["治療カテゴリ検出なし"] == "1件"
    assert not ({"Treatment該当あり", "Treatment該当なし", "Treatment未調査", "HP取得失敗"} & set(metrics))
    helps = {m.label: m.help for m in at.metric}
    assert helps["WebサイトURL取得済み"] == "医院データにWebサイトURLが登録されている医院数です。"
    assert helps["Webサイト調査完了"] == "登録されたWebサイトの取得・解析を完了し、HP ABC判定と治療カテゴリ判定まで完了した医院数です。"
    assert helps["Webサイト調査失敗"] == "Webサイトの取得または解析を正常完了できなかった医院数です。"
    assert helps["Webサイト未調査"] == "WebサイトURLは登録されていますが、まだ自動調査が完了していない医院数です。"
    assert helps["治療カテゴリ検出あり"] == "Webサイトから対象の治療・検査・施術カテゴリが1種類以上確認された医院数です。診療科の件数ではありません。"
    assert helps["治療カテゴリ検出なし"] == "Webサイト調査は完了していますが、現在定義している治療・検査・施術カテゴリが確認されなかった医院数です。"


def test_ui_site_type_filter_list_columns_and_export_alignment(tmp_path, monkeypatch):
    store, clinics = _setup_store(tmp_path, {
        "青空内視鏡クリニック": ("A", "existing"),
        "若葉眼科医院": ("B", ""),
        "日向皮膚科": ("A", ""),
        "月見内科": ("D", ""),
    })
    with store.connect() as conn:
        conn.execute("UPDATE clinics SET hp_status='VERIFIED',hp_url='https://official.example/' WHERE id=?", (clinics["青空内視鏡クリニック"],))
        conn.execute("UPDATE clinics SET hp_status='UNRESEARCHED',hp_url='',maps_website_url='https://doctorsfile.jp/h/1' WHERE id=?", (clinics["若葉眼科医院"],))
        conn.execute("UPDATE clinics SET hp_status='UNRESEARCHED',hp_url='',maps_website_url='https://other.example/' WHERE id=?", (clinics["日向皮膚科"],))
    batch_path = tmp_path / "hp_batch.sqlite3"
    _make_hp_batch(batch_path, [
        (clinics["青空内視鏡クリニック"], "OK", "A", '[]'),
        (clinics["若葉眼科医院"], "OK", "B", '[]'),
        (clinics["日向皮膚科"], "OK", "A", '[]'),
    ])
    with sqlite3.connect(batch_path) as conn:
        conn.execute("UPDATE hp_research_batch_results SET hp_url='https://official.example/',final_url='https://official.example/' WHERE clinic_id=?", (clinics["青空内視鏡クリニック"],))
        conn.execute("UPDATE hp_research_batch_results SET hp_url='https://doctorsfile.jp/h/1',final_url='https://doctorsfile.jp/h/1' WHERE clinic_id=?", (clinics["若葉眼科医院"],))
        conn.execute("UPDATE hp_research_batch_results SET hp_url='https://other.example/',final_url='https://other.example/' WHERE clinic_id=?", (clinics["日向皮膚科"],))
    monkeypatch.setenv(HP_BATCH_ENV_VAR, str(batch_path))

    at = _app(tmp_path, monkeypatch, store.path)
    next(s for s in at.selectbox if s.label == "対象データ").set_value(SCOPE_ALL).run()
    site_filter = next(s for s in at.selectbox if s.label == "サイト種別")
    for label in ("公式HP確認済み", "ポータルサイト", "その他・未確認"):
        site_filter.set_value(label).run()
        assert next(m.value for m in at.metric if m.label == "営業対象") == "1件"
        assert next(m.value for m in at.metric if m.label == "Comdesk出力対象") == "0件"
    frames = [frame.value for frame in at.dataframe if hasattr(frame.value, "columns")]
    assert any({"医院名", "HP ABC", "サイト種別", "ポータル名", "治療カテゴリ", "UUID有無"} <= set(frame.columns) for frame in frames)
