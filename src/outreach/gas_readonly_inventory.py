"""Local-only GAS history inventory, duplicate preview, and state model.

Only caller-supplied rows/observations are read. This module has no network,
database, Gmail, GAS, Sheets, filesystem, or write-side dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Mapping, Sequence


class MailState(str, Enum):
    DRAFT = "DRAFT"
    SCHEDULED = "SCHEDULED"
    SENT = "SENT"
    REPLIED = "REPLIED"
    PENDING_REVIEW = "PENDING_REVIEW"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


class DuplicateKind(str, Enum):
    SAME_SOURCE_ROW_REIMPORT = "SAME_SOURCE_ROW_REIMPORT"
    SAME_GAS_CASE_REFERENCE = "SAME_GAS_CASE_REFERENCE"
    SAME_TRACKING_TOKEN_REFERENCE = "SAME_TRACKING_TOKEN_REFERENCE"
    SAME_EMAIL_CROSS_FORMAT = "SAME_EMAIL_CROSS_FORMAT"
    SAME_CLINIC_CAMPAIGN_CANDIDATE = "SAME_CLINIC_CAMPAIGN_CANDIDATE"
    DIFFERENT_CAMPAIGN_HISTORY = "DIFFERENT_CAMPAIGN_HISTORY"
    SAME_THREAD_DIFFERENT_MESSAGE = "SAME_THREAD_DIFFERENT_MESSAGE"
    DRAFT_SCHEDULED_CONFLICT = "DRAFT_SCHEDULED_CONFLICT"
    IDENTIFIER_CONFLICT = "IDENTIFIER_CONFLICT"


@dataclass(frozen=True)
class InventoryRow:
    tab: str
    row_number: int
    row_hash: str
    values: Mapping[str, str]


@dataclass(frozen=True)
class TabInventory:
    tab: str
    headers: tuple[str, ...]
    row_count: int
    checksum: str
    rows: tuple[InventoryRow, ...]
    source_id: str = ""
    revision: str = ""


@dataclass(frozen=True)
class GmailObservation:
    """A caller-supplied verified snapshot; no live Gmail read is performed here."""
    status: MailState
    verified: bool
    evidence: str = ""
    draft_id: str = ""
    message_id: str = ""
    thread_id: str = ""


@dataclass(frozen=True)
class StateTransition:
    accepted: bool
    previous: MailState
    current: MailState
    reason: str


@dataclass(frozen=True)
class Reconciliation:
    tab: str
    row_number: int
    case_id: str
    clinic_uuid: str
    clinic_id: str
    operation_key: str
    campaign_id: str
    recipient_email: str
    tracking_token: str
    draft_id: str
    draft_thread_id: str
    message_id: str
    sent_thread_id: str
    sheet_status: str
    gmail_status: str
    disposition: str
    reasons: tuple[str, ...]
    source_key: str = ""
    duplicate_kinds: tuple[str, ...] = ()
    related_rows: tuple[str, ...] = ()
    holds_new_draft: bool = True
    gmail_evidence: str = ""


def _hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _get(values: Mapping[str, str], *keys: str) -> str:
    for key in keys:
        value = values.get(key, "")
        if value:
            return value
    return ""


def _source_key(source_id: str, tab: str, row_number: int) -> str:
    return _hash([source_id, tab, row_number]) if source_id else ""


def build_inventory(
    tabs: Mapping[str, Sequence[Mapping[str, object]]], *, source_id: str = "", revision: str = ""
) -> tuple[TabInventory, ...]:
    """Build deterministic per-tab snapshots while preserving every cell as text."""
    result = []
    for tab, source_rows in tabs.items():
        headers = tuple(str(key) for key in source_rows[0].keys()) if source_rows else ()
        rows = []
        for number, source in enumerate(source_rows, start=2):
            values = {str(key): "" if value is None else str(value) for key, value in source.items()}
            rows.append(InventoryRow(tab, number, _hash(list(values.items())), values))
        checksum = _hash({"tab": tab, "headers": headers, "rows": [row.row_hash for row in rows]})
        result.append(TabInventory(tab, headers, len(rows), checksum, tuple(rows), source_id, revision))
    return tuple(result)


def transition_state(previous: MailState | str, target: MailState | str, *, verified: bool,
                     evidence: str = "") -> StateTransition:
    """Validate a conservative lifecycle transition using evidence supplied by the caller."""
    previous, target = MailState(previous), MailState(target)
    if not verified or not evidence.strip():
        return StateTransition(False, previous, MailState.PENDING_REVIEW,
                               "状態遷移に検証済みの証拠がないため確認待ち")
    allowed = {
        MailState.UNKNOWN: {MailState.DRAFT, MailState.SCHEDULED, MailState.SENT, MailState.ERROR},
        MailState.PENDING_REVIEW: {MailState.DRAFT, MailState.SCHEDULED, MailState.SENT,
                                   MailState.REPLIED, MailState.ERROR},
        MailState.DRAFT: {MailState.SCHEDULED, MailState.SENT, MailState.ERROR},
        MailState.SCHEDULED: {MailState.SENT, MailState.ERROR},
        MailState.SENT: {MailState.REPLIED, MailState.ERROR},
        MailState.REPLIED: set(),
        MailState.ERROR: {MailState.PENDING_REVIEW},
    }
    if target == previous:
        return StateTransition(True, previous, target, "同一状態の再観測")
    if target not in allowed[previous]:
        return StateTransition(False, previous, MailState.PENDING_REVIEW,
                               f"許可されない状態遷移: {previous.value} -> {target.value}")
    return StateTransition(True, previous, target, evidence.strip())


def _sheet_state(values: Mapping[str, str]) -> str:
    status = _get(values, "メール状態", "状態")
    if _get(values, "返信有無") == "あり":
        return "返信あり"
    if _get(values, "送信有無") == "送信済み" or (
        _get(values, "送信日時") and status in {"", "下書き作成"}
    ):
        return "送信済み"
    if not status and _get(values, "下書きID", "Draft ID"):
        return "下書き作成"
    return status


def _identity(row: InventoryRow) -> dict[str, str]:
    v = row.values
    return {
        "case": _get(v, "案件ID", "case_id", "gas_case_id"),
        "clinic_uuid": _get(v, "Clinic UUID", "clinic_uuid").strip().casefold(),
        "clinic_id": _get(v, "clinic_id", "clinic id").strip(),
        "operation_key": _get(v, "operation_key", "operation key").strip(),
        "campaign": _get(v, "campaign_id", "campaign id", "キャンペーンID").strip(),
        "recipient": _get(v, "宛先", "メールアドレス", "recipient_email", "recipient email").strip().casefold(),
        "draft": _get(v, "下書きID", "Draft ID", "gmail_draft_id"),
        "message": _get(v, "Message ID", "Gmail Message ID", "送信Message ID", "gmail_message_id"),
        "thread": _get(v, "送信ThreadID", "ThreadID", "下書きThreadID", "Gmail Thread ID", "gmail_thread_id"),
        "token": _get(v, "追跡トークン", "tracking_token"),
    }


def preview_reconciliation(
    inventory: Sequence[TabInventory], *,
    gmail_observations: Mapping[str, GmailObservation] | None = None,
) -> tuple[Reconciliation, ...]:
    """Return non-destructive identity/state findings; never merge or authorize sending.

    `gmail_observations` is optional caller-supplied evidence keyed by source_key.
    Unverified/missing observations are always UNKNOWN and hold new draft creation.
    Thread IDs link conversations only; they never establish email/message identity.
    """
    observations = gmail_observations or {}
    items = [(tab, row, _identity(row)) for tab in inventory for row in tab.rows]
    findings: list[Reconciliation] = []
    for i, (tab, row, ident) in enumerate(items):
        stable_source_key = _source_key(tab.source_id, tab.tab, row.row_number)
        source_key = stable_source_key or _hash([tab.tab, row.row_number, row.row_hash])
        related: list[str] = []
        kinds: set[str] = set()
        reasons: list[str] = []
        sheet_status = _sheet_state(row.values)
        observation = observations.get(source_key)

        for other_tab, other, other_id in items:
            if other_tab is tab and other is row:
                continue
            ref = f"{other.tab}!{other.row_number}"
            other_stable_source_key = _source_key(other_tab.source_id, other.tab, other.row_number)
            other_source_key = other_stable_source_key or _hash([other.tab, other.row_number, other.row_hash])
            same_source = bool(stable_source_key and stable_source_key == other_stable_source_key)
            if same_source:
                if row.row_hash == other.row_hash:
                    kinds.add(DuplicateKind.SAME_SOURCE_ROW_REIMPORT.value)
                    reasons.append(f"同一原票行の再取込: {ref}（row hash一致）")
                else:
                    kinds.add(DuplicateKind.IDENTIFIER_CONFLICT.value)
                    reasons.append(f"同一原票位置の内容変更: {ref}（snapshot間でrow hash不一致）")
                related.append(ref)
                continue

            same_gmail_identity = bool(
                (ident["draft"] and ident["draft"] == other_id["draft"])
                or (ident["message"] and ident["message"] == other_id["message"])
                or (ident["operation_key"] and ident["operation_key"] == other_id["operation_key"])
            )
            if same_gmail_identity:
                kinds.add(DuplicateKind.SAME_EMAIL_CROSS_FORMAT.value)
                reasons.append(f"Gmail/operationの明示IDが複数原票行で一致: {ref}（同一メール候補。原票は統合しない）")
                related.append(ref)
                for field, label in (("clinic_uuid", "Clinic UUID"), ("case", "GAS案件ID"),
                                     ("recipient", "宛先"), ("operation_key", "operation_key")):
                    if ident[field] and other_id[field] and ident[field] != other_id[field]:
                        kinds.add(DuplicateKind.IDENTIFIER_CONFLICT.value)
                        reasons.append(f"同一メール候補間で{label}が不一致: {ref}")

            other_observation = observations.get(other_source_key)
            same_operation_scope = bool(
                (ident["case"] and ident["case"] == other_id["case"])
                or (ident["operation_key"] and ident["operation_key"] == other_id["operation_key"])
                or (ident["clinic_uuid"] and ident["clinic_uuid"] == other_id["clinic_uuid"]
                    and ident["campaign"] and ident["campaign"] == other_id["campaign"]
                    and ident["recipient"] and ident["recipient"] == other_id["recipient"])
            )
            observed_states = {
                observation.status if observation and observation.verified else None,
                other_observation.status if other_observation and other_observation.verified else None,
            }
            if same_operation_scope and {MailState.DRAFT, MailState.SCHEDULED}.issubset(observed_states):
                kinds.add(DuplicateKind.DRAFT_SCHEDULED_CONFLICT.value)
                reasons.append(f"同一操作範囲にDraftとScheduledの並行状態あり: {ref}（両方保留）")
                related.append(ref)

            if ident["case"] and ident["case"] == other_id["case"]:
                kinds.add(DuplicateKind.SAME_GAS_CASE_REFERENCE.value)
                related.append(ref)
                if tab.tab == other_tab.tab:
                    kinds.add(DuplicateKind.IDENTIFIER_CONFLICT.value)
                    reasons.append(f"同一タブ内でGAS案件IDが重複: {ref}")
                else:
                    reasons.append(f"別タブに同じGAS案件IDの参照あり: {ref}（同一メールとは限らない）")
            if ident["token"] and ident["token"] == other_id["token"]:
                kinds.add(DuplicateKind.SAME_TRACKING_TOKEN_REFERENCE.value)
                related.append(ref)
                reasons.append(f"同じ追跡トークンの案件参照あり: {ref}（メールMessageの同一性とは区別）")
            if ident["thread"] and ident["thread"] == other_id["thread"]:
                if ident["message"] and other_id["message"] and ident["message"] != other_id["message"]:
                    kinds.add(DuplicateKind.SAME_THREAD_DIFFERENT_MESSAGE.value)
                    reasons.append(f"同一Thread内の別Message: {ref}（統合しない）")
                    related.append(ref)
                elif not ident["message"] or not other_id["message"]:
                    reasons.append(f"Thread ID共有のみ: {ref}（Message ID不明のため同一性を判定しない）")
                    related.append(ref)

            same_clinic = bool(ident["clinic_uuid"] and ident["clinic_uuid"] == other_id["clinic_uuid"])
            same_campaign_recipient = bool(
                same_clinic and ident["campaign"] and ident["campaign"] == other_id["campaign"]
                and ident["recipient"] and ident["recipient"] == other_id["recipient"]
            )
            if same_campaign_recipient:
                kinds.add(DuplicateKind.SAME_CLINIC_CAMPAIGN_CANDIDATE.value)
                reasons.append(f"同一Clinic UUID・campaign・recipientの重複候補: {ref}（自動統合しない）")
                related.append(ref)
            elif same_clinic and ident["campaign"] and other_id["campaign"] and ident["campaign"] != other_id["campaign"]:
                kinds.add(DuplicateKind.DIFFERENT_CAMPAIGN_HISTORY.value)
                reasons.append(f"同一医院の別campaign履歴: {ref}（別送信履歴として保持）")
                related.append(ref)

        if observation and observation.verified and observation.evidence.strip():
            gmail_status = observation.status.value
            gmail_evidence = observation.evidence.strip()
        else:
            gmail_status = MailState.UNKNOWN.value
            gmail_evidence = ""
            reasons.append("Gmailライブ照合なし、または未検証。送信予約済みかを含め状態UNKNOWNとして新規下書きを保留")

        if sheet_status in {"下書き作成", "DRAFT"} and gmail_status == MailState.SCHEDULED.value:
            kinds.add(DuplicateKind.DRAFT_SCHEDULED_CONFLICT.value)
            reasons.append("Sheet下書き記録とGmail予約済み観測が競合。新規処理を保留")
        if sheet_status in {"送信済み", "返信あり"} and gmail_status in {MailState.DRAFT.value, MailState.SCHEDULED.value}:
            kinds.add(DuplicateKind.IDENTIFIER_CONFLICT.value)
            reasons.append("Sheet送信/返信記録とGmail状態が競合。人手確認が必要")

        for field, observed in (("draft", observation.draft_id if observation else ""),
                                ("message", observation.message_id if observation else ""),
                                ("thread", observation.thread_id if observation else "")):
            if observation and observed and ident[field] and observed != ident[field]:
                kinds.add(DuplicateKind.IDENTIFIER_CONFLICT.value)
                reasons.append(f"Gmail照合{field} IDがSheet値と不一致")

        disposition = "REVIEW" if reasons or gmail_status == MailState.UNKNOWN.value else "RECONCILED"
        findings.append(Reconciliation(
            tab=tab.tab, row_number=row.row_number, case_id=ident["case"],
            clinic_uuid=ident["clinic_uuid"], clinic_id=ident["clinic_id"],
            operation_key=ident["operation_key"], campaign_id=ident["campaign"],
            recipient_email=ident["recipient"], tracking_token=ident["token"],
            draft_id=ident["draft"], draft_thread_id=_get(row.values, "下書きThreadID", "draft_thread_id"),
            message_id=ident["message"], sent_thread_id=ident["thread"],
            sheet_status=sheet_status, gmail_status=gmail_status, disposition=disposition,
            reasons=tuple(dict.fromkeys(reasons)), source_key=source_key,
            duplicate_kinds=tuple(sorted(kinds)), related_rows=tuple(dict.fromkeys(related)),
            # This inventory row is existing history; never authorize a new draft from it.
            holds_new_draft=True,
            gmail_evidence=gmail_evidence,
        ))
    return tuple(findings)


__all__ = [
    "DuplicateKind", "GmailObservation", "InventoryRow", "MailState", "Reconciliation",
    "StateTransition", "TabInventory", "build_inventory", "preview_reconciliation", "transition_state",
]
