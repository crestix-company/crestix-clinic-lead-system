"""Explicit opt-in verification against the disposable Phase J local database only."""
import os
import uuid
from datetime import timezone

import pytest
import psycopg

from src.outreach.phase_j_local_db import _json_safe, connect_verified, import_phase_i_snapshot, load_local_outreach_dashboard
from src.outreach.phase_g_sheets_snapshot import read_workbook_snapshot

pytestmark = pytest.mark.skipif(
    os.environ.get("PHASE_J_RUN_LOCAL_DB_TESTS") != "1",
    reason="set PHASE_J_RUN_LOCAL_DB_TESTS=1 to run against the named disposable Phase J database",
)


def test_phase_j_import_is_idempotent_across_three_runs_and_dashboard_reads_local_rows():
    first = import_phase_i_snapshot()
    before = {name: first[name] for name in (
        "source_rows_total", "confirmed_sales_cases_total", "gmail_messages_total", "state_events_total",
        "operational_open_sales_cases_total", "articles_total", "engagement_events_total", "appointments_total", "hold_rows_total")}
    second = import_phase_i_snapshot()
    third = import_phase_i_snapshot()
    assert second["source_rows_inserted_this_run"] == 0
    assert third["source_rows_inserted_this_run"] == 0
    assert all(value == 0 for value in second["new_business_rows_this_run"].values())
    assert all(value == 0 for value in third["new_business_rows_this_run"].values())
    assert {name: third[name] for name in before} == before
    assert before["source_rows_total"] == 4298
    assert before["confirmed_sales_cases_total"] > 0
    assert before["operational_open_sales_cases_total"] == 0
    assert before["hold_rows_total"] > 0

    view = load_local_outreach_dashboard()
    assert view["available"] is True
    assert view["sales_case_count"] == before["confirmed_sales_cases_total"]
    assert view["source_row_count"] == 4298
    assert view["hold_row_count"] == before["hold_rows_total"]
    assert view["cases"] and view["holds"] and view["activities"]
    assert {"clinic_uuid", "gas_case_id", "gmail_status", "article_url", "click_events", "open_events"}.issubset(view["cases"][0])
    assert all(case["case_status"] == "ON_HOLD" and "Campaign ID" in case["hold_reason"] for case in view["cases"])
    assert all("snapshot_retrieved_at" in row for row in view["holds"])
    with connect_verified() as conn:
        rls_total, rls_enabled = conn.execute("""select count(*),count(*) filter(where c.relrowsecurity and c.relforcerowsecurity)
          from pg_class c join pg_namespace n on n.oid=c.relnamespace
          where n.nspname='outreach' and c.relkind='r'""").fetchone()
        assert rls_total == rls_enabled == 22
        raw = conn.execute("""select raw_headers,raw_values,snapshot_retrieved_at from outreach.source_import_rows
          where source_tab='案件管理' and source_row_number=2""").fetchone()
        source = next(t for t in read_workbook_snapshot("/private/tmp/clinic_phase_i_source.xlsx") if t.name == "案件管理")
        assert raw[0] == list(source.headers)
        assert raw[1] == [_json_safe(value) for value in source.rows[0]]
        assert raw[2].astimezone(timezone.utc).isoformat().startswith("2026-10-20T12:26:53")


def test_phase_j_rls_append_only_and_trash_cancelled_constraints():
    with connect_verified() as conn:
        checks = conn.execute("""select c.relrowsecurity,c.relforcerowsecurity from pg_class c
          join pg_namespace n on n.oid=c.relnamespace where n.nspname='outreach' and c.relname in
          ('sales_cases','source_import_batches','source_import_rows','gmail_messages','email_state_history',
           'article_selection_results','engagement_events','calendar_appointments','sync_jobs')""").fetchall()
        assert checks and all(enabled and forced for enabled, forced in checks)
        constraint = conn.execute("""select pg_get_constraintdef(oid) from pg_constraint
          where conrelid='outreach.gmail_messages'::regclass and conname='gmail_messages_message_status_check'""").fetchone()[0]
        assert "TRASH" in constraint and "CANCELLED" in constraint

        case_id = conn.execute("select sales_case_id from outreach.sales_cases limit 1").fetchone()[0]
        conn.execute("begin")
        for status in ("TRASH", "CANCELLED"):
            conn.execute("""insert into outreach.gmail_messages(sales_case_id,operation_key,message_status,recipient_snapshot)
              values (%s,%s,%s,'fixture@example.invalid')""", (case_id, "phase-j-check:" + str(uuid.uuid4()), status))
        assert conn.execute("select count(*) from outreach.gmail_messages where recipient_snapshot='fixture@example.invalid'").fetchone()[0] == 2
        conn.rollback()

        conn.execute("begin")
        conn.execute("set local role clinic_outreach_runtime")
        assert conn.execute("select count(*) from outreach.sales_cases").fetchone()[0] > 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("update outreach.email_state_history set next_status='CANCELLED'")
        conn.rollback()
