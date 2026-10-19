import io
import json
from pathlib import Path

import pandas as pd

from src.io.input_loader import load_table
from src.master.comdesk import COMDESK_EXPORT_HEADERS, COMDESK_HEADERS
from src.master.filters import Filters
from src.repository.runtime_store import SupabaseRuntimeStore
from src.repository.supabase_filters import where as supabase_where


class _Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(str(sql).split())
        self.connection.statements.append(normalized)
        self.connection.parameters.append(params)
        if "provenance.comdesk_original_rows" in normalized:
            original = [""] * len(COMDESK_HEADERS)
            original[COMDESK_HEADERS.index("UUID")] = "uuid-101"
            original[COMDESK_HEADERS.index("名前")] = "元データ医院"
            original[COMDESK_HEADERS.index("都道府県")] = "東京都"
            original[COMDESK_HEADERS.index("院長名")] = "承認待ち医師"
            original[COMDESK_HEADERS.index("午前始")] = "09:00"
            self.rows = [
                (101, 7, json.dumps(original, ensure_ascii=False), "uuid-101")
            ]
        elif "provenance.templates" in normalized:
            self.rows = [(7, json.dumps(COMDESK_HEADERS, ensure_ascii=False), json.dumps({}))]
        elif "public.clinic_email_enrichment" in normalized:
            self.rows = [
                (101, "Clinic@example.jp", "VERIFIED_EMAIL", True),
                (101, "clinic@EXAMPLE.jp", "VERIFIED_EMAIL", True),
                (102, "candidate@example.jp", "SEARCH_CANDIDATE", True),
                (102, "   ", "VERIFIED_EMAIL", True),
                (999, "out-of-scope@example.jp", "VERIFIED_EMAIL", True),
            ]
        else:
            raise AssertionError(f"Unexpected READ ONLY export query: {normalized}")

    def fetchall(self):
        return list(self.rows)


class _Connection:
    def __init__(self):
        self.statements = []
        self.parameters = []

    def cursor(self):
        return _Cursor(self)


def _store():
    records = [
        {"id": 101, "uuid": "uuid-101", "clinic_name": "元データ医院", "prefecture": "東京都", "effective_hp_rank": "A"},
        {"id": 102, "uuid": "uuid-102", "clinic_name": "二番医院", "prefecture": "大阪府", "effective_hp_rank": "b"},
    ]
    store = object.__new__(SupabaseRuntimeStore)
    store._conn = _Connection()
    clinic_repo = type("ClinicRepo", (), {})()
    clinic_repo.query = lambda _filters, limit=100000, offset=0, as_of=None, read_only_ui=False: list(records)
    store.repositories = type("Repos", (), {"clinics": clinic_repo})()
    return store


def test_read_only_export_preserves_comdesk_data_but_blanks_pending_person_and_schedule_fields():
    readonly = _store()
    normal = _store()

    readonly_output = readonly.export_read_only(Filters())
    normal_output = normal.export(Filters())

    assert readonly_output != normal_output
    assert len(readonly._conn.statements) == 3
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in readonly._conn.statements)
    references = "\n".join(readonly._conn.statements)
    assert "provenance.comdesk_original_rows" in references
    assert "provenance.templates" in references
    assert "public.clinic_email_enrichment" in references
    assert readonly._conn.parameters[2] == ([101, 102],)
    for excluded in (
        "provenance.change_history", "provenance.match_reviews", "provenance.source_records",
        "email_delivery", "email_kpi", "hp_job", "sales_entity_",
    ):
        assert excluded not in references

    csv_payload = readonly_output["final_comdesk_import.csv"]
    assert csv_payload.startswith(b"\xef\xbb\xbf")
    csv_table = load_table(csv_payload, "final_comdesk_import.csv")
    assert csv_table.headers == COMDESK_EXPORT_HEADERS
    assert len(csv_table.data) == 2
    assert csv_table.data.iloc[0][COMDESK_EXPORT_HEADERS.index("名前")] == "元データ医院"
    assert csv_table.data.iloc[0][COMDESK_EXPORT_HEADERS.index("メールアドレス")] == "Clinic@example.jp"
    assert csv_table.data.iloc[1][COMDESK_EXPORT_HEADERS.index("メールアドレス")] == ""
    assert csv_table.data.iloc[0][COMDESK_EXPORT_HEADERS.index("HPランク")] == "A"
    assert csv_table.data.iloc[1][COMDESK_EXPORT_HEADERS.index("HPランク")] == "B"
    assert len({101, 102}) == len(csv_table.data)  # source query has one row per Clinic ID
    original_csv = load_table(normal_output["final_comdesk_import.csv"], "final_comdesk_import.csv")
    assert original_csv.headers == csv_table.headers
    assert original_csv.data.iloc[0][COMDESK_EXPORT_HEADERS.index("院長名")] == "承認待ち医師"
    assert original_csv.data.iloc[0][COMDESK_EXPORT_HEADERS.index("午前始")] == "09:00"
    assert csv_table.data.iloc[0][COMDESK_EXPORT_HEADERS.index("院長名")] == ""
    assert csv_table.data.iloc[0][COMDESK_EXPORT_HEADERS.index("午前始")] == ""
    for header in ("休診日", "診療日", "午前終", "午後始", "午後終"):
        assert csv_table.data.iloc[0][COMDESK_EXPORT_HEADERS.index(header)] == ""
    assert csv_table.data.iloc[0][COMDESK_EXPORT_HEADERS.index("名前")] == original_csv.data.iloc[0][COMDESK_EXPORT_HEADERS.index("名前")]

    workbook = pd.read_excel(io.BytesIO(readonly_output["final_comdesk_import.xlsx"]), sheet_name="営業対象").fillna("")
    assert list(workbook.columns) == COMDESK_EXPORT_HEADERS
    assert len(workbook) == 2
    assert workbook.iloc[0]["名前"] == "元データ医院"
    assert workbook.iloc[0]["メールアドレス"] == "Clinic@example.jp"
    assert workbook.iloc[0]["UUID"] == "uuid-101"
    assert workbook.iloc[0]["院長名"] == ""
    assert workbook.iloc[0]["午前始"] == ""


