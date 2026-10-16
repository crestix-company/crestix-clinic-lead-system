import csv
import io
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.outreach.history import (
    HISTORY_COLUMNS, HistoryInputError, HistoryMatch, OutreachLedger, email_collisions,
    parse_history_csv, reconcile_history,
)
from src.outreach.targets import select_targets


def _csv(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=sorted(HISTORY_COLUMNS))
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode()


def _row(**overrides):
    row = {key: "" for key in HISTORY_COLUMNS}
    row.update(clinic_id="1", clinic_uuid="u1", campaign_id="c1", status="NOT_STARTED", source="sheet")
    row.update(overrides)
    return row


def test_history_csv_requires_explicit_schema_and_does_not_guess():
    with pytest.raises(HistoryInputError, match="標準列"):
        parse_history_csv(b"clinic,email,status\n1,a@example.jp,SENT\n")
    assert parse_history_csv(_csv([_row()]))[0].clinic_id == 1


def test_generation_csv_is_importable_and_has_stable_outreach_identity():
    from datetime import date
    from src.outreach.compose import ARTICLE_NOT_LISTED
    from src.outreach.generation import GenerationSettings, generate_batch, to_csv_bytes
    from src.outreach.targets import ArticleState, ClinicTarget
    target = ClinicTarget(1, "u1", "Clinic", "東京都", "世田谷区", ("内科",), (), "A", "院長", "a@example.jp",
                          "FOUND", article=ArticleState(ARTICLE_NOT_LISTED))
    results = generate_batch([target], [], GenerationSettings(staff="担当", dates=(date(2026, 10, 19),)))
    output = to_csv_bytes(results, "担当", "batch", "campaign")
    parsed = parse_history_csv(output)
    assert parsed[0].clinic_id == 1 and parsed[0].status == "HOLD"
    assert to_csv_bytes(results, "担当", "batch", "campaign") == output


def test_reconciliation_exact_clinic_and_uuid_but_unmatched_is_hold():
    rows = [HistoryMatch(1, "u1", "c1", "SENT", "sheet", "o1")]
    assert reconcile_history(1, "u1", "c1", rows)[0] == "SENT"
    assert reconcile_history(2, "u2", "c1", rows)[0] == "HOLD"
    assert reconcile_history(1, "u1", "c1", [HistoryMatch(1, "u1", "c1", "NOT_STARTED", "sheet")])[0] == "NOT_STARTED"
    assert reconcile_history(1, "other", "c1", rows)[0] == "HOLD"
    uuid_only = [HistoryMatch(999, "u1", "c1", "DRAFTED", "sheet", "o2")]
    assert reconcile_history(1, "u1", "c1", uuid_only)[0] == "DRAFTED"
    assert reconcile_history(1, "u1", "c1", [])[0] == "HOLD"


def test_email_collisions_hold_every_clinic_sharing_address():
    assert email_collisions([(1, "Info@EXAMPLE.jp"), (2, "info@example.jp"), (3, "solo@example.jp")]) == {1, 2}


def test_selection_uses_formal_eligibility_ids_and_withholds_history():
    records = [
        {"id": 1, "uuid": "u1", "clinic_name": "A", "address": "東京都世田谷区", "prefecture": "東京都"},
        {"id": 2, "uuid": "u2", "clinic_name": "B", "address": "東京都世田谷区", "prefecture": "東京都"},
        {"id": 3, "uuid": "u3", "clinic_name": "C", "address": "東京都世田谷区", "prefecture": "東京都"},
    ]
    selected = select_targets(records, email_lookup=lambda ids: {cid: f"{cid}@example.jp" for cid in ids},
                              links=[], limit=10, eligible_clinic_ids={1, 2}, withheld_clinic_ids={2})
    assert [target.clinic_id for target in selected.targets] == [1]


def test_ledger_enforces_idempotency_under_concurrent_generation(tmp_path):
    ledger_path = str(tmp_path / "outreach_ledger.sqlite3")
    def create(_):
        ledger = OutreachLedger(ledger_path)
        return ledger.create_once(clinic_id=17, clinic_uuid="u17", campaign_id="c1",
                                  generation_batch_id="batch", status="GENERATED")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(create, range(16)))
    assert sum(created for _outreach_id, created in results) == 1
    assert len({outreach_id for outreach_id, _created in results}) == 1
    assert OutreachLedger(ledger_path).campaign_statuses("c1") == {17: "GENERATED"}


def test_same_clinic_campaign_cannot_be_duplicate_draft_or_send(tmp_path):
    ledger = OutreachLedger(str(tmp_path / "outreach_ledger.sqlite3"))
    first, created = ledger.create_once(clinic_id=5, clinic_uuid="u5", campaign_id="same",
                                        generation_batch_id="b1", status="DRAFTED")
    second, created_again = ledger.create_once(clinic_id=5, clinic_uuid="u5", campaign_id="same",
                                               generation_batch_id="b2", status="SENT")
    assert created and not created_again and first == second
    assert ledger.counts() == {"DRAFTED": 1}


def test_gmail_metadata_is_recorded_as_imported_fact_only(tmp_path):
    ledger = OutreachLedger(str(tmp_path / "outreach_ledger.sqlite3"))
    ledger.record_generation(clinic_id=3, clinic_uuid="u3", campaign_id="c3",
                             generation_batch_id="b3", owner="担当")
    ledger.record_gmail_metadata(clinic_id=3, campaign_id="c3", status="SENT",
                                 gmail_message_id="message-1", sent_at="2026-10-16T00:00:00Z")
    with sqlite3.connect(ledger.path) as conn:
        row = conn.execute("SELECT status,gmail_message_id,sent_at FROM outreach_records WHERE clinic_id=3").fetchone()
    assert row == ("SENT", "message-1", "2026-10-16T00:00:00Z")
    with pytest.raises(ValueError, match="過去"):
        ledger.record_gmail_metadata(clinic_id=3, campaign_id="c3", status="DRAFTED", gmail_draft_id="draft")


def test_draft_status_requires_external_gmail_id(tmp_path):
    ledger = OutreachLedger(str(tmp_path / "outreach_ledger.sqlite3"))
    ledger.record_generation(clinic_id=7, clinic_uuid="u7", campaign_id="c7", generation_batch_id="b7")
    with pytest.raises(ValueError, match="Draft ID"):
        ledger.record_gmail_metadata(clinic_id=7, campaign_id="c7", status="DRAFTED")


def test_ledger_uses_an_explicit_separate_path_only():
    with pytest.raises(ValueError, match="明示的"):
        OutreachLedger("")
    with pytest.raises(ValueError, match="絶対パス"):
        OutreachLedger("outreach_ledger.sqlite3")


def test_ledger_refuses_clinic_master_database_path(tmp_path, monkeypatch):
    clinic_db = tmp_path / "clinic.sqlite3"
    with sqlite3.connect(clinic_db) as conn:
        conn.execute("CREATE TABLE clinics(id INTEGER)")
    monkeypatch.setenv("CLINIC_DB_PATH", str(clinic_db))
    with pytest.raises(ValueError, match="Clinic Master"):
        OutreachLedger(str(clinic_db))
