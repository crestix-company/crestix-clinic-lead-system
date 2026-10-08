import inspect

import pytest

from src.master.comdesk import record_from_row
from src.master.matching import match_record
from src.normalizer.address import normalize_address
from src.normalizer.clinic_name import normalize_clinic_name
from src.normalizer.phone import tel_match_key
from src.repository.supabase_write_adapter import (
    _PrefetchedMatchingConnection,
    SupabaseClinicWriteRepository,
)


MAPPING = {
    "uuid": 0,
    "clinic_name": 1,
    "phone": 2,
    "prefecture": 3,
    "address": 4,
    "address2": 5,
}


def _record(prefecture, address, *, name="医院", uuid="", phone=""):
    return record_from_row(
        [uuid, name, phone, prefecture, address, ""],
        MAPPING,
    )


@pytest.mark.parametrize(
    ("prefecture", "address"),
    [
        ("宮崎県", "北諸県郡三股町樺山4672-4"),
        ("宮崎県", "東諸県郡綾町南俣443-6"),
        ("岐阜県", "岐阜市県町1-6"),
    ],
)
def test_comdesk_prefecture_parser_does_not_misread_county_or_city_text(prefecture, address):
    result = _record(prefecture, address)

    assert result["prefecture"] == prefecture
    assert result["address"] == prefecture + address


def test_comdesk_prefecture_parser_still_rejects_real_prefecture_mismatch():
    with pytest.raises(ValueError, match="都道府県の列と住所の都道府県が一致しません"):
        _record("宮崎県", "鹿児島県鹿児島市中央町1-1")


def _candidate(
    clinic_id,
    *,
    uuid="",
    phone="",
    name="テスト医院",
    address="東京都新宿区1-1",
    prefecture="東京都",
    medical_type="医科",
    medical_key="",
    merge_hold=False,
):
    name_norm = normalize_clinic_name(name)
    return {
        "id": clinic_id,
        "base_json": {
            "clinic_name": name,
            "phone": phone,
            "address": address,
            "prefecture": prefecture,
            "medical_type": medical_type,
        },
        "uuid": uuid,
        "medical_key": medical_key,
        "tel_match_key": tel_match_key(phone),
        "name_norm": name_norm,
        "name_prefix": name_norm[:2],
        "address_norm": normalize_address(address),
        "prefecture": prefecture,
        "medical_type": medical_type,
        "merge_hold": merge_hold,
        "source_as_of_date": "",
    }


def test_prefetched_matcher_keeps_phone_exact_contract():
    conn = _PrefetchedMatchingConnection([
        _candidate(10, phone="03-1111-2222"),
    ])
    record = {
        "clinic_name": "テスト医院",
        "phone": "03-1111-2222",
        "address": "東京都新宿区1-1",
        "prefecture": "東京都",
        "medical_type": "医科",
        "uuid": "",
    }

    result = match_record(conn, record)

    assert result.status == "MATCHED"
    assert result.candidates == [10]
    assert result.reason == "電話番号キー完全一致"
    assert result.score == 100


def test_prefetched_matcher_keeps_duplicate_phone_ambiguous():
    conn = _PrefetchedMatchingConnection([
        _candidate(10, phone="03-1111-2222", name="甲医院"),
        _candidate(20, phone="03-1111-2222", name="乙医院"),
    ])
    record = {
        "clinic_name": "",
        "phone": "03-1111-2222",
        "address": "",
        "prefecture": "東京都",
        "medical_type": "医科",
        "uuid": "",
    }

    result = match_record(conn, record)

    assert result.status == "AMBIGUOUS"
    assert result.candidates == [10, 20]


def test_prefetched_matcher_updates_uuid_index_for_later_rows_in_same_batch():
    conn = _PrefetchedMatchingConnection([
        _candidate(10, uuid="", phone="03-1111-2222"),
    ])
    updated = conn.get(10)
    updated["uuid"] = "UUID-10"
    conn.upsert(updated)

    result = match_record(
        conn,
        {
            "clinic_name": "",
            "phone": "",
            "address": "",
            "prefecture": "",
            "medical_type": "",
            "uuid": "UUID-10",
        },
    )

    assert result.status == "MATCHED"
    assert result.candidates == [10]
    assert result.reason == "保存済み識別番号の更新"


def test_supabase_comdesk_import_uses_prefetch_and_bulk_write_fast_path():
    source = inspect.getsource(SupabaseClinicWriteRepository.import_comdesk)

    assert "_prefetch_comdesk_candidates" in source
    assert "_bulk_insert_comdesk_source_records" in source
    assert "_projection_values" in source
    assert "_bulk_update_projection" in source
    assert "_upsert_tx(" not in source
    assert "preflight_seconds" in source
