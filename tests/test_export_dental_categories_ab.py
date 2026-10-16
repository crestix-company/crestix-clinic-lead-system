from scripts.export_dental_categories_ab import category_evidence, category_for


def test_dental_category_priority_is_mutually_exclusive():
    assert category_for({"ALIGNER_INVISALIGN", "ORTHODONTICS_GENERAL"}) == "01"
    assert category_for({"ORTHODONTICS_GENERAL"}) == "02"
    assert category_for({"ALL_ON_4", "IMPLANT_GENERAL"}) == "03"
    assert category_for({"IMPLANT_GENERAL"}) == "04"
    assert category_for({"CERAMIC", "AESTHETIC_GENERAL"}) == "05"
    assert category_for({"AESTHETIC_GENERAL"}) == "06"
    assert category_for({"ORAL_SURGERY", "WHITENING", "AESTHETIC_GENERAL"}) == "07a"
    assert category_for({"WHITENING", "AESTHETIC_GENERAL"}) == "07b"
    assert category_for({"PRIVATE_DENTURE", "GENERAL_DENTISTRY"}) == "08"
    assert category_for({"GENERAL_DENTISTRY"}) == "09"


def test_specific_aesthetic_tags_do_not_collapse_into_general_aesthetic():
    assert category_for({"AESTHETIC_GENERAL", "WHITENING"}) == "07b"
    assert category_for({"AESTHETIC_GENERAL", "ZIRCONIA"}) == "07b"
    assert category_for({"AESTHETIC_GENERAL", "LAMINATE_VENEER"}) == "07b"


def test_general_category_evidence_falls_back_to_general_dentistry():
    assert category_evidence({"GENERAL_DENTISTRY"}, "09") == "一般歯科"


def test_export_uses_formal_30_column_comdesk_contract():
    from src.master.comdesk import COMDESK_EXPORT_HEADERS

    assert len(COMDESK_EXPORT_HEADERS) == 30
    assert COMDESK_EXPORT_HEADERS[-2:] == ["HPランク", "メールアドレス"]
    assert "営業カテゴリ" not in COMDESK_EXPORT_HEADERS
    assert "カテゴリ根拠タグ" not in COMDESK_EXPORT_HEADERS
