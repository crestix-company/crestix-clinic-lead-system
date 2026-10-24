"""Local read-only viewer for a connector-exported Google Sheets workbook."""
from __future__ import annotations

import json
import importlib
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
import re
import unicodedata

import pandas as pd
import streamlit as st

from src.outreach.phase_g_sheets_snapshot import (
    CASE_TABS, import_action_counts, preview_import_actions, read_workbook_snapshot,
    read_csv_snapshot, reconcile_mail_state,
)
from src.outreach import phase_h_reconciliation

# Streamlit reruns this file in the existing local process. Reload the Phase H
# helper as well so the long-running port 8514 viewer sees local code updates.
phase_h_reconciliation = importlib.reload(phase_h_reconciliation)
classify_review_rows = phase_h_reconciliation.classify_review_rows
plan_supabase_import = phase_h_reconciliation.plan_supabase_import
import_plan_counts = phase_h_reconciliation.import_plan_counts


PHASE_I_SHEETS = Path("/private/tmp/clinic_phase_i_source.xlsx")
PHASE_I_GMAIL = Path("/private/tmp/clinic_phase_i_gmail_readonly.json")
PHASE_I_UUIDS = Path("/private/tmp/clinic_phase_i_uuid_matches.json")
SHEETS_FILE = Path(os.environ.get("PHASE_G_SHEETS_XLSX_PATH", str(PHASE_I_SHEETS if PHASE_I_SHEETS.exists() else "/private/tmp/clinic_phase_g_source.xlsx")))
GMAIL_FILE = Path(os.environ.get("PHASE_G_GMAIL_JSON_PATH", str(PHASE_I_GMAIL if PHASE_I_GMAIL.exists() else "/private/tmp/clinic_phase_g_gmail.json")))
EXPECTED_TITLE = "第一営業部_メール日程調整管理"

st.set_page_config(page_title="Clinic Lead 営業管理｜Read Only", layout="wide")
st.title("Clinic Lead System｜営業案件管理（実データ Read Only）")
st.warning("READ ONLY SNAPSHOT — Google Sheets/Gmailを閲覧したローカルスナップショットです。Sheets・Gmail・DBへの書込機能はありません。")

if not SHEETS_FILE.is_file():
    st.error("Sheetsのローカルスナップショットがありません。PHASE_G_SHEETS_XLSX_PATHを確認してください。")
    st.stop()

tabs = read_workbook_snapshot(SHEETS_FILE)
tab_by_name = {tab.name: tab for tab in tabs}
gmail_payload = json.loads(GMAIL_FILE.read_text(encoding="utf-8")) if GMAIL_FILE.exists() else {}
if GMAIL_FILE == PHASE_I_GMAIL:
    manifest_path = Path("/private/tmp/clinic_phase_i_sheets_manifest.json")
    if manifest_path.exists():
        gmail_payload["sheets_retrieved_at"] = json.loads(manifest_path.read_text(encoding="utf-8")).get("retrieved_at")
    gmail_payload["snapshot_type"] = "phase_i_google_gmail_read_only"
uuid_match_payload = json.loads(PHASE_I_UUIDS.read_text(encoding="utf-8")) if PHASE_I_UUIDS.exists() else {}
clinic_master_by_case = {str(item.get("gas_case_id")): item for item in uuid_match_payload.get("records", []) if item.get("gas_case_id")}
known_uuid_by_gas_case_id = {key: str(item.get("clinic_uuid") or "") for key, item in clinic_master_by_case.items() if item.get("status") == "MATCHED" and item.get("clinic_uuid")}
known_clinic_id_by_gas_case_id = {key: str(item.get("clinic_id") or "") for key, item in clinic_master_by_case.items() if item.get("status") == "MATCHED" and item.get("clinic_id")}
gmail_by_draft = {str(x.get("draft_id", "")): x for x in gmail_payload.get("drafts", []) if x.get("draft_id")}
observed_at = gmail_payload.get("retrieved_at") or "未取得"
snapshot_kind = "実取得スナップショット（Phase I READ ONLY）" if gmail_payload.get("snapshot_type") == "phase_i_google_gmail_read_only" else ("実取得スナップショット（Phase G取得済み）" if gmail_payload else "Gmailスナップショット未取得")

