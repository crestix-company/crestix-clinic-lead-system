from collections import Counter

from src.master.comdesk import COMDESK_HEADERS
from src.master.filters import Filters
from src.master.sales_treatments import (
    match_pair, matching_pairs, prune_selected_pairs, sales_treatment_master,
)
from src.master.samples import sample_records
from src.master.store import ClinicStore
from src.normalizer.departments import normalize_departments


def evidence(category, keyword, source="HOME_MENU"):
    return {"category": category, "keyword": keyword, "source": source, "confidence": .98}


def test_sales_master_has_all_112_treatments_and_expected_statuses():
    master = sales_treatment_master()
    assert len(master) == 9
    assert sum(map(len, master.values())) == 112
    assert Counter(x.support_status for values in master.values() for x in values) == {
        "EXACT": 10, "ALIAS": 70, "MISSING": 31, "AMBIGUOUS": 1,
    }


def test_beauty_department_canonical_is_distinct_and_beauty_dermatology_stays_skin():
    assert normalize_departments("美容整形外科") == ["美容整形外科"]
    assert normalize_departments("美容整形外科 整形外科") == ["美容整形外科", "整形外科"]
    assert normalize_departments("美容皮膚科") == ["皮膚科"]


def test_keyword_pair_requires_exact_hp_evidence():
    departments = ["皮膚科"]
    categories = ["ニキビ・ニキビ跡"]
    assert match_pair(departments, categories, [evidence("ニキビ・ニキビ跡", "ポテンツァ")],
                      "皮膚科", "ポテンツァ").matched
    assert not match_pair(departments, categories, [], "皮膚科", "ポテンツァ").matched
    assert match_pair(["消化器内科"], ["内視鏡"], [evidence("内視鏡", "胃カメラ")],
                      "消化器内科", "胃カメラ").matched
    assert not match_pair(["消化器内科"], ["内視鏡"], [evidence("内視鏡", "内視鏡")],
                          "消化器内科", "鎮静剤 内視鏡").matched


def test_clinic_name_and_unknown_sources_fail_safe():
    for source in ("CLINIC_NAME", "", None):
        result = match_pair(["皮膚科"], ["ニキビ・ニキビ跡"],
                            [evidence("ニキビ・ニキビ跡", "ポテンツァ", source)],
                            "皮膚科", "ポテンツァ")
        assert not result.matched


def test_overlap_can_return_two_pairs():
    selected = [("皮膚科", "眼瞼下垂手術"), ("美容整形外科", "眼瞼下垂手術")]
    matches = matching_pairs(
        ["皮膚科", "美容整形外科"], ["日帰り手術"],
        [evidence("日帰り手術", "眼瞼下垂手術")], selected,
    )
    assert {(m.department, m.treatment) for m in matches} == set(selected)


def _clinic(i, name, departments):
    row = dict(sample_records()[0])
    row.update({
        "clinic_id": f"sales-{i}", "clinic_name": name, "phone": f"03-2222-{i:04d}",
        "address": f"東京都千代田区pair町{i}-1", "departments": departments,
        "facility_type": "診療所", "medical_type": "医科",
    })
    return row


def test_sql_filter_is_or_of_pairs_without_cartesian_leak_and_results_are_distinct(tmp_path):
    store = ClinicStore(tmp_path / "pair.db")
    store.import_master([
        _clinic(1, "両方一致", "皮 美容整形外科"),
        _clinic(2, "皮膚科だけ胃", "皮"),
        _clinic(3, "消化器だけポテンツァ", "消"),
    ])
    all_rows = store.query(Filters(active_only=False, hp_only=False), limit=100)
    ids = {r["clinic_name"]: r["id"] for r in all_rows}
    store.save_research(ids["両方一致"], {
        "hp_status": "VERIFIED", "hp_url": "https://one.example/",
        "treatment_categories": ["日帰り手術"],
        "treatment_evidence": [evidence("日帰り手術", "眼瞼下垂手術")],
    })
    store.save_research(ids["皮膚科だけ胃"], {
        "hp_status": "VERIFIED", "hp_url": "https://two.example/",
        "treatment_categories": ["内視鏡"],
        "treatment_evidence": [evidence("内視鏡", "胃カメラ")],
    })
    store.save_research(ids["消化器だけポテンツァ"], {
        "hp_status": "VERIFIED", "hp_url": "https://three.example/",
        "treatment_categories": ["ニキビ・ニキビ跡"],
        "treatment_evidence": [evidence("ニキビ・ニキビ跡", "ポテンツァ")],
    })

    overlap = [("皮膚科", "眼瞼下垂手術"), ("美容整形外科", "眼瞼下垂手術")]
    result = store.query(Filters(active_only=False, hp_only=False, sales_pairs=overlap), limit=100)
    assert [r["clinic_name"] for r in result] == ["両方一致"]
    assert len(result) == 1
    assert {(p["department"], p["treatment"]) for p in result[0]["matched_pairs"]} == set(overlap)

    cartesian = [("皮膚科", "ポテンツァ"), ("消化器内科", "胃カメラ")]
    assert store.query(Filters(active_only=False, hp_only=False, sales_pairs=cartesian), limit=100) == []


def test_department_deselect_prunes_only_removed_department():
    selected = [("皮膚科", "ポテンツァ"), ("美容整形外科", "眼瞼下垂手術")]
    assert prune_selected_pairs(selected, ["皮膚科"]) == [("皮膚科", "ポテンツァ")]


def test_comdesk_contract_is_still_28_columns():
    assert len(COMDESK_HEADERS) == 28