def test_read_only_export_does_not_write_sql():
    store = _store()
    store.export_read_only(Filters())
    assert all("INSERT " not in sql.upper() and "UPDATE " not in sql.upper() and "DELETE " not in sql.upper()
               for sql in store._conn.statements)


def test_read_only_home_and_sales_ui_skip_internal_reads_and_revision():
    source = Path("app_v2.py").read_text(encoding="utf-8")
    workflow = source[source.index("def simple_workflow_ui"):source.index("def simple_sales_ui")]
    readonly_home = workflow[workflow.index("if read_only:"):workflow.index("write_disabled =")]
    assert "store.count(" in readonly_home
    assert "show_web_research_metrics(store)" in readonly_home
    for forbidden in ("store.metrics(", "email_repositories_for_ui", "store.latest_completed_hp_job(",
                      "store.reviews(", "store.history(", "comdesk_sync_candidates("):
        assert forbidden not in readonly_home.casefold()

    sales = source[source.index("def simple_sales_ui"):source.index("def advanced_", source.index("def simple_sales_ui"))]
    assert "read_only=read_only" in sales
    assert "None if read_only else store.revision()" in sales
    assert "store.export_read_only(sales_filters)" in sales
    assert "listing(store, sales_filters, \"simple_sales_results\", read_only=read_only)" in sales
    assert "readonly_export_over_limit = read_only and count > 100000" in sales
    assert "disabled=demo or count == 0 or readonly_export_over_limit" in sales
    assert "一部のみのファイルを誤って出力しないよう" in sales


def test_read_only_treatment_pair_filter_uses_only_confirmed_treatment_sidecar():
    filters = Filters(departments=["消化器内科"], sales_pairs=[("消化器内科", "胃カメラ")])
    ordinary_sql, _ = supabase_where(filters, excluded_ids=[])
    readonly_sql, _ = supabase_where(filters, excluded_ids=[], read_only_ui=True)

    assert "research.research_results" in ordinary_sql
    assert "research.research_results" not in readonly_sql
    assert "treatment.clinic_treatment_research" in readonly_sql
    assert "research_status='CONFIRMED'" in readonly_sql


def test_read_only_main_cannot_enter_demo_loader_or_write_side_effects():
    source = Path("app_v2.py").read_text(encoding="utf-8")
    main = source[source.index("def main():"):source.index("def user_error_text")]
    sidebar_start = main.index("with st.sidebar:")
    readonly_sidebar = main[sidebar_start:main.index("    if read_only:\n        st.warning", sidebar_start)]
    assert "if read_only:" in readonly_sidebar
    assert "demo = False" in readonly_sidebar
    assert "st.toggle(\"サンプルモード\"" in readonly_sidebar

    demo_block = main[main.index("if demo:", main.index("def main():")):main.index("if nav == \"かんたん操作\"")]
    assert "load_demo(store)" in demo_block
