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
