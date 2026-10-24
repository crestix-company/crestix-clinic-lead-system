"""Read-only loader and preview helpers for a local Google Sheets export."""
from __future__ import annotations

from collections import Counter
import csv
from dataclasses import dataclass
import io
from pathlib import Path
from typing import Any, Iterable, Mapping

import openpyxl
import re


CASE_TABS = ("案件管理", "メール管理", "328医院_現在進捗_20261018")
RELATED_TABS = (
    "日程候補ログ", "トラッキングログ", "v3下書き管理",
    "記事掲載_Gmail照合_20261016", "328医院_下書き送信予約照合_20261016",
    "Gmail統合管理_20261019", "Gmail現況照合_20261019", "ClinicLead_GAS連携",
)


@dataclass(frozen=True)
class SheetSnapshotTab:
    name: str
    headers: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    source_rows: tuple[int, ...]


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def read_workbook_snapshot(path: str | Path) -> tuple[SheetSnapshotTab, ...]:
    """Read every nonempty worksheet and retain the original row and cell order."""
    workbook = openpyxl.load_workbook(Path(path), read_only=True, data_only=False)
    tabs: list[SheetSnapshotTab] = []
    try:
        for sheet in workbook.worksheets:
            raw_rows = list(sheet.iter_rows(values_only=True))
            while raw_rows and not any(value is not None for value in raw_rows[-1]):
                raw_rows.pop()
            if not raw_rows:
                tabs.append(SheetSnapshotTab(sheet.title, (), (), ()))
                continue
            headers = tuple(_cell_text(value) for value in raw_rows[0])
            # Keep internal empty columns because they are part of the source row shape.
            width = len(headers)
            data_rows = tuple(tuple(row[:width]) + (None,) * max(0, width-len(row))
                              for row in raw_rows[1:])
            tabs.append(SheetSnapshotTab(sheet.title, headers, data_rows,
                                         tuple(range(2, len(data_rows) + 2))))
    finally:
        workbook.close()
    return tuple(tabs)


def read_csv_snapshot(filename: str, payload: bytes) -> SheetSnapshotTab:
    """Parse an uploaded CSV without losing field order or its original source line."""
    try:
        content = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        content = payload.decode("cp932")
    reader = csv.reader(io.StringIO(content, newline=""))
    headers = tuple(next(reader, ()))
    records, source_rows = [], []
    previous_line = reader.line_num
    for row in reader:
        source_rows.append(previous_line + 1)
        previous_line = reader.line_num
        records.append(tuple(row[:len(headers)]) + (None,) * max(0, len(headers) - len(row)))
    return SheetSnapshotTab(Path(filename).stem, headers, tuple(records), tuple(source_rows))


