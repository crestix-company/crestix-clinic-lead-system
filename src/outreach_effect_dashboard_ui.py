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
from src.outreach_effect_dashboard.sheets_source import SheetsAccessError, fetch_raw_rows, parse_rows_to_records
from src.outreach_effect_dashboard.synthetic_source import generate_synthetic_records

K = "effectdash_"


def _load_records():
    """実データ接続が未設定なら合成データにフォールバックする。実データへは書き込み一切なし。"""
    try:
        rows = fetch_raw_rows()
        return parse_rows_to_records(rows), "real"
    except SheetsAccessError as exc:
        st.warning(f"{exc}")
        end = datetime.combine(date.today(), datetime.min.time())
        start = end - timedelta(days=90)
        return generate_synthetic_records(400, start=start, end=end, seed=42), "synthetic"


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
        st.caption("現在は合成データで表示しています（実データ連携の認証情報が未設定のため）。")
    else:
        st.caption("既存Google Sheetsから読み取った実データを表示しています（READ ONLY）。")

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
