"""Fail-closed reconciliation and isolated idempotency ledger for outreach.

External history is accepted only as normalized CSV with the documented exact headers;
we never guess spreadsheet or form columns. The SQLite ledger is an explicitly supplied
path and is never used for Clinic Master / Email Research or as a connection fallback.
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
import sqlite3
import uuid
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

HISTORY_COLUMNS = {
    "clinic_id", "clinic_uuid", "outreach_id", "campaign_id", "generation_batch_id",
    "gmail_draft_id", "gmail_message_id", "sent_at", "owner", "source", "status",
}
BLOCKING_STATUSES = {"DRAFTED", "SENT", "REPLY_RECEIVED", "APPOINTMENT", "EXCLUDED", "HOLD"}
ALLOWED_STATUSES = BLOCKING_STATUSES | {"NOT_STARTED", "ARTICLE_VERIFIED", "GENERATED"}


class HistoryInputError(ValueError):
    pass


@dataclass(frozen=True)
class HistoryMatch:
    clinic_id: int
    clinic_uuid: str
    campaign_id: str
    status: str
    source: str
    outreach_id: str = ""


def parse_history_csv(data: bytes | str) -> list[HistoryMatch]:
    text = data.decode("utf-8-sig") if isinstance(data, bytes) else data
    reader = csv.DictReader(io.StringIO(text))
    headers = set(reader.fieldnames or [])
    if not HISTORY_COLUMNS.issubset(headers):
        missing = sorted(HISTORY_COLUMNS - headers)
        raise HistoryInputError("履歴CSVの標準列が不足しています: " + ", ".join(missing))
    rows = []
    for line, row in enumerate(reader, 2):
        try:
            clinic_id = int((row.get("clinic_id") or "").strip())
        except (TypeError, ValueError) as exc:
            raise HistoryInputError(f"{line}行目のclinic_idが不正です") from exc
        status = ((row.get("outreach_status") or "").strip()
                  or (row.get("status") or "").strip()).upper()
        if status not in ALLOWED_STATUSES:
            raise HistoryInputError(f"{line}行目のstatusが不正です")
        if clinic_id <= 0 or not (row.get("campaign_id") or "").strip():
            raise HistoryInputError(f"{line}行目のclinic_id/campaign_idが不足しています")
        rows.append(HistoryMatch(clinic_id, (row.get("clinic_uuid") or "").strip(), row["campaign_id"].strip(), status,
                                  (row.get("source") or "").strip(),
                                  (row.get("outreach_id") or "").strip()))
    return rows


def reconcile_history(clinic_id: int, clinic_uuid: str, campaign_id: str,
                      rows: Iterable[HistoryMatch]) -> tuple[str, str]:
    """Exact Clinic ID first; UUID-only/multiple/nonidentical matches go to HOLD."""
    rows = [row for row in rows if row.campaign_id == campaign_id]
    if not rows:
        return "HOLD", "Campaign履歴が空または未取込"
    exact_id = [row for row in rows if row.clinic_id == int(clinic_id)]
    if exact_id:
        if any(row.clinic_uuid and clinic_uuid and row.clinic_uuid.casefold() != clinic_uuid.casefold()
               for row in exact_id):
            return "HOLD", "Clinic IDとUUIDが不一致"
        statuses = {row.status for row in exact_id}
        status = next(iter(statuses)) if len(statuses) == 1 else "HOLD"
        return ("NOT_STARTED", "clinic_id exact") if status == "NOT_STARTED" \
            else (status, "clinic_id exact" if len(statuses) == 1 else "履歴状態が競合")
    uuid_rows = [row for row in rows if clinic_uuid and row.clinic_uuid.casefold() == clinic_uuid.casefold()]
    if uuid_rows:
        statuses = {row.status for row in uuid_rows}
        status = next(iter(statuses)) if len(statuses) == 1 else "HOLD"
        return ("NOT_STARTED", "Clinic UUID exact") if status == "NOT_STARTED" \
            else (status, "Clinic UUID exact" if len(statuses) == 1 else "Clinic UUID履歴状態が競合")
    return "HOLD", "履歴未照合"


def email_collisions(rows: Iterable[tuple[int, str]]) -> set[int]:
    """Return all clinics sharing an exact normalized email; every member is held."""
    from collections import defaultdict
    groups = defaultdict(set)
    for clinic_id, email in rows:
        key = unicodedata.normalize("NFKC", email or "").strip().casefold()
        if key:
            groups[key].add(int(clinic_id))
    return {clinic_id for members in groups.values() if len(members) > 1 for clinic_id in members}


class OutreachLedger:
    """An explicit, separate local ledger; it is never a fallback for production data."""

    def __init__(self, path: str):
        if not path:
            raise ValueError("明示的なOUTREACH_STATE_DB_PATHが必要です")
        if str(path).lower().startswith(("postgres://", "postgresql://")):
            raise ValueError("OUTREACH_STATE_DB_PATHには独立したSQLiteファイルパスが必要です")
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            raise ValueError("OUTREACH_STATE_DB_PATHは絶対パスで指定してください")
        candidate = candidate.resolve()
        for variable in ("CLINIC_DB_PATH", "CLINIC_DEMO_DB_PATH"):
            configured = (os.environ.get(variable) or "").strip()
            if configured and candidate == Path(configured).expanduser().resolve():
                raise ValueError("送付履歴台帳はClinic Master/サンプルDBと別ファイルにしてください")
        if candidate.exists():
            conn = None
            try:
                conn = sqlite3.connect(f"file:{candidate}?mode=ro", uri=True)
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if tables & {"clinics", "comdesk_original_rows", "research_jobs", "email_candidates"}:
                    raise ValueError("Clinic Master/Email Researchデータベースを送付履歴台帳に使用できません")
            except sqlite3.DatabaseError as exc:
                raise ValueError("既存ファイルが独立したSQLite台帳として確認できません") from exc
            finally:
                if conn is not None:
                    conn.close()
        self.path = str(candidate)
        self._initialize()

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10, isolation_level="IMMEDIATE")

    def _initialize(self):
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS outreach_records (
                outreach_id TEXT PRIMARY KEY, clinic_id INTEGER NOT NULL, clinic_uuid TEXT NOT NULL,
                campaign_id TEXT NOT NULL, generation_batch_id TEXT NOT NULL,
                status TEXT NOT NULL, gmail_draft_id TEXT NOT NULL DEFAULT '',
                gmail_message_id TEXT NOT NULL DEFAULT '', sent_at TEXT NOT NULL DEFAULT '',
                owner TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
                idempotency_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
                UNIQUE(clinic_id,campaign_id))""")

    def campaign_statuses(self, campaign_id: str) -> dict[int, str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT clinic_id,status FROM outreach_records WHERE campaign_id=?",
                                (campaign_id,)).fetchall()
        return {int(cid): str(status) for cid, status in rows}

    def record_generation(self, *, clinic_id: int, clinic_uuid: str, campaign_id: str,
                          generation_batch_id: str, owner: str = "") -> tuple[str, bool]:
        return self.create_once(clinic_id=clinic_id, clinic_uuid=clinic_uuid,
                                campaign_id=campaign_id, generation_batch_id=generation_batch_id,
                                status="GENERATED", owner=owner, source="UI")

    def record_gmail_metadata(self, *, clinic_id: int, campaign_id: str, status: str,
                              gmail_draft_id: str = "", gmail_message_id: str = "",
                              sent_at: str = "", owner: str = "", source: str = "manual_import") -> None:
        """Attach imported/manual Gmail facts; this never contacts Gmail or retries sending."""
        if status not in {"DRAFTED", "SENT", "REPLY_RECEIVED", "APPOINTMENT", "EXCLUDED", "HOLD"}:
            raise ValueError("送付履歴の更新状態が不正です")
        if status == "DRAFTED" and not gmail_draft_id.strip():
            raise ValueError("下書き済みにはGmail Draft IDが必要です")
        if status == "SENT" and not (gmail_message_id.strip() or sent_at.strip()):
            raise ValueError("送付済みにはGmail Message IDまたは送付日時が必要です")
        with self._connect() as conn:
            row = conn.execute("SELECT status FROM outreach_records WHERE clinic_id=? AND campaign_id=?",
                               (int(clinic_id), campaign_id)).fetchone()
            if row is None:
                raise KeyError("既存のclinic_id/campaign_id台帳行がありません")
            current = str(row[0])
            ranks = {"GENERATED": 0, "DRAFTED": 1, "SENT": 2,
                     "REPLY_RECEIVED": 3, "APPOINTMENT": 4}
            if current in ranks and status in ranks and ranks[status] < ranks[current]:
                raise ValueError("送付状態を過去の状態へ戻せません")
            if current in {"SENT", "REPLY_RECEIVED", "APPOINTMENT", "EXCLUDED"} and status in {"DRAFTED", "HOLD"}:
                raise ValueError("確定済み送付状態を保留・下書きへ戻せません")
            conn.execute(
                "UPDATE outreach_records SET status=?,gmail_draft_id=?,gmail_message_id=?,sent_at=?,owner=?,source=? "
                "WHERE clinic_id=? AND campaign_id=?",
                (status, gmail_draft_id, gmail_message_id, sent_at, owner, source,
                 int(clinic_id), campaign_id),
            )

    def create_once(self, *, clinic_id: int, clinic_uuid: str, campaign_id: str,
                    generation_batch_id: str, status: str = "GENERATED", owner: str = "",
                    source: str = "UI") -> tuple[str, bool]:
        if status not in ALLOWED_STATUSES:
            raise ValueError("不正な履歴状態です")
        key = hashlib.sha256(f"{clinic_id}|{campaign_id}".encode()).hexdigest()
        outreach_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO outreach_records(outreach_id,clinic_id,clinic_uuid,campaign_id,"
                "generation_batch_id,status,owner,source,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (outreach_id, int(clinic_id), clinic_uuid, campaign_id, generation_batch_id,
                 status, owner, source, key, now),
            )
            row = conn.execute("SELECT outreach_id FROM outreach_records WHERE clinic_id=? AND campaign_id=?",
                               (int(clinic_id), campaign_id)).fetchone()
            return row[0], cur.rowcount == 1

    def counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT status,count(*) FROM outreach_records GROUP BY status").fetchall()
        return {str(status): int(count) for status, count in rows}
