from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.outreach.pre_send_review import (
    DRAFT_SAVE_CHECKS, I3Reference, REQUIRED_CHECKS, build_draft_save_review,
    build_pre_send_review, configured_i3_reference,
)
from src.outreach.source_import import CSVPreviewError, preview_csv
from src.outreach.sync_plan import FIELD_AUTHORITIES, event_idempotency_key, plan_field_sync, shape_tracking_event
from src.repository.outreach_delivery_repository import connect_outreach_database


class _Target:
    clinic_id = 10
    clinic_name = "Clinic"
    director_name = "Director"
    director_name_confirmed = True
    email = "clinic@example.jp"
    article = type("Article", (), {"clinic_profile_url": "https://clinic.mynavi.jp/clinic/10"})()


class _Result:
    target = _Target()
    status = "READY"
    subject = "Subject"
    body = "Approved template body"
    listed_url = ""
    example_url = "https://clinic.mynavi.jp/article/1"
    first = None


def test_i3_is_unconfigured_by_default_and_blocks_without_authoritative_reference(monkeypatch):
    monkeypatch.delenv("OUTREACH_I3_DEFINITION_ID", raising=False)
    monkeypatch.delenv("OUTREACH_I3_DEFINITION_URL", raising=False)
    assert not configured_i3_reference().configured
    review, blockers = build_pre_send_review(
        result=_Result(), reviewer="IS lead", checks={key: True for key in REQUIRED_CHECKS},
        i3_reference=I3Reference("", ""), i3_acknowledged=False,
    )
    assert review["i3_definition_status"] == "UNCONFIRMED"
    assert any("I3" in blocker for blocker in blockers)


def test_pre_send_review_requires_all_operator_checks_and_preserves_actor_time():
    reviewed_at = datetime(2026, 10, 19, 1, 2, tzinfo=timezone.utc)
    checks = {key: True for key in REQUIRED_CHECKS}
    checks["duplicate_clear"] = False
    review, blockers = build_pre_send_review(
        result=_Result(), reviewer="Reviewer", checks=checks,
        i3_reference=I3Reference("I3-REF", "https://drive.google.com/approved"),
        i3_acknowledged=True, reviewed_at=reviewed_at, campaign_id="cmp",
    )
    assert review["reviewed_by"] == "Reviewer"
    assert review["reviewed_at"] == reviewed_at.isoformat()
    assert review["operation_key"] == "clinic:10:campaign:cmp"
    assert any("duplicate_clear" in blocker for blocker in blockers)


def test_draft_save_gate_is_separate_and_does_not_approve_send():
    review, blockers = build_draft_save_review(
        result=_Result(), reviewer="Operator", checks={key: True for key in DRAFT_SAVE_CHECKS},
        campaign_id="cmp",
    )
    assert not blockers
    assert review["stage"] == "DRAFT_SAVE"
    assert review["i3_definition_status"] == "UNCONFIRMED"


def test_csv_preview_preserves_header_order_raw_cells_and_duplicate_rows_without_merging():
    preview = preview_csv("案件ID,clinic_id,備考\nA,001, note \nA,001, note \n")
    assert preview.headers == ("案件ID", "clinic_id", "備考")
    assert len(preview.rows) == 2
    assert preview.rows[0] == ("A", "001", " note ")
    assert preview.row_numbers == (2, 3)
    assert preview.key_suggestions["clinic_id"] == ("clinic_id",)
    assert preview.row_hashes[0] == preview.row_hashes[1]
    assert len(set(preview.row_hashes)) == 1


def test_csv_preview_flags_ragged_rows_and_empty_headers():
    preview = preview_csv("clinic_id,case_id\n1\n")
    assert preview.ragged_rows == (2,)
    assert preview.rows == (("1", ""),)
    with pytest.raises(CSVPreviewError):
        preview_csv("\n")