def preview_import_actions(
    tabs: Iterable[SheetSnapshotTab], *, known_uuid_by_clinic_id: Mapping[str, str],
    existing_uuid_by_campaign: Mapping[tuple[str, str], Mapping[str, str]] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Build a no-write preview; missing/ambiguous mappings and duplicates are HOLD."""
    existing_uuid_by_campaign = existing_uuid_by_campaign or {}
    result: list[dict[str, Any]] = []
    for tab in tabs:
        headers = tab.headers
        records = [dict(zip(headers, row)) for row in tab.rows]
        clinic_ids = [_cell_text(next((r.get(k) for k in ("clinic_id", "Clinic ID", "管理ID")
                                       if r.get(k) not in (None, "")), "")) for r in records]
        uuids = [_cell_text(next((r.get(k) for k in ("Clinic UUID", "clinic_uuid", "Clinic UUID（参照）")
                                 if r.get(k) not in (None, "")), "")) for r in records]
        id_counts, uuid_counts = Counter(v for v in clinic_ids if v), Counter(v.casefold() for v in uuids if v)
        for offset, record in enumerate(records):
            clinic_id, clinic_uuid = clinic_ids[offset], uuids[offset]
            source_row = tab.source_rows[offset]
            case_id = _cell_text(next((record.get(k) for k in ("案件ID", "GAS案件ID", "gas_case_id")
                                       if record.get(k) not in (None, "")), ""))
            campaign = _cell_text(next((record.get(k) for k in ("campaign_id", "キャンペーンID")
                                        if record.get(k) not in (None, "")), ""))
            if clinic_id and id_counts[clinic_id] > 1:
                action, reason = "HOLD", "タブ内のClinic ID重複"
            elif clinic_uuid and uuid_counts[clinic_uuid.casefold()] > 1:
                action, reason = "HOLD", "タブ内のClinic UUID重複"
            elif not clinic_uuid and clinic_id and known_uuid_by_clinic_id.get(clinic_id):
                clinic_uuid = known_uuid_by_clinic_id[clinic_id]
                action, reason = "INSERT", "既存の読取専用照合タブでClinic ID/UUIDが一意一致"
            elif clinic_uuid and clinic_id and known_uuid_by_clinic_id.get(clinic_id) != clinic_uuid:
                action, reason = "HOLD", "Clinic ID/UUIDの対応が不一致"
            elif not clinic_uuid or not clinic_id:
                action, reason = "HOLD", "Clinic ID/UUIDの一意な対応を確認できない"
            else:
                key = (clinic_uuid.casefold(), campaign)
                current = existing_uuid_by_campaign.get(key)
                if current is None:
                    action, reason = "INSERT", "対応UUIDを確認。ローカル台帳に未登録"
                elif current.get("source_fingerprint") == repr(tuple(record.values())):
                    action, reason = "NOOP", "ローカル台帳と差分なし"
                else:
                    action, reason = "UPDATE", "ローカル台帳に差分あり"
            result.append({"action": action, "source_tab": tab.name, "source_row": source_row,
                           "clinic_id": clinic_id, "clinic_uuid": clinic_uuid, "gas_case_id": case_id,
                           "raw_values": tuple(tab.rows[offset]), "headers": headers, "reason": reason})
    return tuple(result)


def import_action_counts(actions: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts = Counter(str(action.get("action", "HOLD")) for action in actions)
    return {name: counts.get(name, 0) for name in ("INSERT", "UPDATE", "NOOP", "HOLD")}


_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)


def _emails(value: Any) -> set[str]:
    return {item.casefold() for item in _EMAIL_RE.findall(str(value or ""))}


def _id_values(value: Any) -> set[str]:
    return {token.strip().strip("<>") for token in re.split(r"[,;\s]+", str(value or "")) if token.strip()}


def reconcile_mail_state(row: Mapping[str, Any], gmail_snapshot: Mapping[str, Any]) -> dict[str, str]:
    """Use exact Gmail identifiers first; never infer state from a missing Draft alone."""
    draft_id = _cell_text(next((row.get(k) for k in ("下書きID", "Draft ID", "Gmail Draft ID（Sheets）")
                                if row.get(k) not in (None, "")), ""))
    message_ids = set()
    thread_ids = set()
    for key in ("下書きThreadID", "下書きThread ID", "ThreadID", "送信ThreadID", "送信Thread ID",
                "Gmail Thread ID（Sheets）", "Thread ID", "Sent Message ID", "Scheduled Message ID"):
        message_ids |= _id_values(row.get(key))
    for key in ("下書きID", "旧Draft ID", "Draft ID", "送信ThreadID", "送信Thread ID", "ThreadID",
                "Thread ID", "Sent Message ID", "Scheduled Message ID"):
        thread_ids |= _id_values(row.get(key))

    reply_flag = _cell_text(row.get("返信有無"))
    sheet_text = " ".join(_cell_text(row.get(k)) for k in ("状態", "送信有無", "返信有無", "現況（2026/10/18）", "Gmail現況") if row.get(k))
    sheet_lower = sheet_text.casefold()
    if reply_flag == "あり" or any(x in sheet_lower for x in ("返信あり", "replied", "返信済")):
        sheet_state = "REPLIED"
    elif any(x in sheet_lower for x in ("送信予約中", "予約送信済み", "scheduled")):
        sheet_state = "SCHEDULED"
    elif any(x in sheet_lower for x in ("送信済", "sent")):
        sheet_state = "SENT"
    elif any(x in sheet_lower for x in ("下書き", "draft", "未送信")):
        sheet_state = "DRAFT"
    elif "trash" in sheet_lower or "ゴミ箱" in sheet_lower:
        sheet_state = "TRASH"
    else:
        sheet_state = "UNKNOWN"

    drafts = {str(x.get("draft_id", "")): x for x in gmail_snapshot.get("drafts", [])}
    evidence: set[str] = {"DRAFT"} if draft_id and draft_id in drafts else set()
    searches = gmail_snapshot.get("searches", {})
    for state, values in searches.items():
        for message in values:
            mid, tid = str(message.get("id", "")), str(message.get("thread_id", ""))
            if mid in message_ids or tid in thread_ids or (draft_id and mid == draft_id):
                evidence.add({"sent": "SENT", "replied": "REPLIED", "trash": "TRASH", "scheduled": "SCHEDULED"}.get(state, "UNKNOWN"))
    # A reply on an exact thread is the most advanced verified state.
    if evidence == {"SENT", "REPLIED"}:
        gmail_state = "REPLIED"
    elif len(evidence) == 1:
        gmail_state = next(iter(evidence))
    else:
        gmail_state = "UNKNOWN"
    match = "一致" if gmail_state == sheet_state and gmail_state != "UNKNOWN" else "REVIEW"
    reason = "" if match == "一致" else (
        "Draft ID不在。Scheduled/Sent/Trash/返信の一意なID照合が未確定" if gmail_state == "UNKNOWN"
        else f"Sheets={sheet_state} / Gmail={gmail_state}"
    )
    return {"gmail_state": gmail_state, "sheet_state": sheet_state, "match": match, "hold_reason": reason}
