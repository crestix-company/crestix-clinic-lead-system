import pytest

from src.sales_entity_dedupe import plan_sales_entities, approved_representative_ids


def row(i, kind, uuid="", name="same", address="same", phone="0311112222"):
    return dict(id=i, medical_type=kind, uuid=uuid, name_norm=name,
                address_norm=address, phone_norm=phone)


def test_cross_type_same_destination_single_export_and_uuid_preserved():
    result = plan_sales_entities([row(7, "医科"), row(9, "歯科", uuid="existing")])
    assert len(result) == 1
    assert result[0].decision == "COLLAPSE"
    assert result[0].representative_id == 9
    assert result[0].preserved_uuid == "existing"
    assert approved_representative_ids(result) == [9]


def test_different_phones_remain_separate():
    result = plan_sales_entities([row(7, "医科"), row(9, "歯科", phone="0399999999")])
    assert [d.decision for d in result] == ["SINGLE", "SINGLE"]


def test_conflicting_uuid_is_hold_not_overwritten():
    result = plan_sales_entities([row(7, "医科", uuid="A"), row(9, "歯科", uuid="B")])
    assert result[0].decision == "HOLD"
    with pytest.raises(ValueError, match="review required"):
        approved_representative_ids(result)


def test_same_type_duplicate_is_hold():
    result = plan_sales_entities([row(7, "医科"), row(9, "医科")])
    assert result[0].decision == "HOLD"


def test_missing_phone_is_never_collapsed():
    result = plan_sales_entities([row(7, "医科", phone=""), row(9, "歯科", phone="")])
    assert len(result) == 2


def test_duplicate_input_ids_fail_closed():
    with pytest.raises(ValueError, match="duplicate input"):
        plan_sales_entities([row(7, "医科"), row(7, "歯科")])
