from pathlib import Path

import pytest

from src.io.input_loader import load_table
from src.master.comdesk import COMDESK_HEADERS
from src.repository.runtime_store import (
    PROVISIONAL_COMDESK_EMAIL_HEADER,
    SupabaseRuntimeStore,
)


class _EmailCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.connection.sql = " ".join(str(sql).split())
        self.connection.params = params
        if self.connection.error is not None:
            raise self.connection.error

    def fetchall(self):
        return list(self.connection.rows)


class _EmailConnection:
    def __init__(self, rows=(), error=None):
        self.rows = list(rows)
        self.error = error
        self.sql = ""
        self.params = None

    def cursor(self):
        return _EmailCursor(self)


class _Clinics:
    def __init__(self, records):
        self.records = {record["id"]: record for record in records}

    def _batch_get(self, ids):
        return [self.records[clinic_id] for clinic_id in ids]


def _runtime(records, email_rows=(), error=None):
    store = object.__new__(SupabaseRuntimeStore)
    store._conn = _EmailConnection(email_rows, error)
    store.repositories = type("Repos", (), {"clinics": _Clinics(records)})()
    store.hp_job_export_summary = lambda _job_id: {"export_ids": [r["id"] for r in records]}
    original_rows = []
    for record in records:
        row = [""] * len(COMDESK_HEADERS)
        row[COMDESK_HEADERS.index("UUID")] = record.get("uuid", "")
        row[COMDESK_HEADERS.index("名前")] = record["clinic_name"]
        original_rows.append(row)
    store._export_records = lambda _records: [list(row) for row in original_rows]
    return store, original_rows


def _csv(store):
    return load_table(store.export_hp_job("job-1")["final_comdesk_import.csv"], "out.csv")


def _column_values(table, header):
    column = table.headers.index(header)
    return [table.value(row, column) for row in range(len(table.data))]


def _records():
    return [
        {"id": 10, "uuid": "UUID-10", "clinic_name": "十番医院"},
        {"id": 20, "uuid": "", "clinic_name": "二十番医院"},
    ]


def test_empty_email_table_exports_blank_email_without_changing_rows():
    store, original_rows = _runtime(_records(), [])

    output = _csv(store)

    assert output.headers == [*COMDESK_HEADERS, PROVISIONAL_COMDESK_EMAIL_HEADER]
    assert _column_values(output, PROVISIONAL_COMDESK_EMAIL_HEADER) == ["", ""]
    assert output.data.iloc[:, :len(COMDESK_HEADERS)].values.tolist() == original_rows
    assert _column_values(output, "UUID") == ["UUID-10", ""]
    assert len(output.data) == 2


@pytest.mark.parametrize(
    ("status", "verified_on_official", "expected"),
    [
        ("VERIFIED_EMAIL", True, "contact@example.jp"),
        ("SEARCH_CANDIDATE", True, ""),
        ("REVIEW", True, ""),
        ("VERIFIED_EMAIL", False, ""),
    ],
)
def test_only_official_verified_email_is_exported(status, verified_on_official, expected):
    store, _ = _runtime(
        [_records()[0]],
        [(10, "contact@example.jp", status, verified_on_official)],
    )

    output = _csv(store)

    assert output.value(0, output.headers.index(PROVISIONAL_COMDESK_EMAIL_HEADER)) == expected
    assert "status='VERIFIED_EMAIL'" in store._conn.sql
    assert "verified_on_official=true" in store._conn.sql


def test_multiple_verified_emails_are_deduplicated_and_deterministic():
    rows = [
        (10, "zeta@example.jp", "VERIFIED_EMAIL", True),
        (10, "Alpha@example.jp", "VERIFIED_EMAIL", True),
        (10, "alpha@example.jp", "VERIFIED_EMAIL", True),
        (10, "zeta@example.jp", "VERIFIED_EMAIL", True),
        (10, "candidate@example.jp", "SEARCH_CANDIDATE", True),
    ]
    first, _ = _runtime([_records()[0]], rows)
    second, _ = _runtime([_records()[0]], list(reversed(rows)))

    first_output = _csv(first)
    second_output = _csv(second)
    email_column = first_output.headers.index(PROVISIONAL_COMDESK_EMAIL_HEADER)
    first_value = first_output.value(0, email_column)
    second_value = second_output.value(0, email_column)

    assert first_value == second_value == "Alpha@example.jp;zeta@example.jp"


def test_email_lookup_failure_is_not_reported_as_blank_email():
    store, _ = _runtime([_records()[0]], error=PermissionError("permission denied"))

    with pytest.raises(PermissionError, match="permission denied"):
        store.export_hp_job("job-1")


def test_email_migration_is_select_only_and_preserves_existing_rls_configuration():
    sql = Path(
        "scripts/supabase_migration/clinic_email_enrichment_runtime_select.sql"
    ).read_text(encoding="utf-8").lower()

    assert "grant select on table public.clinic_email_enrichment to clinic_runtime" in sql
    assert "create policy clinic_runtime_clinic_email_enrichment_select" in sql
    assert "for select" in sql and "to clinic_runtime" in sql and "using (true)" in sql
    assert "grant insert" not in sql
    assert "grant update" not in sql
    assert "grant delete" not in sql
    assert "to anon" not in sql
    assert "to authenticated" not in sql
    assert "to public" not in sql
    assert "drop policy" not in sql
    assert "alter table" not in sql
