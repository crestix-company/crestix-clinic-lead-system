from io import BytesIO
from pathlib import Path
import json
import zipfile

import pandas as pd
from openpyxl import Workbook

from src.master.google_maps import QUEUE_HEADERS
from src.master.national_maps_queue import (
    MASTER_HEADERS, OFFICIAL_SOURCES, PREFECTURES, OfficialSource, build_maps_population, build_package,
    parse_code_workbook, split_three,
)


def make_book(pref="大阪府"):
    book = Workbook(); ws = book.active
    ws.title = pref
    ws.append(["コード内容別医療機関一覧表"])
    ws.append(["[令和 8年 9月 1日現在　医科　現存/休止]"])
    ws.append(["1", "01,1234,5", "見本クリニック", "〒530-0001 大阪市北区梅田1-1", "06-1234-5678\n常勤:1", "開設 太郎", "院長 花子", "令2.1.1\n新規\n令2.1.1", "内 消", "診療所\n現存"])
    ws.append(["2", "01,1234,6", "見本病院", "〒530-0002 大阪市北区梅田1-2", "06-1234-5679", "A", "B", "令2.1.1", "一般 20 内", "病院\n現存"])
    ws.append(["3", "01,1234,7", "見本医療センター", "〒530-0003 大阪市北区梅田1-3", "06-1234-5680", "A", "B", "令2.1.1", "内", "診療所\n現存"])
    ws.append(["4", "01,1234,8", "休止クリニック", "〒530-0004 大阪市北区梅田1-4", "06-1234-5681", "A", "B", "令2.1.1", "内", "診療所\n休止"])
    out = BytesIO(); book.save(out); return out.getvalue()


def test_parse_generic_code_workbook_multiline():
    source = OfficialSource("x", "近畿厚生局", "大阪", "https://kouseikyoku.mhlw.go.jp/x.xlsx", "https://kouseikyoku.mhlw.go.jp/", "xlsx", prefecture_hint="大阪府", expected_prefectures=("大阪府",))
    frame = parse_code_workbook(make_book(), "大阪医科.xlsx", source)
    assert len(frame) == 4
    first = frame.iloc[0]
    assert first["clinic_id"] == "大阪府:0112345"
    assert first["phone"] == "06-1234-5678"
    assert first["facility_type"] == "診療所"
    assert first["status"] == "現存"
    assert first["registration_reason"] == "新規"
    assert first["as_of"] == "2026-09-01"


def test_population_filters_and_completed_ids():
    source = OfficialSource("x", "近畿厚生局", "大阪", "https://kouseikyoku.mhlw.go.jp/x.xlsx", "https://kouseikyoku.mhlw.go.jp/", "xlsx", prefecture_hint="大阪府", expected_prefectures=("大阪府",))
    frame = parse_code_workbook(make_book(), "大阪医科.xlsx", source)
    targets, counts = build_maps_population(frame, {"大阪府:0112345"})
    assert targets == []
    assert counts["already_maps_completed"] == 1
    assert counts["hospital"] == 1
    assert counts["center"] == 1
    assert counts["inactive"] == 1


def test_three_way_split_exact_and_no_duplicates():
    records = [
        {"clinic_id": f"東京都:{i:07d}", "clinic_name": f"医院{i}", "phone": "03-0000-0000", "address": "東京都", "prefecture": "東京都", "medical_type": "医科", "facility_type": "診療所", "status": "現存"}
        for i in range(101)
    ]
    groups = split_three(records)
    assert sorted(len(g) for g in groups) == [33, 34, 34]
    keys = [r["clinic_id"] for g in groups for r in g]
    assert len(keys) == len(set(keys)) == 101
    assert [[r["clinic_id"] for r in g] for g in groups] == [[r["clinic_id"] for r in g] for g in split_three(records)]


def test_package_uses_exact_existing_queue_headers():
    rows = [{
        "clinic_id": f"神奈川県:{i:07d}", "clinic_name": f"医院{i}", "address": "神奈川県横浜市", "phone": "045-000-0000",
        "prefecture": "神奈川県", "medical_type": "医科", "facility_type": "診療所", "owner_name": "", "manager_name": "",
        "designation_date": "", "registration_reason": "", "status": "現存", "as_of": "2026-09-01", "source_url": "",
        "designation_period_start": "", "departments": "内", "source_bureau": "関東信越厚生局", "source_file": "x.xlsx",
    } for i in range(5)]
    frame = pd.DataFrame(rows, columns=MASTER_HEADERS)
    package, manifest, files = build_package(frame, set(), [], {"completed": 0})
    assert manifest["pc_counts"] == {"pc1": 2, "pc2": 2, "pc3": 1}
    with zipfile.ZipFile(BytesIO(package)) as zf:
        assert {"maps_queue_pc1.csv", "maps_queue_pc2.csv", "maps_queue_pc3.csv", "maps_queue_all.csv", "national_kouseikyoku_master.csv", "manifest.json", "README_FIRST.txt"}.issubset(zf.namelist())
        header = zf.read("maps_queue_pc1.csv").decode("utf-8-sig").splitlines()[0].split(",")
        assert header == QUEUE_HEADERS


def test_real_tokyo_workbook_if_present():
    path = Path("data/raw/131コード内容別一覧表（医科）東京r0809.xlsx")
    if not path.exists():
        return
    source = OfficialSource("tokyo", "関東信越厚生局", "東京", "https://kouseikyoku.mhlw.go.jp/x.xlsx", "https://kouseikyoku.mhlw.go.jp/", "xlsx", expected_prefectures=("東京都",))
    frame = parse_code_workbook(path.read_bytes(), path.name, source)
    assert len(frame) > 10000
    assert set(frame["prefecture"]) == {"東京都"}
    assert set(frame["medical_type"]) == {"医科"}
    assert frame["clinic_id"].str.startswith("東京都:").all()
    assert frame["facility_type"].isin(["病院", "診療所"]).all()


def test_official_registry_covers_all_47_once():
    covered = [pref for source in OFFICIAL_SOURCES for pref in source.expected_prefectures]
    assert len(covered) == 47
    assert len(set(covered)) == 47
    assert set(covered) == set(PREFECTURES)
    assert all(source.url.startswith("https://kouseikyoku.mhlw.go.jp/") for source in OFFICIAL_SOURCES)
