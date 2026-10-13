"""READ ONLY Google Sheets source for the existing 案件管理 sheet.

Not connected or exercised against the real sheet anywhere in this codebase yet -- real-data
use requires explicit authorization (see docs/outreach_ab_tracking_design.md §6.3). This module
fails closed (raises immediately) if the required env vars are missing, exactly like
src/outreach/readonly_access.py does for the Clinic Master DB connection in the sibling PR.

Two auth modes, so an existing Google identity can be reused instead of always minting a new
service account (see docs/outreach_ab_tracking_design.md §6.8 for the investigation this is
based on -- no reusable credential existed on this machine, but gcloud Application Default
Credentials is a lighter-weight option than a service account once the user runs one command):

- ADC (default, lower friction): ``gcloud auth application-default login`` as the user's own
  Google account. Reuses whatever Sheets access that account already has (this account already
  has Viewer access to the target sheet via an existing share) -- no new sharing step needed.
- Service account (opt-in via OUTREACH_SHEETS_READONLY_CREDENTIALS): for unattended/non-interactive
  use. Requires creating a service account and sharing the sheet with it separately.

Requests only the read-only scope; never requests write/edit access, and this module contains
no code path that calls a write/update API.
"""
from __future__ import annotations

import os
from datetime import datetime

from .models import SendRecord

CREDENTIALS_ENV_VAR = "OUTREACH_SHEETS_READONLY_CREDENTIALS"  # service account key path (optional)
SHEET_ID_ENV_VAR = "OUTREACH_SHEETS_ID"  # required either way
READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"

# 案件管理シートの列（docs/outreach_ab_tracking_design.md §2.2）のうち、このダッシュボードが読む列のみ。
REQUIRED_RANGE = "案件管理!A:CE"


class SheetsAccessError(RuntimeError):
    """Required configuration is missing, or the sheet can't be read read-only. Caller must stop."""


def _require_sheet_id(environ=None) -> str:
    env = os.environ if environ is None else environ
    sheet_id = (env.get(SHEET_ID_ENV_VAR) or "").strip()
    if not sheet_id:
        raise SheetsAccessError(
            f"実データ連携は未許可です。{SHEET_ID_ENV_VAR}（対象スプレッドシートID）を設定し、"
            f"いずれかの認証方式を用意してください: "
            f"(A) `gcloud auth application-default login --scopes={READONLY_SCOPE}` を実行する"
            f"（既存のGoogleアカウントの閲覧権限をそのまま使う、新規共有不要）、"
            f"(B) {CREDENTIALS_ENV_VAR} にREAD ONLYサービスアカウントの認証情報JSONパスを設定する。"
            "許可が整うまでは synthetic_source の合成データを使用します。"
        )
    return sheet_id


def _require_config(environ=None) -> tuple[str, str]:
    """Backward-compat helper: requires the service-account path specifically (used by existing
    tests/callers that assert the service-account mode's own config error). ADC mode does not
    go through this function -- see fetch_raw_rows().
    """
    env = os.environ if environ is None else environ
    credentials_path = (env.get(CREDENTIALS_ENV_VAR) or "").strip()
    sheet_id = _require_sheet_id(environ)
    if not credentials_path:
        raise SheetsAccessError(
            f"{CREDENTIALS_ENV_VAR}（READ ONLYサービスアカウントの認証情報JSONパス）が未設定です。"
        )
    return credentials_path, sheet_id


def _build_credentials(environ=None):
    """Returns (credentials, mode_label). Tries ADC first (reuses an existing Google identity,
    e.g. via ``gcloud auth application-default login``), falls back to a service-account key
    file only if OUTREACH_SHEETS_READONLY_CREDENTIALS is explicitly set.
    """
    env = os.environ if environ is None else environ
    credentials_path = (env.get(CREDENTIALS_ENV_VAR) or "").strip()
    if credentials_path:
        from google.oauth2 import service_account
        return service_account.Credentials.from_service_account_file(
            credentials_path, scopes=[READONLY_SCOPE]), "service_account"
    import google.auth
    credentials, _project = google.auth.default(scopes=[READONLY_SCOPE])
    return credentials, "adc"


def fetch_raw_rows(environ=None, *, range_name: str = REQUIRED_RANGE):
    """Returns raw sheet rows. Raises SheetsAccessError if not configured, or if the read
    itself fails for any reason (missing dependency, no ADC/bad credentials file, insufficient
    permission, network error, etc.) -- the caller must be able to treat every failure mode
    the same way (fall back to synthetic data), never crash the page with a raw traceback.

    Never requests a write scope. Google API client is imported lazily so this module can be
    imported (and its fail-closed config check exercised) without the dependency installed.
    """
    sheet_id = _require_sheet_id(environ)
    try:
        from googleapiclient.discovery import build

        credentials, _mode = _build_credentials(environ)
        service = build("sheets", "v4", credentials=credentials)
        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=range_name).execute()
        return result.get("values", [])
    except SheetsAccessError:
        raise
    except Exception as exc:
        # 例外メッセージ自体に認証情報やファイル内容が含まれないことはライブラリ側の実装に依存するため
        # 信頼しすぎない。ここでは例外の型名のみを安全な情報として伝え、詳細はUIに出さない。
        raise SheetsAccessError(
            f"Google Sheetsの読み取りに失敗しました（{type(exc).__name__}）。"
            "認証情報・共有権限・ネットワークを確認してください。許可が整うまたは復旧するまでは"
            "synthetic_sourceの合成データを使用します。"
        ) from exc


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
