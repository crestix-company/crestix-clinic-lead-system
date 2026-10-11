"""Email Research UI integration into the main Clinic Lead System app (app_v2.py)."""
import app_v2


def test_nav_includes_email_research_without_removing_existing_items():
    assert "Email調査" in app_v2.NAV
    assert "かんたん操作" in app_v2.NAV
    assert "営業対象・出力" in app_v2.NAV
    assert "詳細設定" in app_v2.NAV


def test_email_status_label_primary_takes_precedence():
    assert app_v2._email_status_label({"has_primary": True, "research_status": "FOUND"}) == "Primary"


def test_email_status_label_review():
    assert app_v2._email_status_label({"has_primary": False, "research_status": "REVIEW"}) == "要確認"


def test_email_status_label_no_email_found_and_error_map_to_no_email():
    assert app_v2._email_status_label({"has_primary": False, "research_status": "NO_EMAIL_FOUND"}) == "取得なし"
    assert app_v2._email_status_label({"has_primary": False, "research_status": "ERROR"}) == "取得なし"


def test_email_status_label_unresearched_or_missing_is_not_yet_researched():
    assert app_v2._email_status_label(None) == "未調査"
    assert app_v2._email_status_label({"has_primary": False, "research_status": "NOT_RESEARCHED"}) == "未調査"


def test_display_rows_includes_email_status_column_defaulting_to_not_researched():
    records = [{"id": 1, "clinic_name": "テストクリニック", "uuid": "u1"}]
    frame = app_v2.display_rows(records)
    assert "Email Status" in frame.columns
    assert frame.iloc[0]["Email Status"] == "未調査"
