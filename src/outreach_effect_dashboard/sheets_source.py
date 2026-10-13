"""READ ONLY Google Sheets source for the existing 案件管理 sheet.

Not connected or exercised against the real sheet anywhere in this codebase yet -- real-data
use requires explicit authorization (see docs/outreach_ab_tracking_design.md §6.3). This module
fails closed (raises immediately) if the required env vars are missing, exactly like
src/outreach/readonly_access.py does for the Clinic Master DB connection in the sibling PR.

Requests only the read-only scope; never requests write/edit access, and this module contains
no code path that calls a write/update API.
"""
from __future__ import annotations

import os
from datetime import datetime

from .models import SendRecord

CREDENTIALS_ENV_VAR = "OUTREACH_SHEETS_READONLY_CREDENTIALS"
SHEET_ID_ENV_VAR = "OUTREACH_SHEETS_ID"
READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"

# 案件管理シートの列（docs/outreach_ab_tracking_design.md §2.2）のうち、このダッシュボードが読む列のみ。
REQUIRED_RANGE = "案件管理!A:CE"


class SheetsAccessError(RuntimeError):
    """Required configuration is missing, or the sheet can't be read read-only. Caller must stop."""


def _require_config(environ=None) -> tuple[str, str]:
    env = os.environ if environ is None else environ
    credentials_path = (env.get(CREDENTIALS_ENV_VAR) or "").strip()
    sheet_id = (env.get(SHEET_ID_ENV_VAR) or "").strip()
    if not credentials_path or not sheet_id:
        raise SheetsAccessError(
            f"実データ連携は未許可です。{CREDENTIALS_ENV_VAR}（READ ONLYサービスアカウントの認証情報JSONパス）"
            f"と{SHEET_ID_ENV_VAR}（対象スプレッドシートID）の両方を設定してください。"
            "許可が整うまでは synthetic_source の合成データを使用します。"
        )
    return credentials_path, sheet_id


def fetch_raw_rows(environ=None):
    """Returns raw sheet rows for 案件管理. Raises SheetsAccessError if not configured.

    Never requests a write scope. Google API client is imported lazily so this module can be
    imported (and its fail-closed config check exercised) without the dependency installed.
    """
    credentials_path, sheet_id = _require_config(environ)
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    credentials = service_account.Credentials.from_service_account_file(
        credentials_path, scopes=[READONLY_SCOPE])
    service = build("sheets", "v4", credentials=credentials)
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=REQUIRED_RANGE).execute()
    return result.get("values", [])


_DATETIME_FORMATS = ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d")


def _parse_datetime(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _truthy(value: str) -> bool:
    return (value or "").strip() not in ("", "なし", "未検知", "未了承", "―")


def parse_rows_to_records(rows: list[list[str]]) -> list[SendRecord]:
    """Converts raw 案件管理 rows (header row first) into SendRecord.

    Column lookup is by header name, not position, since the exact real column order has not
    been independently verified against pipeline source (see design doc §1). Unknown/missing
    headers are simply absent from the parsed record (fields default to None/False), rather
    than raising, so a partially-matching real sheet still degrades gracefully instead of
    crashing the dashboard.
    """
    if not rows:
        return []
    header = rows[0]
    index = {name: i for i, name in enumerate(header)}

    def cell(row: list[str], name: str) -> str:
        i = index.get(name)
        return row[i] if i is not None and i < len(row) else ""

    records = []
    for row in rows[1:]:
        sent_at = _parse_datetime(cell(row, "送信日時"))
        records.append(SendRecord(
            case_id=cell(row, "案件ID"),
            is_staff=cell(row, "IS担当"),
            template_number=cell(row, "テンプレート"),
            sent=cell(row, "送信有無").strip() == "送信済み",
            sent_at=sent_at,
            replied=_truthy(cell(row, "返信有無")),
            reply_at=_parse_datetime(cell(row, "初回返信日時")),
            open_estimated=_truthy(cell(row, "開封推定レベル")),
            open_estimated_at=_parse_datetime(cell(row, "初回画像読込日時")),
            open_high_confidence=_truthy(cell(row, "高信頼開封")),
            open_high_confidence_at=_parse_datetime(cell(row, "初回高信頼シグナル日時")),
            clicked=_truthy(cell(row, "記事クリック有無")),
            clicked_at=_parse_datetime(cell(row, "初回クリック日時")),
            appointment=_truthy(cell(row, "メール経由アポ")),
            appointment_at=_parse_datetime(cell(row, "アポ日時")),
        ))
    return records
