import json

from src.master.dental_sales_tags import (
    AUTO_CONFIRMED,
    AUTO_REVIEW,
    CLASSIFIER_VERSION,
    classify_dental_sales_tags,
    primary_dental_sales_tag,
)


def _by_code(tags):
    return {item.tag_code: item for item in tags}


def test_non_dental_never_gets_dental_tags_even_with_orthodontic_word():
    tags = classify_dental_sales_tags({
        "medical_type": "医科",
        "clinic_name": "矯正治療クリニック",
        "departments_json": ["皮膚科"],
    })
    assert tags == []


def test_every_dental_clinic_has_general_fallback():
    tags = classify_dental_sales_tags({
        "medical_type": "歯科",
        "clinic_name": "さとう歯科",
        "departments_json": ["歯科"],
    })
    by_code = _by_code(tags)
    assert by_code["GENERAL_DENTISTRY"].auto_status == AUTO_CONFIRMED
    assert by_code["GENERAL_DENTISTRY"].priority_group == 9
    assert primary_dental_sales_tag(tags).tag_code == "GENERAL_DENTISTRY"


def test_orthodontic_clinic_name_is_priority_two():
    tags = classify_dental_sales_tags({
        "medical_type": "歯科",
        "clinic_name": "中野矯正歯科クリニック",
        "departments_json": ["歯科"],
    })
    assert primary_dental_sales_tag(tags).tag_code == "ORTHODONTICS_GENERAL"
    assert primary_dental_sales_tag(tags).priority_group == 2


def test_invisalign_hp_heading_is_priority_one_and_adds_parent():
    page = {
        "url": "https://example-dental.jp/invisalign/",
        "title": "インビザライン・マウスピース矯正",
        "headings": "当院のインビザライン治療",
        "text": "当院ではインビザライン治療を行っています。",
    }
    tags = classify_dental_sales_tags(
        {"medical_type": "歯科", "clinic_name": "テスト歯科", "departments_json": ["歯科"]},
        [page],
    )
    by_code = _by_code(tags)
    assert by_code["ALIGNER_INVISALIGN"].auto_status == AUTO_CONFIRMED
    assert by_code["ORTHODONTICS_GENERAL"].source == "DERIVED"
    assert primary_dental_sales_tag(tags).tag_code == "ALIGNER_INVISALIGN"


def test_plain_body_mention_without_offer_context_stays_review():
    page = {
        "url": "https://example-dental.jp/blog/",
        "title": "歯科コラム",
        "headings": "最近の治療について",
        "text": "海外ではAll-on-4という考え方があります。",
    }
    tags = classify_dental_sales_tags(
        {"medical_type": "歯科", "clinic_name": "テスト歯科", "departments_json": ["歯科"]},
        [page],
    )
    by_code = _by_code(tags)
    assert by_code["ALL_ON_4"].auto_status == AUTO_REVIEW
    assert primary_dental_sales_tag(tags).tag_code == "GENERAL_DENTISTRY"


def test_all_on_4_offer_is_priority_three_and_implant_parent():
    page = {
        "url": "https://example-dental.jp/all-on-4/",
        "title": "All-on-4",
        "headings": "オールオン4",
        "text": "当院ではAll-on-4に対応しています。",
    }
    tags = classify_dental_sales_tags(
        {"medical_type": "歯科", "clinic_name": "テスト歯科", "departments_json": ["歯科"]},
        [page],
    )
    by_code = _by_code(tags)
    assert by_code["ALL_ON_4"].priority_group == 3
    assert by_code["IMPLANT_GENERAL"].priority_group == 4
    assert primary_dental_sales_tag(tags).tag_code == "ALL_ON_4"


def test_aesthetic_specific_tags_share_priority_group_seven():
    page = {
        "url": "https://example-dental.jp/aesthetic/",
        "title": "審美歯科",
        "headings": "ホワイトニング・ジルコニア・ラミネートベニア",
        "text": "当院では各治療をご案内しています。",
    }
    tags = classify_dental_sales_tags(
        {"medical_type": "歯科", "clinic_name": "テスト歯科", "departments_json": ["歯科"]},
        [page],
    )
    by_code = _by_code(tags)
    assert by_code["WHITENING"].priority_group == 7
    assert by_code["ZIRCONIA"].priority_group == 7
    assert by_code["LAMINATE_VENEER"].priority_group == 7
    assert by_code["AESTHETIC_GENERAL"].priority_group == 6


def test_private_denture_requires_self_pay_specific_alias():
    generic = {
        "url": "https://example-dental.jp/denture/",
        "title": "入れ歯",
        "headings": "入れ歯について",
        "text": "保険の入れ歯について説明します。",
    }
    specific = {
        "url": "https://example-dental.jp/private-denture/",
        "title": "自費入れ歯",
        "headings": "ノンクラスプデンチャー",
        "text": "当院ではノンクラスプデンチャーをご提供しています。",
    }
    record = {"medical_type": "歯科", "clinic_name": "テスト歯科", "departments_json": ["歯科"]}
    assert "PRIVATE_DENTURE" not in _by_code(classify_dental_sales_tags(record, [generic]))
    assert _by_code(classify_dental_sales_tags(record, [specific]))["PRIVATE_DENTURE"].priority_group == 8


def test_page_json_string_is_supported_for_backfill():
    page = {
        "page_json": json.dumps({
            "url": "https://example-dental.jp/root-canal/",
            "title": "精密根管治療",
            "headings": "マイクロスコープ根管治療",
            "text": "当院では精密根管治療を実施しています。",
        }, ensure_ascii=False),
    }
    tags = classify_dental_sales_tags(
        {"medical_type": "歯科", "clinic_name": "テスト歯科", "departments_json": ["歯科"]},
        [page],
    )
    assert _by_code(tags)["ROOT_CANAL"].auto_status == AUTO_CONFIRMED
    assert CLASSIFIER_VERSION.startswith("dental-sales-tags-")


def test_dental_sales_schema_is_private_and_rls_guarded():
    from pathlib import Path

    sql = Path("scripts/supabase_migration/dental_sales_tags_schema.sql").read_text(encoding="utf-8").lower()
    assert "alter table provenance.dental_sales_tags enable row level security" in sql
    assert "alter table provenance.dental_sales_tag_reviews enable row level security" in sql
    assert "grant select,insert,update on provenance.dental_sales_tags to clinic_runtime" in sql
    assert "grant select,insert on provenance.dental_sales_tag_reviews to clinic_runtime" in sql
    assert "revoke all on provenance.dental_sales_tags from anon,authenticated,public" in sql
    assert "revoke all on provenance.dental_sales_tag_reviews from anon,authenticated,public" in sql


def test_dental_sales_repository_is_wired_only_for_supabase_runtime():
    from pathlib import Path

    source = Path("src/repository/write_backend.py").read_text(encoding="utf-8")
    contracts = Path("src/repository/write_contracts.py").read_text(encoding="utf-8")
    assert "SupabaseDentalSalesTagRepository" in source
    assert "dental_sales_tags=SupabaseDentalSalesTagRepository(conn)" in source
    assert "dental_sales_tags: object | None = None" in contracts


def test_hp_worker_refreshes_dental_tags_without_changing_hp_outcome_contract():
    from pathlib import Path

    source = Path("src/master/jobs.py").read_text(encoding="utf-8")
    assert 'record.get("medical_type") == "歯科"' in source
    assert "classify_dental_sales_tags(record, pages or ())" in source
    assert "Dental sales tag refresh failed" in source
    assert "except Exception:" in source
