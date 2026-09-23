"""固定28列の出力契約と保存済み元データの保持を確認する。"""
from io import BytesIO
import json

import pytest
from openpyxl import load_workbook

from src.io.input_loader import load_table
from src.io.output_writer import csv_bytes
from src.master.comdesk import COMDESK_HEADERS, infer_comdesk_columns
from src.master.filters import Filters
from src.master.fixed_export import fixed_row
from src.master.samples import sample_records
from src.master.store import ClinicStore


ALL = Filters(active_only=False, hp_only=False)


def verify_files(files, expected_rows):
    for filename, content in files.items():
        result = load_table(content, filename)
        assert result.headers == COMDESK_HEADERS
        assert result.data.values.tolist() == expected_rows


@pytest.mark.parametrize("name_column", ["クリニック名", "名前", "医院名"])
def test_names_only_and_missing_fields(name_column, tmp_path):
    store = ClinicStore(tmp_path / "names.db")
    incoming = load_table(csv_bytes([name_column, "院長名"], [["架空クリニック", "見本 太郎"]]), "names.csv")
    assert infer_comdesk_columns(incoming)["clinic_name"] == 0
    store.import_comdesk(incoming)
    expected = [""] * 28
    expected[2], expected[26] = "架空クリニック", "見本 太郎"
    verify_files(store.export(ALL), [expected])


def test_empty_export_still_has_a_to_ab_headers(tmp_path):
    store = ClinicStore(tmp_path / "empty.db")
    verify_files(store.export(ALL), [])


def test_master_only_needs_no_input_template_and_includes_director(tmp_path):
    store = ClinicStore(tmp_path / "master.db")
    record = sample_records()[0]
    store.import_master([record])
    assert not store.templates()
    expected = [""] * 28
    expected[2], expected[5], expected[6] = record["clinic_name"], "東京都", record["address"].removeprefix("東京都")
    expected[9], expected[26] = record["phone"], record["manager_name"]
    verify_files(store.export(ALL), [expected])
    # 指定年月日を実際の開業日として捏造しない。
    assert expected[27] == ""


def test_mixed_input_formats_preserve_original_28_values_and_database(tmp_path):
    store = ClinicStore(tmp_path / "mixed.db")
    sparse = load_table(csv_bytes(["クリニック名", "院長名"], [["疎な架空医院", "見本 一郎"]]), "sparse.csv")
    store.import_comdesk(sparse)
    original = [f"元値{i}" for i in range(28)]
    original[0], original[2] = "000001", "別の架空医院"
    original[4], original[5], original[6], original[7] = "0010001", "東京都", "架空区1-2", "架空ビル2F"
    original[9], original[15], original[26] = "0312345678", "  改行\n=1+1  ", ""
    original[18] = "=1+1"
    store.import_comdesk(load_table(csv_bytes(COMDESK_HEADERS, [original]), "full.csv"))
    clinic = next(row for row in store.query() if row["uuid"])
    store.save_research(clinic["id"], {"manager_name": "別の院長", "hp_status": "VERIFIED", "hp_url": "https://new.example/"})
    with store.connect() as connection:
        before = list(connection.iterdump())
    expected_sparse = [""] * 28
    expected_sparse[2], expected_sparse[26] = "疎な架空医院", "見本 一郎"
    files = store.export(ALL)
    verify_files(files, [expected_sparse, original])
    with store.connect() as connection:
        assert list(connection.iterdump()) == before
    book = load_workbook(BytesIO(files["final_comdesk_import.xlsx"]))
    assert book.active["S3"].value == "=1+1"
    assert book.active["S3"].data_type == "s"


def test_conflicting_duplicate_headers_are_not_silently_dropped():
    with pytest.raises(ValueError, match="複数"):
        fixed_row({}, ["住所１", "住所1"], {}, ["住所A", "住所B"])
