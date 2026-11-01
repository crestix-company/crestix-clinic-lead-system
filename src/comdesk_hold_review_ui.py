"""Local-only Comdesk HOLD review page backed by the isolated review ledger."""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import streamlit as st

from src.master.comdesk_hold_review import (
    DECISION_LABELS, DECISIONS, HoldReviewStore, cases_from_reports,
    default_review_db_path,
)


def _store() -> HoldReviewStore:
    return HoldReviewStore(default_review_db_path())


def render_comdesk_hold_review_page() -> None:
    st.title("Comdesk HOLD確認")
    st.caption("隔離ローカルレビューです。Clinicマスター、UUID、HP・調査情報は更新しません。")
    store = _store()

    report_dir = Path(os.environ.get("CLINIC_COMDESK_REVIEW_REPORT_DIR", "/private/tmp/comdesk-hold-analysis-20261021"))
    csv_dir = Path(os.environ.get("CLINIC_COMDESK_REVIEW_INPUT_DIR", str(Path.home() / "Downloads")))
    if st.button("最新のREAD ONLY照合HOLDをレビュー台帳へ読み込む", key="comdesk_review_seed"):
        try:
            snapshot_path = Path(os.environ.get("CLINIC_COMDESK_REVIEW_CANDIDATE_SNAPSHOT", "/private/tmp/comdesk-hold-human-review-20261021/candidate_snapshot.json"))
            cases = cases_from_reports(report_dir, csv_dir, snapshot_path if snapshot_path.is_file() else None)
            inserted = store.seed(cases)
            st.success(f"{len(cases):,}候補行を確認し、{inserted:,}件を隔離台帳へ追加しました。")
            st.rerun()
        except Exception as exc:
            st.error(f"レビュー対象を読み込めませんでした: {exc}")
    summary = store.summary()
    statuses = summary["statuses"]
    cols = st.columns(5)
    cols[0].metric("HOLD候補行", f"{summary['total_candidate_rows']:,}")
    cols[1].metric("入力行", f"{summary['total_input_rows']:,}")
    cols[2].metric("ユニーク医院識別", f"{summary['unique_input_clinics']:,}")
    cols[3].metric("未確認", f"{statuses.get('UNREVIEWED', 0):,}")
    cols[4].metric("UUID矛盾候補行", f"{summary['uuid_conflicts']:,}")
    cols = st.columns(5)
    cols[0].metric("同一医院判定候補行", f"{statuses.get('SAME_CLINIC', 0):,}")
    cols[1].metric("別医院判定候補行", f"{statuses.get('DIFFERENT_CLINIC', 0):,}")
    cols[2].metric("追加調査候補行", f"{statuses.get('NEEDS_RESEARCH', 0):,}")
    cols[3].metric("保留候補行", f"{statuses.get('HOLD', 0):,}")
    cols[4].metric("判定済み候補行", f"{summary['total_candidate_rows'] - statuses.get('UNREVIEWED', 0):,}")

    rows = store.rows()
    if not rows:
        st.info("レビュー対象はまだありません。照合レポートを読み込むか、レビュー台帳CSVを取り込んでください。")
    else:
        reasons = sorted({str(r.get("hold_reason_code", "")) for r in rows if r.get("hold_reason_code")})
        sources = sorted({str(r.get("source_file", "")) for r in rows if r.get("source_file")})
        differences = sorted({str(r.get("address_difference_class", "")) for r in rows if r.get("address_difference_class")})
        a, b, c, d, e = st.columns(5)
        reason = a.selectbox("HOLD理由", ["すべて", *reasons], key="comdesk_hold_reason")
        status = b.selectbox("レビュー状態", ["すべて", "UNREVIEWED", *DECISION_LABELS.keys()], key="comdesk_hold_status")
        source = c.selectbox("入力元", ["すべて", *sources], key="comdesk_hold_source")
        conflict = d.selectbox("UUID競合", ["すべて", "あり", "なし"], key="comdesk_hold_uuid_conflict")
        difference = e.selectbox("住所・名称差異", ["すべて", *differences], key="comdesk_hold_difference")
        keyword = st.text_input("医院名・UUID・内部ID・入力行で検索", key="comdesk_hold_search")
        status_code = DECISIONS.get(status, status)
        shown = store.rows(status=status_code, reason=reason, source=source, uuid_conflict=conflict,
                           difference=difference, keyword=keyword)
        list_rows = [{"入力元":r.get("source_file", ""), "行":r.get("source_row_id", ""),
                      "Comdesk UUID":r.get("comdesk_uuid", ""), "医院名":r.get("comdesk_name", ""),
                      "HOLD理由":r.get("hold_reason_code", ""), "候補数":r.get("candidate_count", ""),
                      "候補Clinic ID":r.get("candidate_clinic_id", ""), "状態":DECISION_LABELS.get(r.get("review_status"), "未確認"),
                      "更新日時":r.get("updated_at", "")} for r in shown]
        st.dataframe(pd.DataFrame(list_rows), hide_index=True, use_container_width=True)
        if shown:
            options = [r["review_id"] for r in shown]
            labels = {r["review_id"]: f"{r.get('source_file','')} / 行 {r.get('source_row_id','')} / Clinic {r.get('candidate_clinic_id','')}" for r in shown}
            selected_id = st.selectbox("詳細を開く", options, format_func=labels.get, key="comdesk_hold_selected")
            selected = next(r for r in shown if r["review_id"] == selected_id)
            st.subheader("候補医院の比較")
            left, right = st.columns(2)
            with left:
                st.markdown("#### Comdesk")
                st.write("医院名", selected.get("comdesk_name") or "（未取得）")
                st.write("電話番号", selected.get("comdesk_phone") or "（未取得）")
                st.write("都道府県", selected.get("comdesk_prefecture") or "（未取得）")
                st.write("住所", selected.get("comdesk_address") or "（未取得）")
                st.write("UUID", selected.get("comdesk_uuid") or "（未取得）")
            with right:
                st.markdown("#### Clinic Lead候補")
                st.write("内部ID", selected.get("candidate_clinic_id"))
                st.write("医院名", selected.get("clinic_name") or "（候補詳細なし）")
                st.write("電話番号", selected.get("clinic_phone") or "（候補詳細なし）")
                st.write("都道府県", selected.get("clinic_prefecture") or "（候補詳細なし）")
                st.write("住所", selected.get("clinic_address") or "（候補詳細なし）")
                st.write("Clinic UUID", selected.get("clinic_uuid") or "（候補詳細なし）")
                st.write("医療機関番号 / Clinic key", selected.get("clinic_medical_key") or "（未登録）")
                if selected.get("clinic_hp_status") == "VERIFIED":
                    st.write("確認済み公式HP", selected.get("clinic_official_hp_url") or "（URLなし）")
                if selected.get("clinic_maps_status") == "MAPS_MATCHED_WEBSITE":
                    st.write("Maps登録URL（参考）", selected.get("clinic_maps_url") or "（URLなし）")
                if selected.get("clinic_merged_into"):
                    st.error(f"このClinic IDは統合済みです（merged_into={selected['clinic_merged_into']}）。自動判定対象から外してください。")
            st.write("HOLD理由", selected.get("hold_reason_code", ""))
            st.write("一致根拠", selected.get("matched_fields", ""))
            st.write("不一致", selected.get("mismatched_fields", ""))
            if selected.get("clinic_snapshot_at"):
                st.caption(f"Clinic比較情報の読み取り時刻（UTC）: {selected['clinic_snapshot_at']}。このスナップショットは読み取り専用で、本番更新には使用していません。")
            prior = [r for r in store.prior_reviews(source_row_hash=selected["source_row_hash"],
                candidate_clinic_id=str(selected["candidate_clinic_id"]), review_basis_hash=selected["review_basis_hash"])
                if r["review_id"] != selected["review_id"]]
            if prior:
                st.info("同じ入力行・Clinic候補・照合根拠に一致する過去レビューがあります。参考表示のみで、今回の判定には自動適用していません。")
                st.dataframe(pd.DataFrame(prior), hide_index=True, use_container_width=True)
            siblings = [r for r in rows if r["source_batch_id"] == selected["source_batch_id"]
                        and r["source_row_id"] == selected["source_row_id"]]
            if len(siblings) > 1:
                st.warning("同じ入力行に複数候補があります。候補を一件ずつ確認し、他候補を黙って除外しないでください。")
                st.dataframe(pd.DataFrame([{"Clinic内部ID":r["candidate_clinic_id"], "医院名":r.get("clinic_name", ""),
                                            "UUID競合":r.get("uuid_conflict", ""), "判定":DECISION_LABELS.get(r.get("review_status"), "未確認")} for r in siblings]), hide_index=True)
            history = store.history(selected_id)
            if history:
                st.markdown("#### 判定履歴")
                st.dataframe(pd.DataFrame(history), hide_index=True, use_container_width=True)
            with st.form("comdesk_hold_decision_form"):
                current_decision = DECISION_LABELS.get(selected.get("review_status"), "保留")
                decision_options = [current_decision] + [x for x in ("保留", "追加調査が必要", "別医院と確認", "同一医院と確認") if x != current_decision]
                decision_label = st.selectbox("人間判定", decision_options, key="comdesk_hold_decision")
                reviewer = st.text_input("確認者", value=os.environ.get("USER", ""), key="comdesk_hold_reviewer")
                note = st.text_area("判定根拠メモ（同一医院判定では必須）", key="comdesk_hold_note")
                submit = st.form_submit_button("判定を隔離台帳へ保存")
            if submit:
                try:
                    store.decide(selected_id, expected_revision=int(selected["revision"]),
                                 decision=DECISIONS[decision_label], reviewer_id=reviewer, note=note)
                    st.success("判定を隔離レビュー履歴へ保存しました。Clinicマスターは変更していません。")
                    st.rerun()
                except Exception as exc:
                    st.error(f"判定を保存できませんでした: {exc}")
    st.divider()
    st.subheader("CSV一括レビュー")
    export_rows = store.rows()
    st.download_button("レビューCSVをダウンロード", store.export_csv(rows=export_rows),
                       file_name="comdesk_hold_review.csv", mime="text/csv", key="comdesk_hold_export")
    upload = st.file_uploader("レビュー済みCSVを選択", type=["csv"], key="comdesk_hold_import")
    if upload is not None:
        preview = store.preview_csv(upload.getvalue())
        st.session_state["_comdesk_hold_csv_preview"] = preview
        if preview["errors"]:
            st.error("CSV検証にエラーがあります。保存はできません。")
            st.dataframe(pd.DataFrame([{"エラー":e} for e in preview["errors"]]), hide_index=True)
        else:
            st.success(f"検証成功：新規反映 {len(preview['accepted']):,}件、既反映のためスキップ {len(preview.get('already_applied', [])):,}件。")
            if st.button("検証済みレビューを隔離台帳へ反映", type="primary", key="comdesk_hold_import_confirm"):
                try:
                    count = store.apply_preview(preview)
                    st.success(f"{count:,}件の判定履歴を保存しました。Clinicマスターは変更していません。")
                    st.session_state.pop("_comdesk_hold_csv_preview", None)
                    st.rerun()
                except Exception as exc:
                    st.error(f"CSVを反映できませんでした: {exc}")
    st.caption(f"隔離レビューDB: {store.path}。このDBはClinicマスターDBとは別です。")