def test_source_authority_conflict_is_not_overwritten():
    plan = plan_field_sync(
        {"gmail_status": "SENT", "is_owner": "Alice", "clinic_name": "Clinic"},
        {"gmail_status": "DRAFT", "is_owner": "Bob", "clinic_name": "Clinic"},
        source_system="GAS_SHEETS",
        known_sources={"gmail_status": "GMAIL_API", "is_owner": "GAS_SHEETS"},
    )
    assert "gmail_status" in plan.conflicts
    assert plan.conflicts["gmail_status"] == ("SENT", "DRAFT", "GMAIL_API")
    assert plan.updates["is_owner"] == "Bob"
    assert plan.unchanged == ("clinic_name",)
    assert FIELD_AUTHORITIES["article_selection"] == "ARTICLE_AUDIT"


def test_tracking_event_is_idempotent_privacy_minimized_and_click_url_is_validated():
    one = shape_tracking_event(
        operation_key="op-1", event_id="provider-event-1", event_type="CLICK",
        url="https://clinic.mynavi.jp/article/1", ip_address="203.0.113.1", user_agent="browser",
    )
    two = shape_tracking_event(
        operation_key="op-1", event_id="provider-event-1", event_type="CLICK",
        url="https://clinic.mynavi.jp/article/1", ip_address="203.0.113.1", user_agent="browser",
    )
    assert one["source_event_key"] == event_idempotency_key("TRACKING_API", "provider-event-1")
    assert one["source_event_key"] == two["source_event_key"]
    assert "203.0.113.1" not in str(one)
    assert "browser" not in str(one)
    with pytest.raises(ValueError):
        shape_tracking_event(operation_key="op", event_id="e", event_type="CLICK", url="http://insecure.test")


def test_schema_proposal_is_additive_idempotent_and_rls_restricted_without_send_path():
    migration = next(Path("supabase/migrations").glob("*outreach_full_sheets_gmail_integration.sql"))
    sql = migration.read_text()
    for table in (
        "sales_cases", "email_draft_save_reviews", "email_pre_send_reviews", "gmail_messages", "email_state_history",
        "article_selection_results", "engagement_events", "calendar_appointments",
        "source_import_batches", "source_import_rows", "sync_jobs", "sync_errors", "audit_events",
    ):
        assert f"create table outreach.{table}" in sql
        assert f"alter table outreach.%I enable row level security" in sql
    assert "unique(source_system,source_event_key)" in sql
    assert "unique(import_batch_id,source_tab,source_row_number)" in sql
    assert "email_delivery_records_sales_case_history_idx" in sql
    assert "unique index email_delivery_records_sales_case" not in sql
    assert "on conflict (operation_key) do nothing" in sql
    assert "revoke delete,truncate on outreach.source_import_rows" in sql
    assert "clinic_outreach_runtime role must exist" in sql
    assert "create role" not in sql.lower()
    assert "gmail.send" not in sql.lower()
    assert "sendmail" not in sql.lower()


def test_cloud_run_read_only_flag_protects_the_separate_outreach_connection():
    class Cursor:
        def __init__(self):
            self.sql = ""
            self.calls = []
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def execute(self, sql, params=()):
            self.sql = sql
            self.calls.append(sql)
        def fetchone(self):
            if self.sql == "SELECT current_user":
                return ("clinic_runtime",)
            return ("clinic_outreach_runtime", "clinic_outreach_runtime", True, True, True,
                    False, False, False, False, False, False)

    class Connection:
        def __init__(self): self.cur = Cursor(); self.committed = False; self.rolled_back = False; self.closed = False
        def cursor(self): return self.cur
        def commit(self): self.committed = True
        def rollback(self): self.rolled_back = True
        def close(self): self.closed = True

    conn = Connection()
    connected = connect_outreach_database(
        environ={"SUPABASE_RUNTIME_DB_URL": "postgres://synthetic", "CLINIC_READ_ONLY_UI": "true"},
        connect_fn=lambda url, **kwargs: conn,
    )
    assert connected is conn
    assert conn.cur.calls[:2] == ["SET TRANSACTION READ ONLY", "SET default_transaction_read_only = on"]
    assert "SET ROLE clinic_outreach_runtime" in conn.cur.calls
    assert conn.committed and not conn.closed
