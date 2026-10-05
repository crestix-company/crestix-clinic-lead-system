from scripts.export_sales_list_to_comdesk import (
    COMDESK_HEADERS,
    apply_filters,
    apply_sales_guard,
    parse_args,
    to_clinic_level,
    to_clinic_treatment_level,
    to_comdesk_row,
)


def make_row(**overrides):
    row = {
        "clinic_id": 1, "uuid": "uuid-1", "clinic_name": "テスト眼科",
        "department": "眼科", "crestix_department": "眼科",
        "sales_tier": "VERIFIED_TREATMENT", "sales_category": "白内障手術",
        "treatment_category": "白内障手術", "treatment_categories_full": ["白内障手術", "ICL手術"],
        "confidence": "HIGH", "sales_usable": "true", "human_verified": "true",
        "human_review_needed": "false", "evidence_url": "https://example.jp/",
        "decision_reason": "test", "phone": "03-1234-5678", "address": "東京都千代田区1-1",
        "prefecture": "東京都", "hp_url": "https://example.jp/",
    }
    row.update(overrides)
    return row


def test_filter_by_crestix_department():
    rows = [make_row(crestix_department="眼科"), make_row(clinic_id=2, crestix_department="皮膚科")]
    args = parse_args(["--crestix-department", "眼科"])
    assert [r["clinic_id"] for r in apply_filters(rows, args)] == [1]


def test_filter_by_treatment_category_checks_full_list():
    rows = [make_row(treatment_category="白内障手術", treatment_categories_full=["白内障手術", "ICL手術"])]
    args = parse_args(["--treatment-category", "ICL手術"])
    assert len(apply_filters(rows, args)) == 1


def test_filter_nonexistent_category_returns_empty():
    rows = [make_row()]
    args = parse_args(["--treatment-category", "存在しない治療"])
    assert apply_filters(rows, args) == []


def test_sales_guard_excludes_unusable():
    rows = [make_row(sales_usable="true"), make_row(clinic_id=2, sales_usable="false")]
    out = apply_sales_guard(rows, exclude_human_review=False)
    assert [r["clinic_id"] for r in out] == [1]


def test_sales_guard_exclude_human_review_flag():
    rows = [make_row(human_review_needed="true"), make_row(clinic_id=2, human_review_needed="false")]
    out = apply_sales_guard(rows, exclude_human_review=True)
    assert [r["clinic_id"] for r in out] == [2]
    out_default = apply_sales_guard(rows, exclude_human_review=False)
    assert len(out_default) == 2


def test_clinic_level_joins_categories_with_pipe():
    rows = [make_row(treatment_categories_full=["白内障手術", "硝子体手術", "緑内障治療"])]
    out = to_clinic_level(rows)
    assert out[0]["treatment_categories_joined"] == "白内障手術|硝子体手術|緑内障治療"


def test_clinic_level_no_duplicate_clinic_ids():
    rows = [make_row(clinic_id=1), make_row(clinic_id=2)]
    out = to_clinic_level(rows)
    assert len({r["clinic_id"] for r in out}) == len(out) == 2


def test_clinic_treatment_level_expands_one_row_per_category():
    rows = [make_row(treatment_categories_full=["白内障手術", "ICL手術"])]
    out = to_clinic_treatment_level(rows)
    assert len(out) == 2
    assert {r["treatment_category_row"] for r in out} == {"白内障手術", "ICL手術"}


def test_clinic_treatment_level_falls_back_to_single_category_when_no_full_list():
    rows = [make_row(treatment_category="白内障手術", treatment_categories_full=[])]
    out = to_clinic_treatment_level(rows)
    assert len(out) == 1
    assert out[0]["treatment_category_row"] == "白内障手術"


def test_to_comdesk_row_uses_existing_header_schema_only():
    row = make_row()
    out = to_comdesk_row(row)
    assert len(out) == len(COMDESK_HEADERS)
    assert out[COMDESK_HEADERS.index("UUID")] == "uuid-1"
    assert out[COMDESK_HEADERS.index("名前")] == "テスト眼科"
    assert out[COMDESK_HEADERS.index("Tel1")] == "03-1234-5678"
    assert out[COMDESK_HEADERS.index("URL")] == "https://example.jp/"
    # fields with no mapped source stay blank - never guessed
    assert out[COMDESK_HEADERS.index("備考")] == ""
    assert out[COMDESK_HEADERS.index("院長名")] == ""


def test_to_comdesk_row_blank_when_fields_missing():
    row = make_row(uuid="", phone="", hp_url="")
    out = to_comdesk_row(row)
    assert out[COMDESK_HEADERS.index("UUID")] == ""
    assert out[COMDESK_HEADERS.index("Tel1")] == ""
    assert out[COMDESK_HEADERS.index("URL")] == ""
