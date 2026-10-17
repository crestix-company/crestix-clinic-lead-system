from pathlib import Path


def _app_source():
    return Path("app_v2.py").read_text(encoding="utf-8")


def test_simple_workflow_no_longer_contains_duplicate_comdesk_steps():
    source = _app_source()
    start = source.index("def simple_workflow_ui")
    end = source.index("def simple_sales_ui", start)
    simple = source[start:end]

    assert 'st.subheader("5. Comdesk形式で出力")' not in simple
    assert 'st.subheader("6. 5.で出したデータを手動でComdeskに入れる（＝UUIDを付与）")' not in simple
    assert 'st.subheader("7. 6.でUUIDが付与されたデータをこのアプリに取り込む")' not in simple
    assert 'key="simple_comdesk_upload"' not in simple
    assert 'key_prefix="simple_step5"' not in simple


def test_comdesk_export_and_import_remain_available_in_dedicated_pages():
    source = _app_source()

    sales_start = source.index("def simple_sales_ui")
    sales_end = source.index("def advanced_ui", sales_start)
    sales = source[sales_start:sales_end]
    assert 'st.subheader("営業対象をComdesk形式で出力")' in sales
    assert 'key="simple_sales_filtered_export"' in sales

    import_start = source.index("def import_ui")
    import_end = source.index("def ", import_start + 10)
    import_block = source[import_start:import_end]
    assert 'st.subheader("1. 既存コムデスクを取り込む")' in import_block
    assert 'key="comdesk_upload"' in import_block


def test_simple_workflow_keeps_step1_through_step7_in_order():
    source = _app_source()
    start = source.index("def simple_workflow_ui")
    end = source.index("def simple_sales_ui", start)
    simple = source[start:end]
    headings = [
        "Step1：既存Comdesk CSV・Excelを取り込む",
        "Step2：厚生局データを取り込む",
        "Step3：Google Maps Collector",
        "Step4：Google Maps結果を取り込む",
        "Step5：HP・Email自動調査",
        "Step6：Comdesk未連携医院CSVを出力",
        "Step7：UUID付与後のComdesk CSVを再取り込む",
    ]

    positions = [simple.index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert "render_unlinked_comdesk_export(store, demo)" in simple
    assert 'key_prefix="simple_comdesk_reimport"' in simple