with st.sidebar:
    st.caption(f"Spreadsheet: {EXPECTED_TITLE}")
    st.caption(f"取得時刻: {gmail_payload.get('sheets_retrieved_at') or datetime.fromtimestamp(SHEETS_FILE.stat().st_mtime).astimezone().isoformat(timespec='seconds')}")
    st.caption(f"Gmail対象: {gmail_payload.get('mailbox', '未接続')}")
    st.caption(f"Gmail同期: {observed_at}")
    st.caption(f"データ種別: {snapshot_kind} / Sheets取得: {gmail_payload.get('sheets_retrieved_at') or '未取得'} / Gmail取得: {observed_at}")
    search = st.text_input("医院名・案件ID・メールで検索", key="phase_g_search").strip().casefold()

case_tab = tab_by_name.get("案件管理")
email_tab = tab_by_name.get("メール管理")
progress_tab = tab_by_name.get("328医院_現在進捗_20261018")
main_rows = []
if case_tab:
    for idx, values in enumerate(case_tab.rows):
        row = dict(zip(case_tab.headers, values))
        row["元タブ"] = case_tab.name
        row["元行"] = case_tab.source_rows[idx]
        main_rows.append(row)

def _norm(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")).casefold())

def _show(value):
    return value if value is not None and str(value).strip() else "未取得"

identity_by_id = {}
identity_conflicts = set()
audit_tab = tab_by_name.get("記事掲載_Gmail照合_20261016")
if audit_tab:
    for values in audit_tab.rows:
        item = dict(zip(audit_tab.headers, values))
        clinic_id, clinic_uuid = str(item.get("clinic_id") or "").strip(), str(item.get("Clinic UUID") or "").strip()
        if clinic_id and clinic_uuid:
            if clinic_id in identity_by_id and identity_by_id[clinic_id] != clinic_uuid:
                identity_conflicts.add(clinic_id)
            identity_by_id[clinic_id] = clinic_uuid
for clinic_id in identity_conflicts:
    identity_by_id.pop(clinic_id, None)

progress_by_email = {}
if progress_tab:
    for values in progress_tab.rows:
        item = dict(zip(progress_tab.headers, values))
        email = _norm(item.get("メールアドレス"))
        clinic_id = str(item.get("clinic_id") or "").strip()
        if email and clinic_id:
            progress_by_email.setdefault(email, set()).add(clinic_id)

mail_by_case = {}
if email_tab:
    for idx, values in enumerate(email_tab.rows):
        item = dict(zip(email_tab.headers, values))
        item["__source_row__"] = email_tab.source_rows[idx]
        case_id = str(item.get("案件ID") or "").strip()
        if case_id:
            mail_by_case.setdefault(case_id, []).append(item)

summary_cols = st.columns(4)
summary_cols[0].metric("取込タブ", len(tabs))
summary_cols[1].metric("案件管理 行", len(main_rows))
summary_cols[2].metric("メール管理 行", len(email_tab.rows) if email_tab else 0)
summary_cols[3].metric("328医院進捗 行", len(progress_tab.rows) if progress_tab else 0)

st.subheader("医院別案件一覧")
if main_rows:
    visible = []
    reconcile_counts = Counter()
    for row in main_rows:
        if search and not any(search in str(v or "").casefold() for v in row.values()):
            continue
        case_id = str(row.get("案件ID") or "").strip()
        recipients = {x.casefold() for x in re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", str(row.get("メールアドレス") or ""), re.I)}
        case_mail_rows = mail_by_case.get(case_id, [])
        matching_mail = case_mail_rows if len(case_mail_rows) == 1 else [
            m for m in case_mail_rows
            if recipients & {x.casefold() for x in re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", str(m.get("宛先") or ""), re.I)}
        ]
        status_row = matching_mail[0] if len(matching_mail) == 1 else row
        draft_id = str(status_row.get("下書きID") or "")
        sheet_status = str(status_row.get("状態") or row.get("送信有無") or "未取得")
        reconciled = reconcile_mail_state(status_row, gmail_payload)
        if len(matching_mail) > 1:
            reconciled = {"gmail_state": "UNKNOWN", "match": "REVIEW", "hold_reason": "同一案件IDにメール管理行が複数あり一意照合できません"}
        clinic_id = str(row.get("clinic_id") or row.get("管理ID") or "").strip()
        if not clinic_id and recipients:
            ids = set().union(*(progress_by_email.get(_norm(address), set()) for address in recipients))
            clinic_id = next(iter(ids)) if len(ids) == 1 else ""
        source_uuid = str(row.get("Clinic UUID") or row.get("clinic_uuid") or "").strip()
        master_match = clinic_master_by_case.get(case_id, {})
        resolved_uuid = str(master_match.get("clinic_uuid") or "") if master_match.get("status") == "MATCHED" else ""
        resolved_clinic_id = str(master_match.get("clinic_id") or "") if master_match.get("status") == "MATCHED" else ""
        if source_uuid and resolved_uuid and source_uuid != resolved_uuid:
            resolved_uuid = ""
            master_match_reason = "既存確認済みUUIDとClinic Master候補が不一致（HOLD）"
        elif clinic_id and resolved_clinic_id and clinic_id != resolved_clinic_id:
            resolved_uuid = ""
            master_match_reason = "Sheets Clinic IDとClinic Master候補が不一致（HOLD）"
        else:
            master_match_reason = str(master_match.get("reason") or ("既存明示UUID" if source_uuid else "未照合"))
        clinic_uuid = source_uuid or identity_by_id.get(clinic_id, "") or resolved_uuid
        if not clinic_id and resolved_clinic_id:
            clinic_id = resolved_clinic_id
        hold_reasons = [reconciled["hold_reason"]] if reconciled["hold_reason"] else []
        if not clinic_id or not clinic_uuid:
            hold_reasons.append("Clinic ID/UUIDを一意に紐付けできません")
        gmail_status, mismatch = reconciled["gmail_state"], reconciled["match"]
        reconcile_counts[mismatch] += 1
        visible.append({
            "医院名": _show(row.get("クリニック名")), "GAS案件ID": _show(row.get("案件ID")),
            "Clinic ID": clinic_id or "未取得",
            "Clinic UUID": _show(clinic_uuid or row.get("Clinic UUID") or row.get("clinic_uuid")),
            "Clinic Master照合": master_match_reason,
            "IS担当": _show(row.get("IS担当")), "FS担当": _show(row.get("担当FS")),
            "Sheets状態": _show(sheet_status), "Gmail状態": gmail_status, "照合": mismatch,
            "記事URL": _show(row.get("掲載記事URL") or row.get("実掲載記事URL")),
            "返信": _show(row.get("返信有無")), "初回返信日時": _show(row.get("初回返信日時")),
            "自動返信数": _show(row.get("自動返信数")),
            "記事クリック": _show(row.get("記事クリック有無")),
            "初回クリック日時": _show(row.get("初回クリック日時")),
            "高信頼クリック数": _show(row.get("高信頼クリック数")),
            "アポイント": _show(row.get("アポ日時") or row.get("アポ確定日時")),
            "Sheets同期日時": _show(status_row.get("最終同期日時")),
            "Gmail Thread ID": _show(status_row.get("ThreadID") or row.get("下書きThreadID") or row.get("送信ThreadID")),
            "Sheets元行": f"{row['元タブ']}!{row['元行']}", "HOLD理由": " / ".join(hold_reasons),
            "宛先": _show(row.get("メールアドレス") or row.get("宛先")), "Draft ID": draft_id or "未取得",
        })
    st.dataframe(pd.DataFrame(visible).fillna("").astype(str), width="stretch", hide_index=True)
    st.caption(f"検索結果 {len(visible)} 件。表示値はSheetsの取得時スナップショットです。")
    st.caption(f"Gmail照合: 一致 {reconcile_counts.get('一致', 0)} 件 / REVIEW {reconcile_counts.get('REVIEW', 0)} 件。IDの一意一致のみ状態確定します。")
    st.caption(f"Clinic UUID取得 {sum(row['Clinic UUID'] != '未取得' for row in visible)} / {len(visible)} 件。未紐付けは空欄推定せずHOLD理由に表示します。")
else:
    st.info("案件管理タブの有効行を取得できませんでした。")

with st.expander("Phase J｜隔離ローカルDB 営業履歴", expanded=True):
    try:
        from src.outreach.phase_j_local_db import load_local_outreach_dashboard
        local_db = load_local_outreach_dashboard()
    except Exception as exc:
        local_db = {"available": False, "reason": f"ローカルDB未接続: {type(exc).__name__}"}
    if not local_db.get("available"):
        st.info(local_db.get("reason", "Phase J隔離PostgreSQLが未起動です。Production/Shadow接続は使用しません。"))
    else:
        st.caption(f"接続先: 隔離ローカルDB / 最終同期: {local_db['last_synced_at']} / Source rows: {local_db['source_row_count']}")
        db_case_cols = st.columns(4)
        db_case_cols[0].metric("Clinic/GAS紐付案件", local_db["sales_case_count"])
        db_case_cols[1].metric("OPEN", local_db["open_case_count"])
        db_case_cols[2].metric("HOLD", local_db["hold_case_count"])
        db_case_cols[3].metric("HOLD原票行", local_db["hold_row_count"])
        st.dataframe(pd.DataFrame(local_db["cases"]).fillna("").astype(str), width="stretch", hide_index=True)
        st.markdown("**メール状態・返信・記事・追跡・アポイント**")
        st.dataframe(pd.DataFrame(local_db["activities"]).fillna("").astype(str), width="stretch", hide_index=True)
        st.markdown("**HOLD原票（元タブ・元行保持）**")
        st.dataframe(pd.DataFrame(local_db["holds"]).fillna("").astype(str), width="stretch", hide_index=True)

with st.expander("GAS連携｜10件ローカルテスト（LOCAL MOCK ONLY）", expanded=True):
    st.warning("テストモードです。GAS・Sheets・Gmail・Supabase・Cloud Runには接続せず、Gmail送信や実下書き作成もしません。")
    st.caption("Phase Jのローカルスナップショットを最大6件読み、キャンペーンID不足はHOLDします。残りは合成fixtureとして明示し、ローカルMockで照合します。")
    if st.button("10件テストを実行（GAS/Gmail Mock）", key="local_gas_validation_run"):
        try:
            from src.outreach.local_gas_validation import run_local_gas_validation
            st.session_state["local_gas_validation_result"] = run_local_gas_validation()
        except Exception as exc:
            st.session_state["local_gas_validation_error"] = type(exc).__name__
            st.session_state.pop("local_gas_validation_result", None)
    validation = st.session_state.get("local_gas_validation_result")
    if validation:
        st.caption(f"Run ID: {validation['run_id']} / 対象 {validation['count']}件 / 2回実行してDB upsert・Mock重複を確認")
        result_cols = st.columns(3)
        result_cols[0].metric("MATCHED", validation["matched"])
        result_cols[1].metric("HOLD", validation["hold"])
        result_cols[2].metric("ERROR", validation["error"])
        st.write(f"再実行時のID・判定一致: {'PASS' if validation['repeat_idempotent'] else 'FAIL'} / GAS Mock ALREADY_REGISTERED {validation['already_registered']}件")
        validation_rows = validation["rows"]
    elif st.session_state.get("local_gas_validation_error"):
        st.error(f"ローカルテストを完了できませんでした（{st.session_state['local_gas_validation_error']}）。既存DB・外部接続は変更していません。")
        validation_rows = []
    else:
        try:
            from src.outreach.local_gas_validation import load_local_gas_validation_results
            validation_rows = load_local_gas_validation_results()
        except Exception:
            validation_rows = []
    if validation_rows:
        st.dataframe(pd.DataFrame([{
            "データ種別": "実スナップショット" if row["source_kind"] == "LOCAL_SNAPSHOT" else "合成fixture",
            "GAS案件ID": row["gas_case_id"], "Clinic ID": row["clinic_id"],
            "Clinic UUID": row["clinic_uuid"], "Campaign ID": row["campaign_id"],
            "判定": row["result_status"], "GAS Mock": row.get("gas_result_status", ""),
            "Mock Draft ID": row.get("mock_draft_id", ""), "HOLD/ERROR理由": row["reason"],
        } for row in validation_rows]).fillna("").astype(str), width="stretch", hide_index=True)
        previews = [row["draft_preview"] for row in validation_rows if row.get("draft_preview")]
        if previews:
            st.markdown("**Gmail下書きプレビュー（合成fixture / 未送信）**")
            for preview in previews:
                st.caption(preview["mode"])
                st.write(f"宛先: {preview['to']} / 件名: {preview['subject']}")
                st.code(preview["body"], language="text")

# Phase H: classify review rows without merging separate campaign histories.
case_review_source = []
for idx, row in enumerate(main_rows):
    item = dict(row)
    item["元行"] = row.get("元行", idx + 2)
    case_review_source.append(item)
mail_review_source = []
if email_tab:
    for idx, values in enumerate(email_tab.rows):
        item = dict(zip(email_tab.headers, values))
        item["元行"] = email_tab.source_rows[idx]
        mail_review_source.append(item)
review_report = classify_review_rows(case_review_source, mail_review_source, gmail_payload,
                                     known_uuid_by_clinic_id=identity_by_id,
                                     known_uuid_by_gas_case_id=known_uuid_by_gas_case_id)
phase_h_plan = plan_supabase_import(tabs, known_uuid_by_clinic_id=identity_by_id,
                                   gmail_snapshot=gmail_payload,
                                   known_uuid_by_gas_case_id=known_uuid_by_gas_case_id,
                                   known_clinic_id_by_gas_case_id=known_clinic_id_by_gas_case_id,
                                   existing_target_tables={"email_contact_events"})
phase_h_plan_counts = import_plan_counts(phase_h_plan)

with st.expander("Phase I｜Shadow DB取込前レビュー・状態差分", expanded=True):
    st.caption(f"分類元: {snapshot_kind} / Sheets取得 {gmail_payload.get('sheets_retrieved_at') or '未取得'} / Gmail取得 {observed_at} / Clinic Master照合 {uuid_match_payload.get('retrieved_at') or '未取得'}")
    cols = st.columns(4)
    cols[0].metric("案件管理 REVIEW", review_report["case_review_count"])
    cols[1].metric("メール管理 REVIEW", review_report["mail_review_count"])
    cols[2].metric("重複除外GAS案件ID", review_report["unique_review_cases"])
    cols[3].metric("Clinic Master MATCHED / REVIEW", f"{uuid_match_payload.get('matched_count', 0)} / {uuid_match_payload.get('review_count', 0)}")
    st.caption(f"タブ別REVIEW件数は別々に表示します。重複除外GAS案件ID {review_report['unique_review_cases']} 件 / 案件IDなしの行 {review_report['review_rows_without_case_id']} 件。医院名だけでは統合しません。")
    st.markdown("**案件管理 REVIEW理由（行単位、複数理由あり）**")
    st.dataframe(pd.DataFrame([{"理由": k, "行数": v} for k, v in review_report["case_reason_counts"].items()]), hide_index=True, width="stretch")
    st.markdown("**メール管理 REVIEW理由（行単位、複数理由あり）**")
    st.dataframe(pd.DataFrame([{"理由": k, "行数": v} for k, v in review_report["mail_reason_counts"].items()]), hide_index=True, width="stretch")
    case_uuid_missing = sum(not (str(row.get("Clinic UUID") or row.get("clinic_uuid") or "").strip() or str(clinic_master_by_case.get(str(row.get("案件ID") or ""), {}).get("clinic_uuid") or "").strip()) for row in main_rows)
    st.write(f"Clinic Master READ ONLY: UUID追加照合 MATCHED {uuid_match_payload.get('matched_count', 0)} / REVIEW {uuid_match_payload.get('review_count', 0)}。取得 {uuid_match_payload.get('retrieved_at') or '未取得'}。既存明示UUIDは維持し衝突はHOLD。案件管理UUID未取得 {case_uuid_missing} 行。")
    st.markdown("**Gmail/Sheetsの状態照合**")
    gmail_counts = {name: status.get("count", 0) for name, status in gmail_payload.get("search_status", {}).items()}
    st.write(f"Gmail最新READ ONLY件数: Draft {len(gmail_by_draft)} / Scheduled {gmail_counts.get('scheduled', 0)} / Sent {gmail_counts.get('sent', 0)} / Replied {gmail_counts.get('replied', 0)}（送信済Threadと一致した受信） / Trash {gmail_counts.get('trash', 0)}。案件管理REVIEW {review_report['case_review_count']} / メール管理REVIEW {review_report['mail_review_count']}。")
    st.markdown("**Supabase取込前判定（営業履歴ターゲット未作成）**")
    count_cols = st.columns(4)
    count_cols[0].metric("INSERT", 0)
    count_cols[1].metric("UPDATE", 0)
    count_cols[2].metric("NOOP", 0)
    count_cols[3].metric("Shadow比較HOLD", len(phase_h_plan))
    st.caption("Shadow DBで確認できた営業系テーブルは email_contact_events（0行）と clinic_email_summary VIEWです。提案取込テーブルが未作成のため、比較不能はHOLDとしINSERT/UPDATE/NOOPを確定しません。")
    review_view = [{"タブ": row["source_tab"], "元行": row["source_row"], "GAS案件ID": row["gas_case_id"],
                    "Clinic ID": row["clinic_id"], "Clinic UUID": row["clinic_uuid"],
                    "Sheets状態": row["sheet_state"], "Gmail状態": row["gmail_state"],
                    "理由": " / ".join(row["reasons"])}
                   for row in (*review_report["case_rows"], *review_report["mail_rows"])]
    st.dataframe(pd.DataFrame(review_view).fillna("").astype(str), hide_index=True, width="stretch")

with st.expander("Clinic Master UUID照合理由", expanded=False):
    st.dataframe(pd.DataFrame([{"理由": key, "件数": value} for key, value in uuid_match_payload.get("reason_counts", {}).items()]), hide_index=True, width="stretch")

with st.expander("Gmail照合サマリー（READ ONLY）", expanded=True):
    st.write(f"対象 mailbox: **{gmail_payload.get('mailbox', '未接続')}**")
    st.write(f"現存Draft: **{len(gmail_by_draft)} 件**（Draft ID一致で確認）")
    search_status = gmail_payload.get("search_status", {})
    st.write("Gmail検索件数: " + " / ".join(f"{name} {item.get('count', 0)}件" for name, item in search_status.items()))
    st.write(f"Scheduled/Sent/Trash/返信の検索: **{gmail_payload.get('non_draft_search_status', '未取得')}**")
    st.write("Draft IDが見つからない案件は、Scheduled/Sent/Trash照合が完了するまでUNKNOWN / REVIEWとして扱います。")

if email_tab:
    with st.expander("メール管理の全件Gmail ID照合", expanded=False):
        mail_view = []
        mail_matches = Counter()
        mail_case_counts = Counter(str(dict(zip(email_tab.headers, values)).get("案件ID") or "").strip()
                                   for values in email_tab.rows)
        for idx, values in enumerate(email_tab.rows):
            item = dict(zip(email_tab.headers, values))
            state = reconcile_mail_state(item, gmail_payload)
            if mail_case_counts[str(item.get("案件ID") or "").strip()] > 1:
                state = {"gmail_state": "UNKNOWN", "match": "REVIEW", "hold_reason": "同一案件IDのメール管理行が複数"}
            mail_matches[state["match"]] += 1
            mail_view.append({
                "GAS案件ID": _show(item.get("案件ID")), "宛先": _show(item.get("宛先")),
                "件名": _show(item.get("件名")), "Sheets状態": _show(item.get("状態")),
                "Gmail状態": state["gmail_state"], "照合": state["match"],
                "元行": email_tab.source_rows[idx], "HOLD理由": state["hold_reason"],
            })
        st.caption(f"全 {len(mail_view)} 行 / 一致 {mail_matches.get('一致', 0)} / REVIEW {mail_matches.get('REVIEW', 0)}。元行番号を保持。")
        st.dataframe(pd.DataFrame(mail_view).fillna("").astype(str), width="stretch", hide_index=True)

st.subheader("全タブ原本行・取込プレビュー")
tab_names = [t.name for t in tabs]
selected_tab_name = st.selectbox("表示タブ", tab_names, index=tab_names.index("案件管理") if "案件管理" in tab_names else 0)
selected_tab = tab_by_name[selected_tab_name]
raw_data = []
for source_row, values in zip(selected_tab.source_rows, selected_tab.rows):
    obj = {header: value for header, value in zip(selected_tab.headers, values)}
    obj["__source_row__"] = source_row
    raw_data.append(obj)
if search:
    raw_data = [r for r in raw_data if any(search in str(v or "").casefold() for v in r.values())]
st.dataframe(pd.DataFrame(raw_data).fillna("").astype(str), width="stretch", hide_index=True)
st.caption(f"原本行を保持。表示 {len(raw_data)} / タブ総行 {len(selected_tab.rows)}。行番号は元シートの2行目から追跡します。")

st.subheader("CSV一括取込（ローカルプレビューのみ）")
uploads = st.file_uploader("CSVを複数選択", type=["csv"], accept_multiple_files=True, key="phase_g_csv_upload")
uploaded_tabs = []
for upload in uploads or []:
    uploaded_tabs.append(read_csv_snapshot(upload.name, upload.getvalue()))
if uploaded_tabs:
    st.caption(f"CSV {len(uploaded_tabs)}ファイル / {sum(len(t.rows) for t in uploaded_tabs)}行。元ファイル名とレコード行番号を保持。")

if st.button("全対象タブの同期プレビューを計算", key="phase_g_preview"):
    # Use only an explicit clinic_id ↔ UUID pair from the sheet's audit tab.
    mapping: dict[str, str] = {}
    audit = tab_by_name.get("記事掲載_Gmail照合_20261016")
    conflicts: set[str] = set()
    if audit:
        for values in audit.rows:
            row = dict(zip(audit.headers, values))
            cid, uid = str(row.get("clinic_id") or "").strip(), str(row.get("Clinic UUID") or "").strip()
            if cid and uid:
                if cid in mapping and mapping[cid] != uid:
                    conflicts.add(cid)
                mapping[cid] = uid
    for cid in conflicts:
        mapping.pop(cid, None)
    actions = preview_import_actions((*tabs, *uploaded_tabs), known_uuid_by_clinic_id=mapping)
    counts = import_action_counts(actions)
    st.session_state["phase_g_actions"] = actions
    st.session_state["phase_g_action_counts"] = counts
    st.session_state["phase_h_import_plan"] = plan_supabase_import(
        (*tabs, *uploaded_tabs), known_uuid_by_clinic_id=mapping, gmail_snapshot=gmail_payload,
        known_uuid_by_gas_case_id=known_uuid_by_gas_case_id,
        known_clinic_id_by_gas_case_id=known_clinic_id_by_gas_case_id,
        existing_target_tables={"email_contact_events"})

counts = st.session_state.get("phase_g_action_counts")
if counts:
    st.write("Sheets内 Clinic ID/UUID対応候補（SupabaseのINSERT/UPDATE/NOOP判定ではありません）")
    cols = st.columns(4)
    for col, name in zip(cols, ("INSERT", "UPDATE", "NOOP", "HOLD")):
        col.metric(name, counts[name])
    st.caption("ここでのINSERT等はローカルのSheets対応プレビューです。DB既存行比較は下のSupabase判定にのみ表示し、未作成テーブルへの行はHOLDします。")
    actions = st.session_state.get("phase_g_actions", ())
    st.dataframe(pd.DataFrame([{k: row[k] for k in ("action", "source_tab", "source_row", "clinic_id", "clinic_uuid", "gas_case_id", "reason")} for row in actions]).fillna("").astype(str), width="stretch", hide_index=True)
    st.markdown("**Supabase論理テーブル別プレビュー（DB比較不能はHOLD）**")
    plan = st.session_state.get("phase_h_import_plan", ())
    if plan:
        hcounts = import_plan_counts(plan)
        cols = st.columns(4)
        cols[0].metric("INSERT", 0)
        cols[1].metric("UPDATE", 0)
        cols[2].metric("NOOP", 0)
        cols[3].metric("HOLD", len(plan))
        st.caption("既存営業履歴ターゲットが未作成のため、UUID一致候補を含めて全行HOLDです。原票行、Clinic UUID、GAS案件ID、Gmail状態、元タブ・元行を保持し、同一医院の別案件やメール履歴は統合しません。")
        by_table = Counter(row["logical_table"] for row in plan)
        st.dataframe(pd.DataFrame([{"論理テーブル": key, "行数": value} for key, value in sorted(by_table.items())]), hide_index=True, width="stretch")
    st.dataframe(pd.DataFrame([{k: row[k] for k in ("logical_table", "raw_snapshot_table", "target_table", "source_tab", "source_row", "clinic_id", "clinic_uuid", "gmail_state", "action", "reason", "target_compared")} for row in plan]).fillna("").astype(str), width="stretch", hide_index=True)
