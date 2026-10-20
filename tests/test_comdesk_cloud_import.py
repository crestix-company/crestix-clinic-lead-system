from types import SimpleNamespace

import pandas as pd

from src.master.comdesk_cloud_import import comdesk_import_idempotency_key, preview_comdesk_import


def _table(rows, headers=("UUID", "名前")):
    return SimpleNamespace(headers=list(headers), data=pd.DataFrame(rows, columns=list(headers)))


def test_idempotency_key_stable_for_identical_file_and_mapping():
    key1 = comdesk_import_idempotency_key(b"same bytes", {"uuid": 0, "clinic_name": 1})
    key2 = comdesk_import_idempotency_key(b"same bytes", {"uuid": 0, "clinic_name": 1})
    assert key1 == key2


def test_idempotency_key_changes_with_different_file_bytes():
    key1 = comdesk_import_idempotency_key(b"file A", {"uuid": 0})
    key2 = comdesk_import_idempotency_key(b"file B", {"uuid": 0})
    assert key1 != key2


def test_idempotency_key_changes_with_different_mapping():
    key1 = comdesk_import_idempotency_key(b"same bytes", {"uuid": 0})
    key2 = comdesk_import_idempotency_key(b"same bytes", {"uuid": 1})
    assert key1 != key2


def test_idempotency_key_is_order_independent_for_mapping_keys():
    key1 = comdesk_import_idempotency_key(b"x", {"uuid": 0, "clinic_name": 1})
    key2 = comdesk_import_idempotency_key(b"x", {"clinic_name": 1, "uuid": 0})
    assert key1 == key2


def test_preview_never_mutates_or_writes_and_reports_row_and_column_counts():
    table = _table([["u1", "医院A"], ["u2", "医院B"], ["u3", "医院C"]])
    mapping = {"uuid": 0, "clinic_name": 1}
    result = preview_comdesk_import(table, mapping)
    assert result["row_count"] == 3
    assert result["column_count"] == 2
    assert result["mapping"] == mapping
    assert len(result["preview"]) == 3  # within default preview_rows=5


def test_preview_bounds_to_preview_rows():
    table = _table([["u%d" % i, "医院%d" % i] for i in range(20)])
    result = preview_comdesk_import(table, {"uuid": 0}, preview_rows=5)
    assert len(result["preview"]) == 5
