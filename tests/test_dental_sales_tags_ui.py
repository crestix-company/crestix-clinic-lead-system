from pathlib import Path


def test_dental_sales_ui_is_wired_into_advanced_settings():
    app = Path("app_v2.py").read_text(encoding="utf-8")
    assert "from src.dental_sales_tags_ui import dental_sales_tags_page" in app
    assert '"歯科営業タグ"' in app
    assert "dental_sales_tags_page(store)" in app


def test_dental_sales_ui_contains_priority_export_review_and_learning_tabs():
    source = Path("src/dental_sales_tags_ui.py").read_text(encoding="utf-8")
    assert '"①→⑨ 営業リスト"' in source
    assert '"Human Review"' in source
    assert '"学習・精度"' in source
    assert '"①→⑨順のCSVを作成"' in source
    assert '"カテゴリ別CSVをZIPで一括作成"' in source
    assert 'repo.save_review(' in source
    assert '"CONFIRMED"' in source
    assert '"REJECTED"' in source
    assert '"UNCERTAIN"' in source
    assert "repo.training_stats()" in source


def test_dental_sales_ui_uses_effective_human_overridden_list():
    source = Path("src/dental_sales_tags_ui.py").read_text(encoding="utf-8")
    assert "repo.effective_count(" in source
    assert "repo.list_effective_clinics(" in source
    repo_source = Path("src/repository/dental_sales_tag_repository.py").read_text(encoding="utf-8")
    assert "human_decision,'')<>'REJECTED'" in repo_source
    assert "r.human_decision='CONFIRMED'" in repo_source


def test_dental_priority_labels_match_business_order():
    from src.dental_sales_tags_ui import PRIORITY_LABELS

    assert PRIORITY_LABELS[1].startswith("① マウスピース矯正")
    assert PRIORITY_LABELS[2].startswith("② 矯正歯科")
    assert PRIORITY_LABELS[3].startswith("③ All-on-4")
    assert PRIORITY_LABELS[4].startswith("④ インプラント")
    assert PRIORITY_LABELS[5].startswith("⑤ セラミック")
    assert PRIORITY_LABELS[6].startswith("⑥ 審美歯科")
    assert PRIORITY_LABELS[7].startswith("⑦")
    assert PRIORITY_LABELS[8].startswith("⑧")
    assert PRIORITY_LABELS[9].startswith("⑨")


def test_exclusive_dental_category_assignment_follows_business_order():
    from src.dental_sales_tags_ui import _exclusive_category_key

    def row(*codes):
        return {"codes": " / ".join(codes)}

    assert _exclusive_category_key(row("ALIGNER_INVISALIGN", "ORTHODONTICS_GENERAL")) == "01"
    assert _exclusive_category_key(row("ORTHODONTICS_GENERAL")) == "02"
    assert _exclusive_category_key(row("ALL_ON_4", "IMPLANT_GENERAL")) == "03"
    assert _exclusive_category_key(row("IMPLANT_GENERAL")) == "04"
    assert _exclusive_category_key(row("CERAMIC", "AESTHETIC_GENERAL")) == "05"
    assert _exclusive_category_key(row("AESTHETIC_GENERAL")) == "06"
    assert _exclusive_category_key(row("ORAL_SURGERY", "WISDOM_TOOTH")) == "07a"
    assert _exclusive_category_key(row("AESTHETIC_GENERAL", "WHITENING")) == "07b"
    assert _exclusive_category_key(row("PRIVATE_DENTURE", "ROOT_CANAL")) == "08"
    assert _exclusive_category_key(row("GENERAL_DENTISTRY", "CHECKUP")) == "09"


def test_category_zip_contains_summary_and_all_category_csvs():
    import io
    import zipfile

    from src.dental_sales_tags_ui import CATEGORY_EXPORTS, _build_category_zip

    base = {
        "id": 1,
        "uuid": "u1",
        "clinic_name": "テスト歯科",
        "phone": "03-0000-0000",
        "address": "東京都",
        "prefecture": "東京都",
        "hp_url": "https://example.test/",
        "tag_label": "一般歯科",
        "tag_code": "GENERAL_DENTISTRY",
        "labels": "一般歯科",
        "confidence": 0.9,
        "source": "TEST",
        "rule_id": "TEST",
    }
    rows = [
        {**base, "id": 1, "codes": "ALIGNER_INVISALIGN / ORTHODONTICS_GENERAL"},
        {**base, "id": 2, "codes": "WHITENING / AESTHETIC_GENERAL"},
        {**base, "id": 3, "codes": "GENERAL_DENTISTRY / CHECKUP"},
    ]
    payload, counts = _build_category_zip(rows)

    assert counts["01"] == 1
    assert counts["07b"] == 1
    assert counts["09"] == 1
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = set(archive.namelist())
    assert "00_カテゴリ件数.csv" in names
    for _key, _label, filename in CATEGORY_EXPORTS:
        assert filename in names
