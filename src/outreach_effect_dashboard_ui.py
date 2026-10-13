"""効果測定ダッシュボード（READ ONLY、既存メール送付文章の4タブとは独立したページ）。

既存Google Sheets「第一営業部_メール日程調整管理」(案件管理シート)をREAD ONLYデータソースとする。
実データ連携には OUTREACH_SHEETS_READONLY_CREDENTIALS / OUTREACH_SHEETS_ID が必要（未設定時は
合成データにフォールバックし、その旨を明示する）。Google Sheetsへの書き込み・Gmail操作は一切行わない。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

from src.outreach_effect_dashboard.metrics import (
    EVALUATION_WINDOW_OPTIONS, aggregate, breakdown_by_is_staff, breakdown_by_template,
)
from src.outreach_effect_dashboard.reconciliation import SUMMARY_SHEET_RANGE, reconcile
from src.outreach_effect_dashboard.sheets_source import SheetsAccessError, fetch_raw_rows, parse_rows_to_records
from src.outreach_effect_dashboard.synthetic_source import generate_synthetic_records

K = "effectdash_"


def _load_records():
    """実データ接続が未設定・失敗なら合成データにフォールバックする。実データへは書き込み一切なし。"""
    try:
        rows = fetch_raw_rows()
        return parse_rows_to_records(rows), "real"
    except SheetsAccessError as exc:
        st.warning(f"{exc}")
        end = datetime.combine(date.today(), datetime.min.time())
        start = end - timedelta(days=90)
        return generate_synthetic_records(400, start=start, end=end, seed=42), "synthetic"


def _load_summary_rows_for_reconciliation():
    """集計シートの読み取りに失敗しても、本体データの表示は止めない（整合性確認パネルを省略するだけ）。"""
    try:
        return fetch_raw_rows(range_name=SUMMARY_SHEET_RANGE)
    except SheetsAccessError:
        return None


def _metric_row(result):
    return {
        "対象": result.label, "送信数": result.sent_count,
        "返信数": result.replied_count, "返信率": f"{result.reply_rate:.1%}",
        "開封推定数": result.open_estimated_count, "開封推定率": f"{result.open_estimated_rate:.1%}",
        "高信頼開封数": result.open_high_confidence_count, "高信頼開封率": f"{result.open_high_confidence_rate:.1%}",
        "クリック数": result.clicked_count, "クリック率": f"{result.clicked_rate:.1%}",
        "アポ獲得数": result.appointment_count, "アポ率": f"{result.appointment_rate:.1%}",
    }


def effect_dashboard_page():
    st.header("効果測定ダッシュボード（READ ONLY）")
    st.info(
        "既存Google Sheets（第一営業部_メール日程調整管理）をREAD ONLYで参照する専用ページです。"
        "Google Sheetsへの書き込み・Gmail操作は一切行いません。"
    )

    records, source_kind = _load_records()
    if source_kind == "synthetic":
        st.warning("🧪 **合成データを表示しています**（実データ連携の認証情報が未設定、または接続に失敗したため）。"
                   "これは実在の送信実績ではありません。", icon="🧪")
    else:
        st.success("🔒 **既存Google Sheetsの実データを表示しています**（READ ONLY。書き込みは行いません）。", icon="🔒")

    if source_kind == "real":
        st.markdown("#### 整合性確認（実データ接続後、最初に確認してください）")
        st.caption(
            "既存「集計」シート自身の数値（全期間・テンプレート別）と、このダッシュボードの集計ロジックを"
            "比較します。送信数が一致し、各率の差が1pt未満であれば一致とみなします。"
            "既存シートに「高信頼開封率」列は無いため、その指標だけは比較できません（ダッシュボード独自の指標）。"
        )
        summary_rows = _load_summary_rows_for_reconciliation()
        if summary_rows is None:
            st.caption("集計シートを読み取れなかったため、整合性確認は省略しました。")
        else:
            recon = reconcile(records, summary_rows)
            if not recon:
                st.caption("比較対象のテンプレートが見つかりませんでした。")
            else:
                st.dataframe(pd.DataFrame([{
                    "テンプレート": r.template_number, "一致": "✅" if r.matches else "⚠️",
                    "送信数(既存)": r.sheet_sent, "送信数(本機能)": r.dashboard_sent,
                    "開封推定率(既存)": r.sheet_open_estimated_rate, "開封推定率(本機能)": f"{r.dashboard_open_estimated_rate:.1%}",
                    "返信率(既存)": r.sheet_reply_rate, "返信率(本機能)": f"{r.dashboard_reply_rate:.1%}",
                    "クリック率(既存)": r.sheet_click_rate, "クリック率(本機能)": f"{r.dashboard_click_rate:.1%}",
                    "アポ率(既存)": r.sheet_appointment_rate, "アポ率(本機能)": f"{r.dashboard_appointment_rate:.1%}",
                } for r in recon]), hide_index=True, use_container_width=True)
                if not all(r.matches for r in recon):
                    st.error("既存シートと一致しないテンプレートがあります。下記の指標表示は参考値として扱い、"
                             "列マッピング（docs/outreach_ab_tracking_design.md §2.2）を見直してください。")

    sent_dates = [r.sent_at.date() for r in records if r.sent_at]
    default_from = min(sent_dates) if sent_dates else date.today() - timedelta(days=90)
    default_to = max(sent_dates) if sent_dates else date.today()

    c1, c2, c3 = st.columns(3)
    date_from = c1.date_input("送信期間（開始）", value=default_from, key=K + "from")
    date_to = c2.date_input("送信期間（終了）", value=default_to, key=K + "to")
    window = c3.selectbox("評価期間（返信・開封・クリック・アポの集計対象期間）", EVALUATION_WINDOW_OPTIONS,
                          format_func=lambda d: f"{d}日", key=K + "window")

    if date_from > date_to:
        st.error("送信期間の開始は終了以前にしてください。")
        return

    overall = aggregate(records, date_from=date_from, date_to=date_to, evaluation_window_days=window)
    st.markdown("#### 全体")
    st.dataframe(pd.DataFrame([_metric_row(overall)]), hide_index=True, use_container_width=True)

    st.markdown("#### IS担当者別")
    by_staff = breakdown_by_is_staff(records, date_from=date_from, date_to=date_to, evaluation_window_days=window)
    st.dataframe(pd.DataFrame([_metric_row(r) for r in by_staff]), hide_index=True, use_container_width=True)

    st.markdown("#### テンプレート番号別")
    st.caption("既存の「テンプレート」番号をそのまま使用しています（将来のA/Bテスト用variant_idとは別軸）。")
    by_template = breakdown_by_template(records, date_from=date_from, date_to=date_to, evaluation_window_days=window)
    st.dataframe(pd.DataFrame([_metric_row(r) for r in by_template]), hide_index=True, use_container_width=True)


if __name__ == "__main__":
    st.set_page_config(page_title="効果測定ダッシュボード", page_icon="📊", layout="wide")
    effect_dashboard_page()
