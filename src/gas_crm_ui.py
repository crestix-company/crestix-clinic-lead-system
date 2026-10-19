"""GAS / SalesNest CRM read-only preflight screen."""
from __future__ import annotations
import csv
from io import StringIO
import pandas as pd
import streamlit as st
from src.outreach.gas_crm_preview import (
    CANDIDATE_COLUMNS, GAS_HISTORY_COLUMNS, CRM_MAPPING_COLUMNS,
    PreviewInputError, preview_cases, read_csv_snapshot, to_safe_csv,
)

def _sample_headers(columns: tuple[str, ...]) -> bytes:
    out = StringIO()
    csv.writer(out).writerow(columns)
    return out.getvalue().encode("utf-8-sig")

def gas_crm_readonly_page() -> None:
    st.header("GAS・CRM連携")
    st.info("現在は登録前のREAD ONLYチェックです。GAS、Gmail、CRM、Supabaseへの書込やメール送信は行いません。")
    st.caption("医院の基本情報はClinic Master、営業活動はSalesNest CRMで管理。Clinic UUIDとCRM会社IDは区別して紐付けます。")
    a,b,c = st.columns(3)
    a.metric("GAS実登録", "未接続")
    b.metric("CRM実取込", "未接続")
    c.metric("確認モード", "READ ONLY")
    with st.expander("機能の現状・次に必要な作業"):
        st.markdown(
            "- 実装済み：Clinic UUID照合とoperation keyによる重複防止（既存ロジックを再利用）\n"
            "- 進行中：GAS PythonクライアントPR #98のmain反映確認\n"
            "- 未完了：GAS Web App本番接続、CRM会社取込APIと共通IDの照合\n"
            "- この画面は実登録もGmail下書き作成も実行しません"
        )
    st.subheader("CSVで案件・CRM紐付けを事前検証")
    st.caption("UTF-8 CSVのみ。最大3MB・5,000行。アップロードしたデータはメモリ内だけで照合します。")
    x,y,z=st.columns(3)
    x.download_button("候補CSVひな形", _sample_headers(CANDIDATE_COLUMNS),
                      "gas_candidates_template.csv", mime="text/csv", key="gas_prep_candidate_header")
    y.download_button("GAS履歴CSVひな形", _sample_headers(GAS_HISTORY_COLUMNS),
                      "gas_history_template.csv", mime="text/csv", key="gas_prep_history_header")
    z.download_button("CRM対応表ひな形", _sample_headers(CRM_MAPPING_COLUMNS),
                      "crm_mapping_template.csv", mime="text/csv", key="gas_prep_crm_header")
    candidate_file=x.file_uploader("候補CSV（必須）", type="csv", key="gas_prep_candidates")
    gas_file=y.file_uploader("GAS履歴CSV（必須）", type="csv", key="gas_prep_history")
    crm_file=z.file_uploader("CRM紐付けCSV（任意）", type="csv", key="gas_prep_crm")
    verified=st.checkbox("GAS・Sheets・Gmailの全履歴を別途確認済み（プレビュー用の手動申告）",
                         value=False, key="gas_prep_history_verified",
                         help="システムによる外部履歴検証の代わりにはなりません。未確認なら全件HOLDです。")
    if candidate_file is None or gas_file is None:
        st.warning("候補CSVとGAS履歴CSVを指定してください。")
        return
    try:
        candidates=read_csv_snapshot(candidate_file.getvalue(), required_columns=CANDIDATE_COLUMNS)
        history=read_csv_snapshot(gas_file.getvalue(), required_columns=GAS_HISTORY_COLUMNS)
        mappings=(read_csv_snapshot(crm_file.getvalue(), required_columns=CRM_MAPPING_COLUMNS)
                  if crm_file is not None else [])
        results=preview_cases(candidates, history, mappings, history_verified=verified)
    except PreviewInputError as exc:
        st.error(str(exc))
        return
    col1,col2,col3,col4=st.columns(4)
    col1.metric("候補", len(results))
    col2.metric("GASプレビューOK", sum(r.gas_status=="PREVIEW_OK" for r in results))
    col3.metric("GAS HOLD/REVIEW", sum(r.gas_status!="PREVIEW_OK" for r in results))
    col4.metric("CRM紐付け確認", sum(r.crm_status=="MAPPED" for r in results))
    if results:
        st.dataframe(pd.DataFrame([{
            "Clinic UUID":r.clinic_uuid, "医院名":r.clinic_name, "Campaign":r.campaign_id,
            "GAS判定":r.gas_status, "CRM判定":r.crm_status, "理由":" / ".join(r.reasons),
        } for r in results]), hide_index=True, use_container_width=True)
        st.download_button("判定結果CSVをダウンロード", to_safe_csv(results),
                           "gas_crm_preflight_result.csv", mime="text/csv", key="gas_prep_export")
    st.warning("PREVIEW_OKはCSVと手動申告に基づく事前判定です。本番登録・CRM取込の実行許可ではありません。")
