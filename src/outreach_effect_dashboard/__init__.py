"""READ ONLY effect-measurement dashboard for the existing Gmail outreach pipeline.

Reads the existing 「第一営業部_メール日程調整管理」Google Sheet (案件管理 sheet) as the sole
data source. Never writes to the sheet, never touches Gmail, never changes the existing
pipeline. See docs/outreach_ab_tracking_design.md for the design this implements.
"""
