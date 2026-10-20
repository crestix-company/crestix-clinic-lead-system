"""Exercise the READ ONLY repository against a disposable local PostgreSQL fixture.

Run tests/fixtures/cloudrun_readonly_projection_pg.sql first, then set
CLINIC_READONLY_PROJECTION_TEST_DSN to a loopback-only DSN for that disposable cluster.
This script refuses non-loopback hosts and never creates or modifies database objects.
"""
from __future__ import annotations

import io
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import psycopg
from psycopg import Cursor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.master.comdesk import COMDESK_EXPORT_HEADERS
from src.master.filters import Filters
from src.repository.runtime_store import SupabaseRuntimeStore
from src.repository.supabase_adapter import SupabaseClinicRepository, SupabaseHpResearchRepository


ALLOWED_READS = (
    "public.clinic_cloudrun_readonly",
    "hp_research.clinic_hp_research",
    "treatment.clinic_treatment_research",
    "treatment.clinic_research_status",
    "provenance.clinic_cloudrun_comdesk_rows",
    "provenance.templates",
    "public.clinic_email_enrichment",
)


class CaptureCursor(Cursor):
    statements: list[str] = []

    def execute(self, query, params=None):
        self.statements.append(" ".join(str(query).split()))
        return super().execute(query, params)


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    dsn = os.environ.get("CLINIC_READONLY_PROJECTION_TEST_DSN", "")
    if not dsn:
        raise SystemExit("Set CLINIC_READONLY_PROJECTION_TEST_DSN to the disposable local cluster DSN.")

    CaptureCursor.statements = []
    with psycopg.connect(dsn, autocommit=True, cursor_factory=CaptureCursor) as conn:
        if conn.info.host not in {"127.0.0.1", "localhost", "::1"}:
            raise SystemExit("Refusing a non-loopback database host.")
        with conn.cursor() as cur:
            cur.execute("SELECT current_user")
            _assert(cur.fetchone()[0] == "clinic_cloudrun_reader", "Must connect as clinic_cloudrun_reader")
            cur.execute("SET default_transaction_read_only = on")
            cur.execute("""
                SELECT owner.rolcanlogin, owner.rolbypassrls, owner.rolsuper,
                       clinics.relowner = owner.oid,
                       EXISTS (
                         SELECT 1 FROM pg_auth_members m
                         WHERE m.roleid = owner.oid AND m.member = reader.oid
                       )
                FROM pg_roles reader
                JOIN pg_roles owner ON owner.rolname='clinic_cloudrun_projection_owner'
                JOIN pg_class clinics ON clinics.oid='public.clinics'::regclass
                WHERE reader.rolname=current_user
            """)
            owner_login, owner_bypass, owner_super, owns_clinics, reader_is_member = cur.fetchone()
            _assert(not owner_login and not owner_bypass and not owner_super,
                    "Projection owner must be NOLOGIN/NOBYPASSRLS/NOSUPERUSER")
            _assert(not owns_clinics, "Projection owner must not own public.clinics")
            _assert(not reader_is_member, "Reader must not be able to SET ROLE to projection owner")
            cur.execute("SELECT has_column_privilege(current_user,'provenance.comdesk_original_rows','row_json','SELECT'), "
                        "has_table_privilege(current_user,'provenance.comdesk_original_rows','INSERT'), "
                        "has_table_privilege(current_user,'provenance.comdesk_original_rows','UPDATE'), "
                        "has_table_privilege(current_user,'provenance.comdesk_original_rows','DELETE'), "
                        "has_table_privilege(current_user,'provenance.comdesk_original_rows','TRUNCATE'), "
                        "has_table_privilege(current_user,'public.clinics','INSERT'), "
                        "has_table_privilege(current_user,'public.clinics','UPDATE'), "
                        "has_table_privilege(current_user,'public.clinics','DELETE'), "
                        "has_table_privilege(current_user,'public.clinics','TRUNCATE')")
            _assert(cur.fetchone() == (False,)*9, "Reader retains raw Comdesk or DML privileges")
            for relation in ALLOWED_READS:
                for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                    cur.execute("SELECT has_table_privilege(current_user,%s,%s)",
                                (relation, privilege))
                    _assert(cur.fetchone()[0] is False,
                            f"Reader retains {privilege} on {relation}")
            cur.execute("""
                SELECT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema='public' AND table_name='clinic_cloudrun_readonly'
                    AND column_name IN ('age_probability', 'age_probability_under_59')
                )
            """)
            _assert(not cur.fetchone()[0], "Inferred age projection is present in the READ ONLY view")
            cur.execute("""
                SELECT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema='public' AND table_name='clinic_cloudrun_readonly'
                    AND column_name='tel_match_key'
                ), has_column_privilege(
                  'clinic_cloudrun_projection_owner','public.clinics','tel_match_key','SELECT'
                )
            """)
            _assert(cur.fetchone() == (False, False),
                    "tel_match_key must remain internal to CRM identity matching")
            cur.execute("""
                SELECT data_type FROM information_schema.columns
                WHERE table_schema='public' AND table_name='clinic_cloudrun_readonly'
                  AND column_name='phone_norm'
            """)
            _assert(cur.fetchone() == ("text",), "phone_norm must be projected as text")

        CaptureCursor.statements = []
        hp = SupabaseHpResearchRepository(conn)
        clinics = SupabaseClinicRepository(conn, hp)
        store = SupabaseRuntimeStore(SimpleNamespace(clinics=clinics, hp_research=hp))

        default_filters = Filters()
        rows = store.query_read_only_ui(default_filters, limit=100, offset=0)
        _assert(store.count_read_only_ui(default_filters) == 9, "READ ONLY home count differs")
        _assert("東京都" in store.prefectures(read_only_ui=True), "READ ONLY prefectures missing")
        _assert(store.ad_count_max(read_only_ui=True) >= 0, "READ ONLY ad count failed")
        _assert(store.web_research_metrics(read_only_ui=True)["url_acquired"] >= 2,
                "READ ONLY HP summary failed")
        ids = [row["id"] for row in rows]
        _assert(ids == [1, 2, 3, 4, 5, 6, 7, 8, 1001], f"Nationwide eligible IDs differ: {ids}")
        _assert(len(ids) == len(set(ids)), "Clinic IDs duplicated in result list")
        _assert(store.count_read_only_ui(default_filters) == 9, "Nationwide count differs")

        department_rows = store.query_read_only_ui(Filters(departments=["内科"]))
        _assert([r["id"] for r in department_rows] == [1], "Department filter differs")
        name_rows = store.query_read_only_ui(Filters(keyword="Projection Clinic"))
        _assert([r["id"] for r in name_rows] == [1], "Clinic-name keyword search differs")
        phone_rows = store.query_read_only_ui(Filters(keyword="03-0000-0001"))
        _assert([r["id"] for r in phone_rows] == [1], "Normalized-phone keyword search differs")
        uuid_rows = store.query_read_only_ui(Filters(keyword="uuid-1"))
        _assert([r["id"] for r in uuid_rows] == [1], "UUID keyword search differs")
        hp_rank_rows = store.query_read_only_ui(Filters(effective_ranks=["A"]))
        _assert([r["id"] for r in hp_rank_rows] == [1], "Effective HP rank filter differs")
        treatment_rows = store.query_read_only_ui(Filters(hp_treatment_categories=["胃カメラ"]))
        _assert([r["id"] for r in treatment_rows] == [1], "Confirmed treatment filter differs")
        production_rows = store.query_read_only_ui(Filters(production_companies=["Alpha"]))
        _assert([r["id"] for r in production_rows] == [1], "Production-company postfilter differs")
        tokyo_rows = store.query_read_only_ui(Filters(prefectures=["東京都"]))
        _assert([r["id"] for r in tokyo_rows] == [1, 1001], "Nationwide prefecture selection differs")
        page_rows = store.query_read_only_ui(default_filters, limit=2, offset=3)
        _assert([r["id"] for r in page_rows] == [4, 5], "Pagination order differs")

        by_id = {row["id"]: row for row in rows}
        _assert(by_id[1]["epark_url"] == "A\x00B", "Escaped NUL was not restored in projected value")
        _assert(by_id[2]["epark_url"] == "A\\u0000B", "Literal backslash-u0000 changed")
        _assert(by_id[4]["clinic_name"] == "Malformed JSON Clinic", "Invalid JSON row crashed or disappeared")
        _assert(by_id[3]["clinic_name"] == "Null JSON Clinic", "NULL JSON row crashed or disappeared")
        _assert("owner_name" not in by_id[1], "Unapproved owner name was exposed")
        _assert("manager_name" not in by_id[1], "Unapproved manager name was exposed")

        output = store.export_read_only(default_filters)
        csv_payload = output["final_comdesk_import.csv"]
        csv_frame = pd.read_csv(io.BytesIO(csv_payload), encoding="utf-8-sig", dtype=str, keep_default_na=False)
        xlsx_frame = pd.read_excel(io.BytesIO(output["final_comdesk_import.xlsx"]), sheet_name="営業対象", dtype=str).fillna("")
        _assert(list(csv_frame.columns) == COMDESK_EXPORT_HEADERS, "CSV columns differ from Comdesk contract")
        _assert(list(xlsx_frame.columns) == COMDESK_EXPORT_HEADERS, "XLSX columns differ from Comdesk contract")
        _assert(len(csv_frame) == len(xlsx_frame) == 9, "CSV/XLSX row count differs")
        _assert(csv_frame.iloc[0]["名前"] == "Original Name", "Stored Comdesk value did not win")
        _assert(csv_frame.iloc[0]["Tel1"] == "03-1234-5678", "Stored Comdesk phone did not win")
        _assert(csv_frame.iloc[0]["午前始"] == "", "Pending business hours were not removed")
        _assert(csv_frame.iloc[0]["メールアドレス"] == "official@example.jp", "Verified official email missing")
        _assert(csv_frame.iloc[1]["メールアドレス"] == "", "Unverified email was output")
        _assert(csv_frame.iloc[1]["院長名"] == "", "Unapproved manager name fallback was output")
        _assert(csv_frame.iloc[1]["午前始"] == "", "Unapproved business hours fallback was output")
        _assert(csv_frame.iloc[0]["院長名"] == "", "Pending source doctor name was output")
        _assert(csv_frame.iloc[0]["休診日"] == "" and csv_frame.iloc[0]["診療日"] == "",
                "Pending source schedule values were output")
        _assert(csv_frame.iloc[0]["備考"] == "" and csv_frame.iloc[0]["履歴"] == "",
                "Unapproved free-text Comdesk values were output")
        try:
            store._export_records([{"id": 1000, "uuid": "uuid-hospital"}], read_only=True)
        except ValueError as exc:
            _assert("Comdesk元行の投影列" in str(exc), "Malformed Comdesk projection failed incorrectly")
        else:
            raise AssertionError("Malformed Comdesk row was exported instead of failing closed")
        _assert(xlsx_frame.iloc[0]["名前"] == "Original Name", "XLSX lost original Comdesk precedence")
        _assert(xlsx_frame.iloc[0]["Tel1"] == "03-1234-5678", "XLSX lost original phone precedence")
        _assert(xlsx_frame.iloc[0]["院長名"] == "" and xlsx_frame.iloc[0]["午前始"] == "",
                "XLSX exposed pending fields")
        nonblank_uuids = [value for value in csv_frame["UUID"] if value]
        _assert(nonblank_uuids == ["uuid-1"], "Export fixture contains duplicate or unexpected UUIDs")

        sql = "\n".join(CaptureCursor.statements).casefold()
        statements = [statement.lstrip().split(None, 1)[0].rstrip(";").upper()
                      for statement in CaptureCursor.statements]
        _assert(all(keyword in {"SELECT", "SET", "WITH"} for keyword in statements),
                f"READ ONLY path executed a non-read statement: {statements}")
        _assert(not re.search(r"\b(insert\s+into|update\s+[a-z_]|delete\s+from|alter\s+table|create\s+table|drop\s+table|truncate\s+table)\b", sql),
                "READ ONLY application SQL contains a write operation")
        _assert("effective_json" not in sql, "READ ONLY application SQL referenced effective_json")
        _assert(not re.search(r"\b(from|join)\s+public\.clinics\b", sql),
                "READ ONLY application directly selected public.clinics")
        _assert(not re.search(r"\b(from|join)\s+provenance\.comdesk_original_rows\b", sql),
                "READ ONLY application selected raw Comdesk rows")
        for forbidden in ("provenance.change_history", "provenance.match_reviews", "provenance.source_records",
                          "research.research_jobs", "research.research_results", "email_delivery", "email_kpi"):
            _assert(forbidden not in sql, f"Out-of-scope table was referenced: {forbidden}")
        for table in ALLOWED_READS:
            if table in sql:
                continue
            if table == "treatment.clinic_research_status":
                continue  # schema/table permission is retained but this UI route has no status lookup
            raise AssertionError(f"Unexpected allowlisted object missing only if expected by this operation: {table}")

    print("PASS: local READ ONLY adapter, filters, paging, CSV/XLSX, verified email, table boundary, and SQL capture")


if __name__ == "__main__":
    main()
