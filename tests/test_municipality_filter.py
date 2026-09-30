"""市区町村（municipality）filterの単体・統合テスト。"""
from src.master.filters import Filters
from src.master.samples import sample_records
from src.master.store import ClinicStore
from src.normalizer.address import extract_municipality


def test_extract_municipality_strips_prefecture_prefix():
    assert extract_municipality("東京都千代田区架空町1-1") == "千代田区"


def test_extract_municipality_handles_gun_plus_town():
    assert extract_municipality("東京都西多摩郡日の出町1234") == "西多摩郡日の出町"


def test_extract_municipality_no_match_returns_empty():
    assert extract_municipality("架空1-1") == ""
    assert extract_municipality("") == ""
    assert extract_municipality(None) == ""


def test_municipality_filter_narrows_to_matching_clinics(tmp_path):
    store = ClinicStore(tmp_path / "muni.db")
    records = sample_records()
    # sample_recordsは全件「東京都千代田区架空町...」なので、1件だけ別の市区町村に差し替える。
    records[0]["address"] = "東京都新宿区別架空町9-9"
    store.import_master(records)

    all_filter = Filters(active_only=False, hp_only=False)
    assert store.count(all_filter) == len(records)

    chiyoda = Filters(active_only=False, hp_only=False, municipalities=["千代田区"])
    rows = store.query(chiyoda, limit=100)
    assert store.count(chiyoda) == len(rows) == len(records) - 1
    assert all("千代田区" in row["address"] for row in rows)

    shinjuku = Filters(active_only=False, hp_only=False, municipalities=["新宿区"])
    rows = store.query(shinjuku, limit=100)
    assert store.count(shinjuku) == len(rows) == 1
    assert "新宿区" in rows[0]["address"]

    none_match = Filters(active_only=False, hp_only=False, municipalities=["存在しない区"])
    assert store.count(none_match) == 0
