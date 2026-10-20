"""Phase H read-only reconciliation and import planning helpers.

The planner deliberately never labels a row as INSERT/UPDATE/NOOP: no target
database ledger is connected during this phase.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import re
from typing import Any, Iterable, Mapping

from src.outreach.phase_g_sheets_snapshot import reconcile_mail_state

REVIEW_REASONS = (
    "案件ID不一致", "Gmail Draft ID変化・消失", "Scheduled/Sentへの移動",
    "医院名・メールアドレス不一致", "同一医院の複数案件", "UUID不足",
    "対応先なし", "その他",
)


def _s(row: Mapping[str, Any], *keys: str) -> str:
    return next((str(row.get(k)).strip() for k in keys if row.get(k) not in (None, "")), "")


def _identity(row: Mapping[str, Any]) -> str:
    return _s(row, "Clinic UUID", "clinic_uuid", "clinic_id", "管理ID").casefold()


def classify_review_rows(case_rows: Iterable[Mapping[str, Any]], mail_rows: Iterable[Mapping[str, Any]],
                         gmail_snapshot: Mapping[str, Any], *,
                         known_uuid_by_clinic_id: Mapping[str, str] | None = None,
                         known_uuid_by_gas_case_id: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Classify per-tab REVIEW rows and report cross-tab unique cases separately."""
    known_uuid_by_clinic_id = known_uuid_by_clinic_id or {}
    known_uuid_by_gas_case_id = known_uuid_by_gas_case_id or {}
    cases, mails = [dict(r) for r in case_rows], [dict(r) for r in mail_rows]
    for row in (*cases, *mails):
        cid = _s(row, "clinic_id", "Clinic ID", "管理ID")
        case_id = _s(row, "案件ID", "GAS案件ID", "gas_case_id")
        if not _s(row, "Clinic UUID", "clinic_uuid"):
            row["Clinic UUID"] = (known_uuid_by_clinic_id.get(cid, "") if cid else "") or known_uuid_by_gas_case_id.get(case_id, "")
    case_by_id: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    mail_by_id: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    case_by_identity: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    mail_by_identity: dict[str, list[Mapping[str, Any]]] = defaultdict(list)

    def identity_keys(row: Mapping[str, Any]) -> set[str]:
        keys = set()
        clinic_uuid = _s(row, "Clinic UUID", "clinic_uuid").casefold()
        clinic_id = _s(row, "clinic_id", "Clinic ID", "管理ID")
        if clinic_uuid:
            keys.add("uuid:" + clinic_uuid)
        if clinic_id:
            keys.add("clinic_id:" + clinic_id)
        for address in re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
                                  " ".join(str(row.get(k) or "") for k in ("メールアドレス", "宛先", "送信先")), re.I):
            keys.add("email:" + address.casefold())
        return keys

    for row in cases:
        if _s(row, "案件ID", "GAS案件ID", "gas_case_id"):
            case_by_id[_s(row, "案件ID", "GAS案件ID", "gas_case_id")].append(row)
        for key in identity_keys(row):
            case_by_identity[key].append(row)
    for row in mails:
        if _s(row, "案件ID", "GAS案件ID", "gas_case_id"):
            mail_by_id[_s(row, "案件ID", "GAS案件ID", "gas_case_id")].append(row)
        for key in identity_keys(row):
            mail_by_identity[key].append(row)

    identities: dict[str, set[str]] = defaultdict(set)
    for row in (*cases, *mails):
        ident = _identity(row)
        cid = _s(row, "案件ID", "GAS案件ID", "gas_case_id")
        if ident and cid:
            identities[ident].add(cid)

    def classify(rows: list[Mapping[str, Any]], *, is_case: bool) -> tuple[list[dict[str, Any]], Counter]:
        output, counts = [], Counter()
        for row in rows:
            result = reconcile_mail_state(row, gmail_snapshot)
            if result["match"] != "REVIEW":
                continue
            cid = _s(row, "案件ID", "GAS案件ID", "gas_case_id")
            peers = mail_by_id.get(cid, []) if is_case else case_by_id.get(cid, [])
            reasons: list[str] = []
            if not cid:
                reasons.append("対応先なし")
            elif peers and len(peers) > 1:
                reasons.append("案件ID不一致")
            elif peers:
                peer = peers[0]
                left_name = _s(row, "クリニック名", "医院名", "院名", "施設名")
                right_name = _s(peer, "クリニック名", "医院名", "院名", "施設名")
                left_email = _s(row, "メールアドレス", "宛先", "送信先").casefold()
                right_email = _s(peer, "メールアドレス", "宛先", "送信先").casefold()
                if (left_name and right_name and left_name != right_name) or (left_email and right_email and left_email != right_email):
                    reasons.append("医院名・メールアドレス不一致")
            elif cid:
                identity_index = mail_by_identity if is_case else case_by_identity
                identity_peers = {id(candidate): candidate for key in identity_keys(row)
                                  for candidate in identity_index.get(key, [])}
                if identity_peers:
                    reasons.append("案件ID不一致")
                else:
                    reasons.append("対応先なし")

            if result["gmail_state"] == "UNKNOWN" and (_s(row, "下書きID", "Draft ID", "Gmail Draft ID（Sheets）") or "draft" in result["sheet_state"].casefold()):
                reasons.append("Gmail Draft ID変化・消失")
            elif result["gmail_state"] in {"SCHEDULED", "SENT", "REPLIED", "TRASH"} and result["sheet_state"] == "DRAFT":
                reasons.append("Scheduled/Sentへの移動")
            ident = _identity(row)
            if ident and len(identities.get(ident, ())) > 1:
                reasons.append("同一医院の複数案件")
            if not _s(row, "Clinic UUID", "clinic_uuid"):
                reasons.append("UUID不足")
            if not reasons:
                reasons.append("その他")
            for reason in set(reasons):
                counts[reason] += 1
            output.append({"source_tab": "案件管理" if is_case else "メール管理",
                           "source_row": row.get("元行", row.get("__source_row__", "未取得")),
                           "gas_case_id": cid or "未取得", "clinic_id": _s(row, "clinic_id", "管理ID") or "未取得",
                           "clinic_uuid": _s(row, "Clinic UUID", "clinic_uuid") or "未取得",
                           "gmail_state": result["gmail_state"], "sheet_state": result["sheet_state"],
                           "reasons": tuple(sorted(set(reasons)))})
        return output, counts

    case_review, case_counts = classify(cases, is_case=True)
    mail_review, mail_counts = classify(mails, is_case=False)
    # Stable cross-tab union uses GAS case ID only; rows without one remain source-row scoped.
    union_keys = set()
    rows_without_case_id = 0
    for item in (*case_review, *mail_review):
        key = item["gas_case_id"]
        if key != "未取得":
            union_keys.add(key)
        else:
            rows_without_case_id += 1
    return {"case_rows": case_review, "mail_rows": mail_review,
            "case_reason_counts": {k: case_counts.get(k, 0) for k in REVIEW_REASONS},
            "mail_reason_counts": {k: mail_counts.get(k, 0) for k in REVIEW_REASONS},
            "unique_review_cases": len(union_keys), "review_rows_without_case_id": rows_without_case_id,
            "case_review_count": len(case_review),
            "mail_review_count": len(mail_review)}


