import pandas as pd

from src.master.kouseikyoku_cloud_import import kouseikyoku_import_idempotency_key, preview_kouseikyoku_import


def test_idempotency_key_stable_for_identical_inputs():
    key1 = kouseikyoku_import_idempotency_key(b"bytes", mode="standard", prefecture="東京都", sheet=None)
    key2 = kouseikyoku_import_idempotency_key(b"bytes", mode="standard", prefecture="東京都", sheet=None)
    assert key1 == key2


def test_idempotency_key_changes_with_different_bytes():
    key1 = kouseikyoku_import_idempotency_key(b"A", mode="standard", prefecture="東京都", sheet=None)
    key2 = kouseikyoku_import_idempotency_key(b"B", mode="standard", prefecture="東京都", sheet=None)
    assert key1 != key2


def test_idempotency_key_changes_with_different_mode():
    key1 = kouseikyoku_import_idempotency_key(b"x", mode="standard", prefecture="東京都", sheet=None)
    key2 = kouseikyoku_import_idempotency_key(b"x", mode="kanto_excel", prefecture="東京都", sheet=None)
    assert key1 != key2


def test_idempotency_key_changes_with_different_prefecture():
    key1 = kouseikyoku_import_idempotency_key(b"x", mode="standard", prefecture="東京都", sheet=None)
    key2 = kouseikyoku_import_idempotency_key(b"x", mode="standard", prefecture="大阪府", sheet=None)
    assert key1 != key2


def test_idempotency_key_changes_with_different_sheet():
    key1 = kouseikyoku_import_idempotency_key(b"x", mode="kanto_excel", prefecture="東京都", sheet="Sheet1")
    key2 = kouseikyoku_import_idempotency_key(b"x", mode="kanto_excel", prefecture="東京都", sheet="Sheet2")
    assert key1 != key2


def test_preview_never_mutates_or_writes_and_reports_counts():
    frame = pd.DataFrame({"name": ["a", "b", "c"], "pref": ["東京都"] * 3})
    result = preview_kouseikyoku_import(frame)
    assert result["row_count"] == 3
    assert result["column_count"] == 2
    assert len(result["preview"]) == 3


def test_preview_bounds_to_preview_rows():
    frame = pd.DataFrame({"name": [f"n{i}" for i in range(20)]})
    result = preview_kouseikyoku_import(frame, preview_rows=5)
    assert len(result["preview"]) == 5
