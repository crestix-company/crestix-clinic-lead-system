"""Phase J importer and read-only dashboard for the isolated local PostgreSQL cluster."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, time
import hashlib
import json
import os
from pathlib import Path
import re
import unicodedata
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from src.outreach.phase_g_sheets_snapshot import read_workbook_snapshot, reconcile_mail_state

EXPECTED_DATABASE = "phase_j_outreach_import_v1"
EXPECTED_DATA_DIRECTORY = "/private/tmp/clinic_phase_j_pgdata_20261020"
EXPECTED_SOCKET_DIRECTORY = "/private/tmp/clinic_phase_j_socket_20261020"
SOURCE_TIMEZONE = ZoneInfo("Asia/Tokyo")
DEFAULT_DSN = f"host={EXPECTED_SOCKET_DIRECTORY} port=55439 dbname={EXPECTED_DATABASE} user=maekawahiroyuki"
BASE = Path("/private/tmp")
WORKBOOK = BASE / "clinic_phase_i_source.xlsx"
MANIFEST = BASE / "clinic_phase_i_sheets_manifest.json"
GMAIL = BASE / "clinic_phase_i_gmail_readonly.json"
UUID_MATCHES = BASE / "clinic_phase_i_uuid_matches.json"


def _sha(value: bytes | str) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode("utf-8")).hexdigest()


def _norm(value: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")).casefold())


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or SOURCE_TIMEZONE)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=SOURCE_TIMEZONE)
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or SOURCE_TIMEZONE)
    except ValueError:
        pass
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=SOURCE_TIMEZONE)
        except ValueError:
            continue
    return None


def _appointment_time(day: Any, clock: Any) -> datetime | None:
    day_text, clock_text = _text(day), _text(clock)
    if isinstance(day, datetime):
        base_date = day.date()
    elif isinstance(day, date):
        base_date = day
    else:
        base_date = None
        for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y年%m月%d日"):
            try:
                base_date = datetime.strptime(day_text, fmt).date()
                break
            except ValueError:
                continue
    if not base_date:
        return None
    clock_text = re.split(r"[〜～~\-−–]", clock_text)[0].strip()
    clock_value = None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            clock_value = datetime.strptime(clock_text, fmt).time()
            break
        except ValueError:
            continue
    if clock_value is None:
        return None
    return datetime.combine(base_date, clock_value, tzinfo=SOURCE_TIMEZONE)


def _open_files() -> tuple[tuple[Any, ...], dict[str, Any], dict[str, Any], dict[str, Any]]:
    missing = [str(p) for p in (WORKBOOK, MANIFEST, GMAIL, UUID_MATCHES) if not p.is_file()]
    if missing:
        raise FileNotFoundError("Phase I local snapshot files missing: " + ", ".join(missing))
    tabs = read_workbook_snapshot(WORKBOOK)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    gmail = json.loads(GMAIL.read_text(encoding="utf-8"))
    uuid_matches = json.loads(UUID_MATCHES.read_text(encoding="utf-8"))
    if manifest.get("snapshot_type") != "google_sheets_live_read_only" or gmail.get("snapshot_type") != "phase_i_google_gmail_read_only":
        raise ValueError("Unexpected snapshot provenance; refusing local import")
    return tabs, manifest, gmail, uuid_matches


def connect_verified(dsn: str | None = None) -> psycopg.Connection:
    """Connect only to the named Phase J DB directory over its isolated Unix socket."""
    dsn = dsn or os.environ.get("PHASE_J_LOCAL_DATABASE_URL") or DEFAULT_DSN
    conn = psycopg.connect(dsn, connect_timeout=3)
    row = conn.execute("select current_database(), current_setting('data_directory'), inet_server_addr()::text").fetchone()
    if row[0] != EXPECTED_DATABASE or str(Path(row[1]).resolve()) != str(Path(EXPECTED_DATA_DIRECTORY).resolve()) or row[2] is not None:
        conn.close()
        raise RuntimeError("Safety stop: Phase J accepts only its named local Unix-socket PostgreSQL cluster")
    return conn


def _snapshot_indexes(tabs: Iterable[Any], uuid_matches: Mapping[str, Any]) -> tuple[dict[str, dict[str, str]], set[str], dict[str, str]]:
    by_name = {tab.name: tab for tab in tabs}
    case_tab, progress_tab, audit_tab = (by_name.get(name) for name in (
        "案件管理", "328医院_現在進捗_20261018", "記事掲載_Gmail照合_20261016"))
    cases_by_source_row: dict[str, dict[str, Any]] = {}
    case_id_counts: Counter[str] = Counter()
    if case_tab:
        for source_row, values in zip(case_tab.source_rows, case_tab.rows):
            row = dict(zip(case_tab.headers, values))
            cases_by_source_row[str(source_row)] = row
            gas_id = _text(row.get("案件ID"))
            if gas_id:
                case_id_counts[gas_id] += 1

    clinic_pairs: dict[str, str] = {}
    conflicts: set[str] = set()
    if audit_tab:
        for values in audit_tab.rows:
            row = dict(zip(audit_tab.headers, values))
            clinic_id, clinic_uuid = _text(row.get("clinic_id")), _text(row.get("Clinic UUID"))
            if clinic_id and clinic_uuid:
                if clinic_id in clinic_pairs and clinic_pairs[clinic_id] != clinic_uuid:
                    conflicts.add(clinic_id)
                clinic_pairs[clinic_id] = clinic_uuid
    for clinic_id in conflicts:
        clinic_pairs.pop(clinic_id, None)
    for item in uuid_matches.get("records", []):
        if item.get("status") != "MATCHED":
            continue
        clinic_id, clinic_uuid = _text(item.get("clinic_id")), _text(item.get("clinic_uuid"))
        if not clinic_id or not clinic_uuid:
            continue
        if clinic_id in clinic_pairs and clinic_pairs[clinic_id] != clinic_uuid:
            clinic_pairs.pop(clinic_id, None)
            conflicts.add(clinic_id)
        elif clinic_id not in conflicts:
            clinic_pairs[clinic_id] = clinic_uuid

    case_identity: dict[str, dict[str, str]] = {}
    case_conflicts: set[str] = set()
    if progress_tab and case_tab:
        for values in progress_tab.rows:
            progress = dict(zip(progress_tab.headers, values))
            source_row = _text(progress.get("案件管理行"))
            case = cases_by_source_row.get(source_row)
            clinic_id = _text(progress.get("clinic_id"))
            clinic_uuid = clinic_pairs.get(clinic_id, "")
            if not case or not clinic_id or not clinic_uuid:
                continue
            case_id = _text(case.get("案件ID"))
            # Require the progress pointer plus exact clinic-name/email agreement.
            if not case_id or _norm(case.get("クリニック名")) != _norm(progress.get("医院名")) or _norm(case.get("メールアドレス")) != _norm(progress.get("メールアドレス")):
                continue
            if case_identity.get(case_id, {}).get("clinic_uuid") not in (None, clinic_uuid):
                case_conflicts.add(case_id)
                continue
            case_identity[case_id] = {"clinic_id": clinic_id, "clinic_uuid": clinic_uuid,
                                      "evidence": f"progress_row_pointer:{source_row};exact_name_email;uuid_audit_tab"}

    for case_id in case_conflicts:
        case_identity.pop(case_id, None)
    for item in uuid_matches.get("records", []):
        if item.get("status") != "MATCHED":
            continue
        case_id, clinic_id, clinic_uuid = _text(item.get("gas_case_id")), _text(item.get("clinic_id")), _text(item.get("clinic_uuid"))
        if not case_id or not clinic_id or not clinic_uuid or case_id_counts[case_id] != 1:
            continue
        old = case_identity.get(case_id)
        if old and (old["clinic_id"], old["clinic_uuid"]) != (clinic_id, clinic_uuid):
            case_identity.pop(case_id, None)
            case_conflicts.add(case_id)
            continue
        case_identity[case_id] = {"clinic_id": clinic_id, "clinic_uuid": clinic_uuid,
                                  "evidence": "clinic_master_exact_name_phone_unique_match"}
    return case_identity, {k for k, n in case_id_counts.items() if n != 1} | case_conflicts, clinic_pairs


def _gmail_candidates(row: Mapping[str, Any], snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    draft_ids = set()
    message_ids = set()
    thread_ids = set()
    for key in ("下書きID", "旧Draft ID", "Draft ID", "Gmail Draft ID（Sheets）"):
        if _text(row.get(key)):
            draft_ids.add(_text(row.get(key)).strip("<>"))
    for key in ("Message ID", "Gmail Message ID", "Sent Message ID", "Scheduled Message ID", "旧Message ID"):
        if _text(row.get(key)):
            message_ids.add(_text(row.get(key)).strip("<>"))
    for key in ("下書きThreadID", "下書きThread ID", "ThreadID", "Thread ID", "送信ThreadID", "送信Thread ID", "旧Thread ID", "Gmail Thread ID（Sheets）"):
        if _text(row.get(key)):
            thread_ids.update(x.strip("<>") for x in re.split(r"[,;\s]+", _text(row.get(key))) if x)
    found = {}
    for item in snapshot.get("drafts", []):
        did, mid, tid = _text(item.get("draft_id")), _text(item.get("message_id")), _text(item.get("thread_id"))
        if (did and did in draft_ids) or (tid and tid in thread_ids):
            key = mid or did
            found[key] = {"status": "DRAFT", "gmail_draft_id": did, "gmail_message_id": mid,
                          "gmail_thread_id": tid, "email_ts": item.get("email_ts")}
    states = {"scheduled": "SCHEDULED", "sent": "SENT", "replied": "REPLIED", "trash": "TRASH"}
    for bucket, status in states.items():
        for item in snapshot.get("searches", {}).get(bucket, []):
            mid, tid = _text(item.get("id")), _text(item.get("thread_id"))
            if (mid and mid in message_ids) or (tid and tid in thread_ids) or (mid and mid in draft_ids):
                key = mid or tid
                found[key] = {"status": status, "gmail_draft_id": None, "gmail_message_id": mid,
                              "gmail_thread_id": tid, "email_ts": item.get("email_ts")}
    return tuple(found[k] for k in sorted(found))


def import_phase_i_snapshot() -> dict[str, Any]:
    tabs, manifest, gmail, uuid_matches = _open_files()
    retrieved_at = _timestamp(manifest.get("retrieved_at"))
    if not retrieved_at:
        raise ValueError("Sheets snapshot retrieval time missing")
    workbook_bytes = WORKBOOK.read_bytes()
    workbook_hash = _sha(workbook_bytes)
    gmail_retrieved_at = _timestamp(gmail.get("retrieved_at"))
    if not gmail_retrieved_at:
        raise ValueError("Gmail snapshot retrieval time missing")
    case_identity, duplicate_case_ids, clinic_pairs = _snapshot_indexes(tabs, uuid_matches)
    by_tab = {tab.name: tab for tab in tabs}
    case_tab = by_tab["案件管理"]
    case_rows = [dict(zip(case_tab.headers, row)) for row in case_tab.rows]
    case_statuses: dict[str, str] = {}
    case_records: dict[str, dict[str, Any]] = {}
    for row in case_rows:
        case_id = _text(row.get("案件ID"))
        if not case_id or case_id in duplicate_case_ids or case_id not in case_identity:
            continue
        if case_id in case_records:
            continue
        state = reconcile_mail_state(row, gmail)["gmail_state"]
        case_statuses[case_id] = state
        if state == "UNKNOWN":
            continue
        case_records[case_id] = row

    inserted_counts = Counter()
    hold_reasons = Counter()
    with connect_verified() as conn:
        # Reference rows exist only in this empty local fixture DB; no source/remote DB is mutated.
        clinic_details: dict[str, dict[str, str]] = {}
        for tab_name in ("記事掲載_Gmail照合_20261016",):
            tab = by_tab.get(tab_name)
            if not tab:
                continue
            for values in tab.rows:
                row = dict(zip(tab.headers, values))
                clinic_id = _text(row.get("clinic_id"))
                if clinic_id in {item["clinic_id"] for item in case_identity.values()}:
                    clinic_details.setdefault(clinic_id, {"clinic_name": _text(row.get("Clinic Master医院名") or row.get("医院名（CSV）")),
                                                           "address": _text(row.get("Clinic Master住所") or row.get("住所（CSV）"))})
        clinic_id_to_uuid = dict(clinic_pairs)
        clinic_id_to_uuid.update({v["clinic_id"]: v["clinic_uuid"] for v in case_identity.values()})
        for clinic_id, clinic_uuid in sorted(clinic_id_to_uuid.items()):
            detail = clinic_details.get(clinic_id, {})
            existing = conn.execute("select uuid from public.clinics where id=%s", (int(clinic_id),)).fetchone()
            if existing and existing[0] != clinic_uuid:
                raise RuntimeError("Local clinic ID/UUID fixture conflict; refusing import")
            conn.execute("""insert into public.clinics(id,uuid,clinic_name,address,active)
                           values (%s,%s,%s,%s,true) on conflict (id) do nothing""",
                         (int(clinic_id), clinic_uuid, detail.get("clinic_name", ""), detail.get("address", "")))
        for case_id, identity in case_identity.items():
            conn.execute("""insert into outreach.gas_case_identity_links
              (gas_case_id,clinic_id,clinic_uuid,match_status,evidence,confirmed_by,confirmed_at,source_payload_hash)
              values (%s,%s,%s,'CONFIRMED',%s,'phase_j_local_import',now(),%s)
              on conflict (gas_case_id) do nothing""",
                         (case_id, int(identity["clinic_id"]), identity["clinic_uuid"],
                          Jsonb({"method": "source_snapshot_identity", "evidence": identity["evidence"]}), workbook_hash))
        conn.commit()

        # Every source tab receives a deterministic batch. The workbook checksum and
        # acquisition timestamp make the exact snapshot idempotent across reruns.
        batches: dict[str, str] = {}
        for tab in tabs:
            batch_row = conn.execute("""insert into outreach.source_import_batches
              (source_system,source_file_id,source_file_name,source_tab,source_file_checksum,source_revision,
               raw_headers,import_status,total_source_rows,mapping_version,snapshot_retrieved_at)
              values ('GOOGLE_SHEETS','phase_i_connector_snapshot','第一営業部_メール日程調整管理',%s,%s,%s,%s,
                      'PREVIEW',%s,'phase-j-v1',%s)
              on conflict (source_system,source_file_checksum,source_tab,source_revision) do nothing
              returning import_batch_id""",
              (tab.name, workbook_hash, retrieved_at.isoformat(), Jsonb(list(tab.headers)), len(tab.rows), retrieved_at)).fetchone()
            if batch_row:
                batch_id = batch_row[0]
                inserted_counts["batches"] += 1
            else:
                batch_id = conn.execute("""select import_batch_id from outreach.source_import_batches
                   where source_system='GOOGLE_SHEETS' and source_file_checksum=%s and source_tab=%s and source_revision=%s""",
                    (workbook_hash, tab.name, retrieved_at.isoformat())).fetchone()[0]
            batches[tab.name] = str(batch_id)

        case_db_ids: dict[str, str] = {}
        for case_id, row in case_records.items():
            identity = case_identity[case_id]
            campaign_id = _text(row.get("campaign_id") or row.get("キャンペーンID"))
            source_row = next((r for r, values in zip(case_tab.source_rows, case_tab.rows)
                               if _text(dict(zip(case_tab.headers, values)).get("案件ID")) == case_id), None)
            payload = {"case_id": case_id, "source_row": source_row,
                       "clinic_id": identity["clinic_id"], "clinic_uuid": identity["clinic_uuid"]}
            op_key = "phase-j-gas-case:" + case_id
            inserted = conn.execute("""insert into outreach.sales_cases
              (operation_key,source_system,external_case_id,clinic_id,clinic_uuid,clinic_name_snapshot,campaign_id,is_owner,fs_owner,
               case_status,hold_reason,source_payload_hash)
              values (%s,'GAS_SHEETS',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              on conflict do nothing returning sales_case_id""",
              (op_key, case_id, int(identity["clinic_id"]), identity["clinic_uuid"], _text(row.get("クリニック名")), campaign_id,
               _text(row.get("IS担当")), _text(row.get("担当FS")), "ON_HOLD" if not campaign_id else "OPEN",
               "Campaign IDは原票にありません" if not campaign_id else "", _sha(json.dumps(payload, sort_keys=True)))) .fetchone()
            if inserted:
                inserted_counts["sales_cases"] += 1
                sales_case_id = inserted[0]
            else:
                previous = conn.execute("select sales_case_id,clinic_id,clinic_uuid from outreach.sales_cases where source_system='GAS_SHEETS' and external_case_id=%s", (case_id,)).fetchone()
                if not previous or int(previous[1]) != int(identity["clinic_id"]) or previous[2] != identity["clinic_uuid"]:
                    raise RuntimeError("Existing local GAS case identity conflict; refusing import")
                sales_case_id = previous[0]
                conn.execute("""update outreach.sales_cases set clinic_name_snapshot=%s,campaign_id=%s,
                  is_owner=%s,fs_owner=%s,case_status=%s,hold_reason=%s,source_payload_hash=%s
                  where sales_case_id=%s""",
                  (_text(row.get("クリニック名")), campaign_id, _text(row.get("IS担当")), _text(row.get("担当FS")),
                   "ON_HOLD" if not campaign_id else "OPEN",
                   "Campaign IDは原票にありません" if not campaign_id else "", _sha(json.dumps(payload, sort_keys=True)), sales_case_id))
            case_db_ids[case_id] = str(sales_case_id)

        # Raw immutable rows first; non-unique or unsupported rows stay HOLD in place.
        for tab in tabs:
            for offset, values in enumerate(tab.rows):
                source_row = tab.source_rows[offset]
                raw = dict(zip(tab.headers, values))
                gas_id = _text(raw.get("案件ID") or raw.get("GAS案件ID") or raw.get("gas_case_id"))
                raw_uuid = _text(raw.get("Clinic UUID") or raw.get("clinic_uuid") or raw.get("Clinic UUID（参照）"))
                clinic_id = _text(raw.get("clinic_id") or raw.get("Clinic ID") or raw.get("管理ID"))
                known = case_identity.get(gas_id) if gas_id else None
                if known:
                    if raw_uuid and raw_uuid != known["clinic_uuid"]:
                        status, reason = "CONFLICT", "原票UUIDが確認済み対応と不一致"
                    elif clinic_id and clinic_id != known["clinic_id"]:
                        status, reason = "CONFLICT", "原票Clinic IDが確認済み対応と不一致"
                    elif gas_id not in case_db_ids:
                        status, reason = "HOLD", "Gmail状態UNKNOWNまたは案件状態を確定できません"
                        clinic_id, raw_uuid = known["clinic_id"], known["clinic_uuid"]
                    else:
                        status, reason = "MATCHED", "一意なClinic ID/UUID/GAS案件ID対応"
                        clinic_id, raw_uuid = known["clinic_id"], known["clinic_uuid"]
                elif raw_uuid and clinic_id:
                    pair = conn.execute("select uuid from public.clinics where id=%s", (int(clinic_id),)).fetchone()
                    if pair and pair[0] == raw_uuid:
                        status, reason = "MATCHED", "原票のClinic ID/UUIDがローカル参照台帳と一致"
                    else:
                        status, reason = "CONFLICT", "原票Clinic ID/UUIDが不一致または候補なし"
                else:
                    status = "HOLD"
                    reason = "案件IDなし" if not gas_id else "Clinic UUID/一意な案件対応なし"
                linked_case = case_db_ids.get(gas_id) if status == "MATCHED" else None
                raw_values = Jsonb([_json_safe(value) for value in values])
                row_hash = _sha(json.dumps(_json_safe(list(values)), ensure_ascii=False, separators=(",", ":")))
                inserted = conn.execute("""insert into outreach.source_import_rows
                  (import_batch_id,source_tab,source_row_number,raw_headers,raw_values,raw_row_hash,
                   candidate_clinic_id,candidate_clinic_uuid,candidate_case_id,linked_sales_case_id,
                   mapping_status,mapping_reason,snapshot_retrieved_at)
                  values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                  on conflict (import_batch_id,source_tab,source_row_number) do update set
                    candidate_clinic_id=coalesce(excluded.candidate_clinic_id,source_import_rows.candidate_clinic_id),
                    candidate_clinic_uuid=case when source_import_rows.mapping_status in ('MATCHED','APPROVED')
                      then source_import_rows.candidate_clinic_uuid else excluded.candidate_clinic_uuid end,
                    linked_sales_case_id=coalesce(excluded.linked_sales_case_id,source_import_rows.linked_sales_case_id),
                    mapping_status=case when source_import_rows.mapping_status in ('MATCHED','APPROVED')
                      then source_import_rows.mapping_status else excluded.mapping_status end,
                    mapping_reason=case when source_import_rows.mapping_status in ('MATCHED','APPROVED')
                      then source_import_rows.mapping_reason else excluded.mapping_reason end
                  where source_import_rows.mapping_status not in ('MATCHED','APPROVED')
                    and (excluded.mapping_status='MATCHED' or source_import_rows.mapping_status<>excluded.mapping_status)
                  returning import_row_id,(xmax=0)""",
                  (batches[tab.name], tab.name, source_row, Jsonb(list(tab.headers)), raw_values, row_hash,
                   int(clinic_id) if clinic_id.isdigit() else None,
                   raw_uuid, gas_id,
                   linked_case, status, reason, retrieved_at)).fetchone()
                if inserted:
                    if inserted[1]:
                        inserted_counts["source_rows"] += 1
                    else:
                        inserted_counts["source_rows_reclassified"] += 1
                if tab.name == "メール管理" and reconcile_mail_state(raw, gmail)["gmail_state"] == "UNKNOWN":
                    status, reason, linked_case = "HOLD", "Gmail Draft/Scheduled/Sent/Trash/返信を一意照合できません", None
                    conn.execute("""update outreach.source_import_rows set mapping_status=%s,mapping_reason=%s,linked_sales_case_id=null
                      where import_batch_id=%s and source_tab=%s and source_row_number=%s""",
                      (status, reason, batches[tab.name], tab.name, source_row))
                if status != "MATCHED":
                    hold_reasons[reason] += 1

        # Gmail IDs are attached only by exact ID/Thread ID evidence. Unknown and
        # ambiguous records remain in source_import_rows as HOLD.
        mail_tab = by_tab.get("メール管理")
        if mail_tab:
            for source_row, values in zip(mail_tab.source_rows, mail_tab.rows):
                row = dict(zip(mail_tab.headers, values))
                case_id = _text(row.get("案件ID"))
                sales_case_id = case_db_ids.get(case_id)
                if not sales_case_id:
                    continue
                observed = _gmail_candidates(row, gmail)
                if not observed:
                    continue
                for item in observed:
                    state = item["status"]
                    if state not in {"DRAFT", "SCHEDULED", "SENT", "REPLIED", "TRASH"}:
                        continue
                    primary_id = item.get("gmail_message_id") or item.get("gmail_draft_id")
                    if not primary_id:
                        continue
                    operation_key = "phase-j-gmail:" + primary_id
                    at = _timestamp(item.get("email_ts")) or retrieved_at
                    prev = conn.execute("select gmail_record_id,message_status,sales_case_id from outreach.gmail_messages where operation_key=%s", (operation_key,)).fetchone()
                    if prev and str(prev[2]) != sales_case_id:
                        hold_reasons["Gmail ID is already linked to another sales case"] += 1
                        continue
                    if prev and prev[1] != state:
                        conn.execute("""update outreach.gmail_messages set message_status=%s,
                          scheduled_at=case when %s='SCHEDULED' then coalesce(scheduled_at,%s) else scheduled_at end,
                          sent_at=case when %s='SENT' then coalesce(sent_at,%s) else sent_at end,
                          replied_at=case when %s='REPLIED' then coalesce(replied_at,%s) else replied_at end,
                          last_synced_at=%s where gmail_record_id=%s""",
                          (state, state, at, state, at, state, at, gmail_retrieved_at, prev[0]))
                        gmail_record_id, previous_status = prev[0], prev[1]
                    elif prev:
                        conn.execute("update outreach.gmail_messages set last_synced_at=%s where gmail_record_id=%s",
                                     (gmail_retrieved_at, prev[0]))
                        gmail_record_id, previous_status = prev[0], prev[1]
                    else:
                        inserted = conn.execute("""insert into outreach.gmail_messages
                          (sales_case_id,operation_key,gmail_draft_id,gmail_message_id,gmail_thread_id,message_status,
                           recipient_snapshot,subject_snapshot,scheduled_at,sent_at,replied_at,gmail_updated_at,last_synced_at,source_payload_hash)
                          values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                          on conflict do nothing returning gmail_record_id""",
                          (sales_case_id, operation_key, item.get("gmail_draft_id"), item.get("gmail_message_id"),
                           item.get("gmail_thread_id"), state, _text(row.get("宛先")), _text(row.get("件名")),
                           at if state == "SCHEDULED" else None, at if state == "SENT" else None,
                           at if state == "REPLIED" else None, at, gmail_retrieved_at,
                           _sha(json.dumps(_json_safe(item), sort_keys=True)))).fetchone()
                        if not inserted:
                            continue
                        inserted_counts["gmail_messages"] += 1
                        gmail_record_id, previous_status = inserted[0], None
                    event_key = f"phase-j-gmail:{primary_id}:{state}:{at.isoformat()}"
                    event = conn.execute("""insert into outreach.email_state_history
                      (gmail_record_id,previous_status,next_status,source_system,source_event_key,occurred_at,source_payload_hash)
                      values (%s,%s,%s,'GMAIL_API',%s,%s,%s) on conflict do nothing returning state_event_id""",
                      (gmail_record_id, previous_status, state, event_key, at, _sha(event_key))).fetchone()
                    if event:
                        inserted_counts["state_events"] += 1

        # Article URL remains unconfirmed unless the source explicitly says LISTED.
        for case_id, row in case_records.items():
            sales_case_id = case_db_ids[case_id]
            url = _text(row.get("掲載記事URL"))
            if not url:
                continue
            state_text = _text(row.get("記事状況"))
            article_status = state_text.upper() if state_text.upper() in {"LISTED", "NOT_LISTED", "UNCONFIRMED"} else "UNCONFIRMED"
            source_key = "案件管理:" + str(next((r for r, values in zip(case_tab.source_rows, case_tab.rows)
                                                   if _text(dict(zip(case_tab.headers, values)).get("案件ID")) == case_id), ""))
            inserted = conn.execute("""insert into outreach.article_selection_results
              (sales_case_id,clinic_id,article_status,actual_article_url,source_event_key,source_payload_hash,selected_by)
              values (%s,%s,%s,%s,%s,%s,'GAS_SHEETS') on conflict do nothing returning article_selection_id""",
              (sales_case_id, int(case_identity[case_id]["clinic_id"]), article_status, url,
               source_key + ":" + _sha(url), _sha(url))).fetchone()
            if inserted:
                inserted_counts["articles"] += 1

            event_id = _text(row.get("カレンダーEventID"))
            meet_url = _text(row.get("Meet URL"))
            appt_at = _timestamp(row.get("アポ確定日時") or row.get("アポ日時"))
            if event_id and appt_at:
                calendar_key = "GAS_SHEETS:case:" + case_id + ":" + _sha(event_id + appt_at.isoformat())
                inserted = conn.execute("""insert into outreach.calendar_appointments
                  (sales_case_id,provider,external_event_id,source_event_key,appointment_status,starts_at,meet_url,
                   is_owner,fs_owner,source_payload_hash,last_synced_at)
                  values (%s,'GAS_SHEETS',%s,%s,'CONFIRMED',%s,%s,%s,%s,%s,%s)
                  on conflict do nothing returning appointment_id""",
                  (sales_case_id, event_id, calendar_key, appt_at, meet_url, _text(row.get("IS担当")),
                   _text(row.get("担当FS")), _sha(calendar_key), retrieved_at)).fetchone()
                if inserted:
                    inserted_counts["appointments"] += 1

        # Activity history is retained per source row; no clinic-level aggregation.
        tracking = by_tab.get("トラッキングログ")
        if tracking:
            for source_row, values in zip(tracking.source_rows, tracking.rows):
                row = dict(zip(tracking.headers, values))
                case_id, sales_case_id = _text(row.get("案件ID")), case_db_ids.get(_text(row.get("案件ID")))
                if not sales_case_id:
                    continue
                label = (_text(row.get("イベント")) + " " + _text(row.get("分類"))).casefold()
                event_type = "CLICK" if "click" in label or "クリック" in label else "OPEN" if "open" in label or "開封" in label else "REPLY" if "reply" in label or "返信" in label else ""
                event_at = _timestamp(row.get("受信日時"))
                if not event_type or not event_at:
                    hold_reasons["Tracking event type/time unresolved"] += 1
                    continue
                event_key = f"phase-j:{tracking.name}:{source_row}:{_sha(json.dumps(_json_safe(row),ensure_ascii=False,sort_keys=True))}"
                inserted = conn.execute("""insert into outreach.engagement_events
                  (sales_case_id,operation_key,source_system,source_event_key,event_type,event_at,url,confidence,privacy_metadata,source_payload_hash)
                  values (%s,%s,'GAS_SHEETS',%s,%s,%s,%s,%s,%s,%s) on conflict do nothing returning event_id""",
                  (sales_case_id, "phase-j-event:" + case_id, event_key, event_type, event_at,
                   _text(row.get("URL")), _text(row.get("信頼度")) or "UNKNOWN",
                   Jsonb({"classification": _text(row.get("分類")), "source_row": source_row}), _sha(event_key))).fetchone()
                if inserted:
                    inserted_counts["engagement_events"] += 1

        schedule = by_tab.get("日程候補ログ")
        if schedule:
            for source_row, values in zip(schedule.source_rows, schedule.rows):
                row = dict(zip(schedule.headers, values))
                case_id = _text(row.get("案件ID"))
                sales_case_id = case_db_ids.get(case_id)
                if not sales_case_id:
                    continue
                start = _appointment_time(row.get("日付"), row.get("開始"))
                end = _appointment_time(row.get("日付"), row.get("終了"))
                if not start or not end or end <= start:
                    hold_reasons["Appointment date/time unresolved"] += 1
                    continue
                source_key = f"phase-j:{schedule.name}:{source_row}:{_sha(json.dumps(_json_safe(row),ensure_ascii=False,sort_keys=True))}"
                inserted = conn.execute("""insert into outreach.calendar_appointments
                  (sales_case_id,provider,source_event_key,appointment_status,starts_at,ends_at,is_owner,fs_owner,source_payload_hash,last_synced_at)
                  values (%s,'GAS_SHEETS',%s,'PROPOSED',%s,%s,%s,%s,%s,%s)
                  on conflict do nothing returning appointment_id""",
                  (sales_case_id, source_key, start, end, _text(row.get("対応可能FS")), "",
                   _sha(source_key), retrieved_at)).fetchone()
                if inserted:
                    inserted_counts["appointments"] += 1

        for tab in tabs:
            batch_id = batches[tab.name]
            matched_count, held_count = conn.execute("""select count(*) filter(where mapping_status='MATCHED'),
              count(*) filter(where mapping_status<>'MATCHED') from outreach.source_import_rows where import_batch_id=%s""",
                (batch_id,)).fetchone()
            conn.execute("""update outreach.source_import_batches set import_status=%s,inserted_rows=%s,error_rows=%s
              where import_batch_id=%s""",
              ("PARTIAL" if held_count else "IMPORTED", matched_count, held_count, batch_id))
        all_rows = sum(len(tab.rows) for tab in tabs)
        hold_count = conn.execute("select count(*) from outreach.source_import_rows where mapping_status in ('HOLD','UNMATCHED','AMBIGUOUS','CONFLICT')").fetchone()[0]
        job_key = "phase-j-sheet-snapshot:" + workbook_hash
        conn.execute("""insert into outreach.sync_jobs
          (operation_key,source_system,sync_mode,cursor_after,job_status,rows_seen,rows_inserted,rows_conflicted,started_at,finished_at)
          values (%s,'GOOGLE_SHEETS','INITIAL_FULL',%s,%s,%s,%s,%s,now(),now())
          on conflict (operation_key) do update set rows_seen=excluded.rows_seen,rows_inserted=excluded.rows_inserted,
          rows_conflicted=excluded.rows_conflicted,finished_at=excluded.finished_at,job_status=excluded.job_status""",
          (job_key, retrieved_at.isoformat(), "PARTIAL" if hold_count else "SUCCEEDED", all_rows,
           all_rows, hold_count))
        conn.commit()
        stats = {
            "source_rows_total": all_rows,
            "source_rows_inserted_this_run": inserted_counts["source_rows"],
            "source_rows_reclassified_this_run": inserted_counts["source_rows_reclassified"],
            "source_batches_inserted_this_run": inserted_counts["batches"],
            "confirmed_sales_cases_total": conn.execute("select count(*) from outreach.sales_cases where source_system='GAS_SHEETS'").fetchone()[0],
            "operational_open_sales_cases_total": conn.execute("select count(*) from outreach.sales_cases where source_system='GAS_SHEETS' and case_status='OPEN'").fetchone()[0],
            "gmail_messages_total": conn.execute("select count(*) from outreach.gmail_messages").fetchone()[0],
            "state_events_total": conn.execute("select count(*) from outreach.email_state_history").fetchone()[0],
            "articles_total": conn.execute("select count(*) from outreach.article_selection_results").fetchone()[0],
            "engagement_events_total": conn.execute("select count(*) from outreach.engagement_events").fetchone()[0],
            "appointments_total": conn.execute("select count(*) from outreach.calendar_appointments").fetchone()[0],
            "hold_rows_total": hold_count,
            "mapping_status_counts": dict(conn.execute("select mapping_status,count(*) from outreach.source_import_rows group by mapping_status").fetchall()),
            "hold_reason_counts": dict(hold_reasons),
            "new_business_rows_this_run": {k: inserted_counts[k] for k in ("sales_cases", "gmail_messages", "state_events", "articles", "engagement_events", "appointments")},
            "retrieved_at": retrieved_at.isoformat(),
            "workbook_checksum": workbook_hash,
            "repeat_safe": True,
        }
        return stats


def load_local_outreach_dashboard(limit: int = 500) -> dict[str, Any]:
    try:
        conn = connect_verified()
    except Exception as exc:
        return {"available": False, "reason": f"隔離Phase J DBを利用できません: {type(exc).__name__}"}
    try:
        conn.commit()
        conn.execute("set transaction read only")
        conn.execute("set local role clinic_outreach_runtime")
        counts = conn.execute("""select
          (select count(*) from outreach.sales_cases where source_system='GAS_SHEETS'),
          (select count(*) from outreach.source_import_rows),
          (select max(finished_at) from outreach.sync_jobs where source_system='GOOGLE_SHEETS'),
          (select count(*) from outreach.sales_cases where source_system='GAS_SHEETS' and case_status='OPEN'),
          (select count(*) from outreach.sales_cases where source_system='GAS_SHEETS' and case_status='ON_HOLD'),
          (select count(*) from outreach.source_import_rows where mapping_status<>'MATCHED')""").fetchone()
        cases = conn.execute("""select c.clinic_name_snapshot as clinic_name,c.clinic_id,c.clinic_uuid,
          c.external_case_id as gas_case_id,c.case_status,c.hold_reason,
          coalesce(g.message_status,'未取得') as gmail_status,
          coalesce(a.actual_article_url,'未取得') as article_url,
          coalesce(e.reply_count,0) as reply_events,coalesce(e.click_count,0) as click_events,
          coalesce(e.open_count,0) as open_events,coalesce(ap.appointment_status,'未取得') as appointment_status,
          ap.starts_at,ap.meet_url,c.updated_at
          from outreach.sales_cases c
          left join lateral (select message_status from outreach.gmail_messages x where x.sales_case_id=c.sales_case_id order by x.last_synced_at desc nulls last limit 1) g on true
          left join lateral (select actual_article_url from outreach.article_selection_results x where x.sales_case_id=c.sales_case_id order by selected_at desc limit 1) a on true
          left join lateral (select count(*) filter(where event_type='REPLY') reply_count,
            count(*) filter(where event_type='CLICK') click_count,count(*) filter(where event_type='OPEN') open_count
            from outreach.engagement_events x where x.sales_case_id=c.sales_case_id) e on true
          left join lateral (select appointment_status,starts_at,meet_url from outreach.calendar_appointments x
            where x.sales_case_id=c.sales_case_id order by starts_at desc nulls last limit 1) ap on true
          where c.source_system='GAS_SHEETS' order by c.created_at desc limit %s""", (limit,)).fetchall()
        activities = conn.execute("""select * from (
          select c.external_case_id as gas_case_id,'GMAIL' as record_type,g.message_status as state,
            g.gmail_draft_id,g.gmail_message_id,g.gmail_thread_id,null::text as previous_status,
            null::timestamptz as occurred_at,a.actual_article_url as article_url,null::text as event_type,
            null::text as confidence,null::timestamptz as engagement_at,null::text as appointment_status,
            null::timestamptz as starts_at,null::text as meet_url
            from outreach.gmail_messages g join outreach.sales_cases c using(sales_case_id)
            left join lateral (select actual_article_url from outreach.article_selection_results x where x.sales_case_id=c.sales_case_id order by selected_at desc limit 1) a on true
          union all
          select c.external_case_id,'STATE',h.next_status,null,null,null,h.previous_status,h.occurred_at,null,null,null,null,null,null,null
            from outreach.email_state_history h join outreach.gmail_messages g using(gmail_record_id)
            join outreach.sales_cases c using(sales_case_id)
          union all
          select c.external_case_id,'ARTICLE',null,null,null,null,null,a.selected_at,a.actual_article_url,null,null,null,null,null,null
            from outreach.article_selection_results a join outreach.sales_cases c using(sales_case_id)
          union all
          select c.external_case_id,'ENGAGEMENT',null,null,null,null,null,e.event_at,null,e.event_type,e.confidence,e.event_at,null,null,null
            from outreach.engagement_events e join outreach.sales_cases c using(sales_case_id)
          union all
          select c.external_case_id,'APPOINTMENT',null,null,null,null,null,ap.starts_at,null,null,null,null,ap.appointment_status,ap.starts_at,ap.meet_url
            from outreach.calendar_appointments ap join outreach.sales_cases c using(sales_case_id)
          ) activity order by gas_case_id,occurred_at nulls last limit %s""", (limit,)).fetchall()
        holds = conn.execute("""select source_tab,source_row_number,candidate_case_id,candidate_clinic_id,candidate_clinic_uuid,mapping_status,mapping_reason,snapshot_retrieved_at
          from outreach.source_import_rows where mapping_status <> 'MATCHED'
          order by source_tab,source_row_number limit %s""", (limit,)).fetchall()
        conn.commit()
        columns_cases = ("clinic_name","clinic_id","clinic_uuid","gas_case_id","case_status","hold_reason","gmail_status","article_url","reply_events","click_events","open_events","appointment_status","starts_at","meet_url","updated_at")
        columns_activity = ("gas_case_id","record_type","state","gmail_draft_id","gmail_message_id","gmail_thread_id","previous_status","occurred_at","article_url","event_type","confidence","engagement_at","appointment_status","starts_at","meet_url")
        columns_holds = ("source_tab","source_row_number","candidate_case_id","candidate_clinic_id","candidate_clinic_uuid","mapping_status","mapping_reason","snapshot_retrieved_at")
        return {"available": True, "sales_case_count": counts[0], "source_row_count": counts[1],
                "open_case_count": counts[3], "hold_case_count": counts[4],
                "hold_row_count": counts[5],
                "last_synced_at": counts[2].isoformat() if counts[2] else "未同期",
                "cases": [dict(zip(columns_cases, row)) for row in cases],
                "activities": [dict(zip(columns_activity, row)) for row in activities],
                "holds": [dict(zip(columns_holds, row)) for row in holds],
                "hold_count_shown": len(holds)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