def plan_supabase_import(tabs: Iterable[Any], *, known_uuid_by_clinic_id: Mapping[str, str],
                         gmail_snapshot: Mapping[str, Any] | None = None,
                         known_uuid_by_gas_case_id: Mapping[str, str] | None = None,
                         known_clinic_id_by_gas_case_id: Mapping[str, str] | None = None,
                         existing_target_tables: set[str] | None = None) -> tuple[dict[str, Any], ...]:
    """Partition snapshots into logical future tables without asserting DB writes.

    Even with a provided target, database actions remain HOLD until a real target
    comparison is explicitly implemented and reviewed.
    """
    planned = []
    gmail_snapshot = gmail_snapshot or {}
    known_uuid_by_gas_case_id = known_uuid_by_gas_case_id or {}
    known_clinic_id_by_gas_case_id = known_clinic_id_by_gas_case_id or {}
    for tab in tabs:
        name = tab.name
        if name in ("案件管理", "メール管理"):
            logical = "sales_case"
        elif "記事" in name or "掲載" in name:
            logical = "article_info"
        elif "トラッキング" in name or "クリック" in name or "開封" in name:
            logical = "engagement_estimate"
        elif "日程" in name or "アポ" in name:
            logical = "appointment"
        elif "Gmail" in name or "下書き" in name or "送信" in name:
            logical = "gmail_history"
        elif "同期" in name or "連携" in name:
            logical = "sync_history"
        elif "ログ" in name or "履歴" in name:
            logical = "state_event"
        else:
            logical = "source_snapshot"
        rows = [dict(zip(tab.headers, values)) for values in tab.rows]
        target_table = {
            "sales_case": "sales_cases", "article_info": "article_selection_results",
            "engagement_estimate": "engagement_events", "appointment": "calendar_appointments",
            "gmail_history": "gmail_messages", "sync_history": "sync_jobs",
            "state_event": "email_state_history", "source_snapshot": "source_import_rows",
        }[logical]
        case_ids = [_s(row, "案件ID", "GAS案件ID", "gas_case_id") for row in rows]
        campaign_keys = [(_s(row, "Clinic UUID", "clinic_uuid", "clinic_id", "管理ID").casefold(),
                          _s(row, "campaign_id", "キャンペーンID")) for row in rows]
        for idx, values in enumerate(tab.rows):
            raw = dict(zip(tab.headers, values))
            cid = _s(raw, "clinic_id", "Clinic ID", "管理ID")
            uid = _s(raw, "Clinic UUID", "clinic_uuid", "Clinic UUID（参照）")
            case_id = _s(raw, "案件ID", "GAS案件ID", "gas_case_id")
            resolved_uuid = known_uuid_by_gas_case_id.get(case_id, "") if case_id else ""
            resolved_clinic_id = known_clinic_id_by_gas_case_id.get(case_id, "") if case_id else ""
            explicit = known_uuid_by_clinic_id.get(cid, "") if cid else ""
            if not explicit and resolved_clinic_id:
                explicit = known_uuid_by_clinic_id.get(str(resolved_clinic_id), "")
            campaign_key = (_s(raw, "Clinic UUID", "clinic_uuid", "clinic_id", "管理ID").casefold(),
                            _s(raw, "campaign_id", "キャンペーンID"))
            has_gmail_identity = any(_s(raw, key) for key in (
                "下書きID", "Draft ID", "Gmail Draft ID（Sheets）", "Message ID", "ThreadID",
                "Thread ID", "送信ThreadID", "Scheduled Message ID", "Sent Message ID",
            ))
            gmail_status = reconcile_mail_state(raw, gmail_snapshot) if has_gmail_identity else None
            if uid and explicit and uid != explicit:
                action, reason = "HOLD", "UUIDと既存の明示対応が不一致"
            elif uid and resolved_uuid and uid != resolved_uuid:
                action, reason = "HOLD", "既存UUIDとClinic Master照合結果が不一致"
            elif cid and resolved_clinic_id and cid != str(resolved_clinic_id):
                action, reason = "HOLD", "Clinic IDとClinic Master照合結果が不一致"
            elif logical in {"sales_case", "gmail_history"} and has_gmail_identity and gmail_status and gmail_status["gmail_state"] == "UNKNOWN":
                action, reason = "HOLD", "GmailのDraft/Scheduled/Sent/Trash/返信を一意確認できない"
            elif logical == "sales_case" and case_id and case_ids.count(case_id) > 1:
                action, reason = "HOLD", "同一タブ内でGAS案件IDが重複"
            elif logical == "sales_case" and campaign_key[0] and campaign_key[1] and campaign_keys.count(campaign_key) > 1:
                action, reason = "HOLD", "同一医院・同一キャンペーンの案件行が重複"
            elif not uid and (explicit or resolved_uuid):
                if not cid and resolved_clinic_id:
                    cid = str(resolved_clinic_id)
                uid = explicit or resolved_uuid
                reason = "Clinic ID/UUIDの一意な照合候補。DB既存営業履歴は未比較"
                action = "UNMATCHED_CANDIDATE"
            elif uid and cid:
                action, reason = "UNMATCHED_CANDIDATE", "IDはあるがDB既存行との比較未実施"
            else:
                action, reason = "UNMATCHED", "Clinic ID/UUIDを一意確定できない"
            if existing_target_tables is not None and target_table not in existing_target_tables:
                action, reason = "HOLD", f"比較対象テーブル {target_table} がDBに存在せず既存行比較不可"
            planned.append({"logical_table": logical, "source_tab": name,
                            "raw_snapshot_table": "source_import_rows", "target_table": target_table,
                            "source_row": tab.source_rows[idx], "clinic_id": cid or "",
                            "clinic_uuid": uid or "", "action": action, "reason": reason,
                            "gmail_state": gmail_status["gmail_state"] if gmail_status else "未照合",
                            "raw_values": tuple(values), "target_compared": False})
    return tuple(planned)


def import_plan_counts(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts = Counter(str(row["action"]) for row in rows)
    return {k: counts.get(k, 0) for k in ("UNMATCHED_CANDIDATE", "UNMATCHED", "HOLD")}
