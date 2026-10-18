from __future__ import annotations

import json

from scripts import gas_outreach_readonly_canary as canary
from src.outreach.gas_email_lookup import GasEmailLookupError, GasEmailResult
from src.outreach.readonly_access import ReadOnlyAccessError
from src.repository.outreach_delivery_repository import OutreachDatabaseError


def records(n=5):
    # comdesk_uuid is intentionally absent from every record: Clinic Master never
    # carries it (see src/outreach/targets.py), and this Canary must not need it.
    return [{"id": i, "uuid": f"0000{i:04d}-aaaa-bbbb-cccc-dddddddddddd", "clinic_name": f"医院{i}"}
            for i in range(1, n + 1)]


class FakeOutreachRepo:
    def __init__(self, statuses_by_clinic):
        self._statuses = statuses_by_clinic
        self.closed = False

    def clinic_statuses(self, clinic_ids):
        return {cid: self._statuses.get(cid, []) for cid in clinic_ids}

    def close(self):
        self.closed = True


def test_happy_path_counts_and_never_filters_on_comdesk_uuid(tmp_path):
    recs = records(5)

    class FakeEmailResearch:
        def primary_listing(self, *, limit=500):
            return [{"clinic_id": i, "candidate_id": "", "email": f"c{i}@example.jp",
                     "final_sales_eligible": True} for i in range(1, 4)]

    class FakeBundle:
        email_research = FakeEmailResearch()

    code = canary.main(
        ["--limit", "5", "--out", str(tmp_path / "out")],
        fetch_records=lambda args, connect_fn: recs,
        email_build_repositories=lambda **k: (FakeBundle(), None),
        open_outreach_repository=lambda: FakeOutreachRepo({}),
    )
    assert code == 0
    report = json.loads((tmp_path / "out" / "gas_canary_report.json").read_text(encoding="utf-8"))
    assert report["clinic_id_resolved"] == 5
    assert report["primary_email_found"] == 3
    assert report["gas_payload_ready"] == 3
    assert report["comdesk_uuid_used_as_filter"] is False
    assert report["writes_performed"] is False
    assert report["gmail_drafts_created"] == 0 and report["gmail_messages_sent"] == 0


def test_gmail_blocking_status_excludes_from_payload_ready(tmp_path):
    recs = records(3)

    class FakeEmailResearch:
        def primary_listing(self, *, limit=500):
            return [{"clinic_id": i, "candidate_id": "", "email": f"c{i}@example.jp",
                     "final_sales_eligible": True} for i in (1, 2, 3)]

    class FakeBundle:
        email_research = FakeEmailResearch()

    code = canary.main(
        ["--limit", "3", "--out", str(tmp_path / "out")],
        fetch_records=lambda args, connect_fn: recs,
        email_build_repositories=lambda **k: (FakeBundle(), None),
        open_outreach_repository=lambda: FakeOutreachRepo({2: [{"status": "SENT"}]}),
    )
    assert code == 0
    report = json.loads((tmp_path / "out" / "gas_canary_report.json").read_text(encoding="utf-8"))
    assert report["gmail_excluded_count"] == 1
    assert report["gas_payload_ready"] == 2  # clinic 2 excluded despite having a Primary Email


def test_clinic_master_permission_denial_stops_before_any_other_connection(tmp_path):
    def fetch_records(args, connect_fn):
        raise ReadOnlyAccessError("CLINIC_READONLY_DB_URL が未設定です。")
    code = canary.main(["--limit", "5", "--out", str(tmp_path / "out")], fetch_records=fetch_records)
    assert code == canary.EXIT_PERMISSION
    assert not (tmp_path / "out").exists()


def test_email_research_connection_failure_is_reported_not_swallowed(tmp_path):
    recs = records(2)

    def boom(**kwargs):
        raise GasEmailLookupError("Email調査DBに接続できません: boom")
    code = canary.main(
        ["--limit", "2", "--out", str(tmp_path / "out")],
        fetch_records=lambda args, connect_fn: recs,
        email_build_repositories=boom,
    )
    assert code == canary.EXIT_PERMISSION
    assert not (tmp_path / "out").exists()


def test_outreach_ledger_connection_failure_is_reported(tmp_path):
    recs = records(1)

    class FakeEmailResearch:
        def primary_listing(self, *, limit=500):
            return []

    class FakeBundle:
        email_research = FakeEmailResearch()

    def boom():
        raise OutreachDatabaseError("boom")
    code = canary.main(
        ["--limit", "1", "--out", str(tmp_path / "out")],
        fetch_records=lambda args, connect_fn: recs,
        email_build_repositories=lambda **k: (FakeBundle(), None),
        open_outreach_repository=boom,
    )
    assert code == canary.EXIT_PERMISSION
    assert not (tmp_path / "out").exists()
