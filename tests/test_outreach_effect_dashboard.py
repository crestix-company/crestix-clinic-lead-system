"""効果測定ダッシュボードの集計ロジック・READ ONLYゲート・合成データ生成のテスト。

実際のGoogle Sheetsには一切接続しない（ネットワークI/Oなし）。
"""
from datetime import datetime, timedelta

import pytest

from src.outreach_effect_dashboard.metrics import (
    EVALUATION_WINDOW_OPTIONS, aggregate, breakdown_by_is_staff, breakdown_by_template,
)
from src.outreach_effect_dashboard.models import SendRecord
from src.outreach_effect_dashboard.reconciliation import parse_summary_sheet_template_breakdown, reconcile
from src.outreach_effect_dashboard.sheets_source import (
    SheetsAccessError, _require_config, fetch_raw_rows, parse_rows_to_records,
)
from src.outreach_effect_dashboard.synthetic_source import generate_synthetic_records

SENT = datetime(2026, 9, 1, 10, 0, 0)


def record(**kw) -> SendRecord:
    base = dict(case_id="20260901-100000-001", is_staff="担当A", template_number="1",
               sent=True, sent_at=SENT, replied=False, reply_at=None,
               open_estimated=False, open_estimated_at=None,
               open_high_confidence=False, open_high_confidence_at=None,
               clicked=False, clicked_at=None, appointment=False, appointment_at=None)
    base.update(kw)
    return SendRecord(**base)


# --- 集計の基本 ---
def test_denominator_is_sent_count_in_period():
    records = [record(), record(sent_at=SENT + timedelta(days=1)),
              record(sent=False, sent_at=None)]  # 未送信は分母に含めない
    result = aggregate(records, date_from=SENT.date(), date_to=SENT.date() + timedelta(days=1),
                       evaluation_window_days=14)
    assert result.sent_count == 2


def test_date_range_filters_population():
    records = [record(), record(sent_at=SENT + timedelta(days=30))]
    result = aggregate(records, date_from=SENT.date(), date_to=SENT.date(), evaluation_window_days=14)
    assert result.sent_count == 1


# --- 評価期間（14日/30日）の境界 ---
def test_event_within_window_is_counted():
    r = record(replied=True, reply_at=SENT + timedelta(days=14))  # ちょうど14日 = 含む
    result = aggregate([r], date_from=SENT.date(), date_to=SENT.date(), evaluation_window_days=14)
    assert result.replied_count == 1
    assert result.reply_rate == 1.0


def test_event_outside_window_is_excluded():
    r = record(replied=True, reply_at=SENT + timedelta(days=15))  # 14日を超える
    result = aggregate([r], date_from=SENT.date(), date_to=SENT.date(), evaluation_window_days=14)
    assert result.replied_count == 0
    assert result.reply_rate == 0.0


def test_30_day_window_counts_event_excluded_by_14_day_window():
    r = record(appointment=True, appointment_at=SENT + timedelta(days=25))
    at_14 = aggregate([r], date_from=SENT.date(), date_to=SENT.date(), evaluation_window_days=14)
    at_30 = aggregate([r], date_from=SENT.date(), date_to=SENT.date(), evaluation_window_days=30)
    assert at_14.appointment_count == 0
    assert at_30.appointment_count == 1


def test_invalid_window_rejected():
    with pytest.raises(ValueError):
        aggregate([record()], date_from=SENT.date(), date_to=SENT.date(), evaluation_window_days=7)


def test_event_flag_true_without_timestamp_is_not_counted():
    # フラグはTrueだが日時が無い（不整合データ）行は、評価期間判定ができないため計上しない。
    r = record(replied=True, reply_at=None)
    result = aggregate([r], date_from=SENT.date(), date_to=SENT.date(), evaluation_window_days=30)
    assert result.replied_count == 0


# --- 開封推定 vs 高信頼開封は独立に数える ---
def test_open_estimated_and_high_confidence_are_independent_counts():
    r = record(open_estimated=True, open_estimated_at=SENT + timedelta(hours=1),
              open_high_confidence=False, open_high_confidence_at=None)
    result = aggregate([r], date_from=SENT.date(), date_to=SENT.date(), evaluation_window_days=14)
    assert result.open_estimated_count == 1
    assert result.open_high_confidence_count == 0


# --- IS担当者別・テンプレート別 ---
def test_breakdown_by_is_staff():
    records = [record(is_staff="担当A"), record(is_staff="担当A"), record(is_staff="担当B")]
    rows = breakdown_by_is_staff(records, date_from=SENT.date(), date_to=SENT.date(), evaluation_window_days=14)
    counts = {r.label: r.sent_count for r in rows}
    assert counts == {"担当A": 2, "担当B": 1}


def test_breakdown_by_template_keeps_existing_numbers_untouched():
    records = [record(template_number="1"), record(template_number="6"), record(template_number="6")]
    rows = breakdown_by_template(records, date_from=SENT.date(), date_to=SENT.date(), evaluation_window_days=14)
    counts = {r.label: r.sent_count for r in rows}
    assert counts == {"1": 1, "6": 2}
    assert all(label in ("1", "6") for label in counts)  # variant_id等の新概念を混ぜていない


# --- READ ONLY Gate (fail closed) ---
def test_sheets_source_fails_closed_without_config():
    with pytest.raises(SheetsAccessError, match="OUTREACH_SHEETS_READONLY_CREDENTIALS"):
        _require_config({})


def test_sheets_source_fails_closed_with_only_one_var_set():
    with pytest.raises(SheetsAccessError):
        _require_config({"OUTREACH_SHEETS_READONLY_CREDENTIALS": "/tmp/x.json"})
    with pytest.raises(SheetsAccessError):
        _require_config({"OUTREACH_SHEETS_ID": "abc"})


def test_sheets_source_passes_with_both_vars_set():
    path, sheet_id = _require_config({
        "OUTREACH_SHEETS_READONLY_CREDENTIALS": "/tmp/x.json", "OUTREACH_SHEETS_ID": "abc"})
    assert path == "/tmp/x.json" and sheet_id == "abc"


# --- Row parsing (header-name based) ---
def test_parse_rows_to_records_maps_by_header_name():
    rows = [
        ["案件ID", "IS担当", "テンプレート", "送信有無", "送信日時", "返信有無", "初回返信日時",
         "開封推定レベル", "初回画像読込日時", "高信頼開封", "初回高信頼シグナル日時",
         "記事クリック有無", "初回クリック日時", "メール経由アポ", "アポ日時"],
        ["20260901-100000-001", "担当A", "1", "送信済み", "2026/09/01 10:00:00", "あり",
         "2026/09/03 09:00:00", "可能性あり", "2026/09/01 12:00:00", "あり", "2026/09/01 12:05:00",
         "あり", "2026/09/01 12:10:00", "あり", "2026/09/05 15:00:00"],
    ]
    records = parse_rows_to_records(rows)
    assert len(records) == 1
    r = records[0]
    assert r.case_id == "20260901-100000-001" and r.is_staff == "担当A" and r.template_number == "1"
    assert r.sent is True and r.sent_at == datetime(2026, 9, 1, 10, 0, 0)
    assert r.replied is True and r.reply_at == datetime(2026, 9, 3, 9, 0, 0)
    assert r.open_high_confidence is True
    assert r.appointment_at == datetime(2026, 9, 5, 15, 0, 0)


def test_parse_rows_to_records_handles_empty_and_missing_columns():
    rows = [["案件ID", "送信有無"], ["x", ""]]
    records = parse_rows_to_records(rows)
    assert len(records) == 1
    assert records[0].sent is False
    assert records[0].is_staff == ""  # 未知/欠落ヘッダーでも例外にならない


def test_parse_rows_to_records_empty_input():
    assert parse_rows_to_records([]) == []


# --- 合成データ ---
def test_synthetic_records_are_reproducible_and_shaped_correctly():
    start = datetime(2026, 1, 1)
    end = datetime(2026, 3, 1)
    a = generate_synthetic_records(50, start=start, end=end, seed=1)
    b = generate_synthetic_records(50, start=start, end=end, seed=1)
    assert a == b  # 同じseedなら再現可能
    assert len(a) == 50
    assert all(r.sent and r.sent_at is not None for r in a)
    assert all(start <= r.sent_at <= end + timedelta(hours=23) for r in a)
    # クリック・高信頼開封は開封推定が無ければ発生しない（整合性のある合成データ）
    assert all((r.clicked_at is None) or r.open_estimated for r in a)
    assert all((r.open_high_confidence_at is None) or r.open_estimated for r in a)


def test_synthetic_records_feed_metrics_without_error():
    records = generate_synthetic_records(300, start=datetime(2026, 1, 1), end=datetime(2026, 4, 1), seed=7)
    sent_dates = [r.sent_at.date() for r in records]
    for window in EVALUATION_WINDOW_OPTIONS:
        result = aggregate(records, date_from=min(sent_dates) - timedelta(days=1),
                           date_to=max(sent_dates) + timedelta(days=1),
                           evaluation_window_days=window)
        assert result.sent_count == 300
        assert 0.0 <= result.reply_rate <= 1.0
        assert 0.0 <= result.open_high_confidence_rate <= result.open_estimated_rate + 1e-9


# --- fail-closed / 接続失敗時の安全な挙動（監査対応） ---
def test_fetch_raw_rows_without_config_raises_before_any_network_or_import():
    with pytest.raises(SheetsAccessError, match="OUTREACH_SHEETS_READONLY_CREDENTIALS"):
        fetch_raw_rows({})


def test_fetch_raw_rows_wraps_any_failure_as_sheets_access_error_not_raw_exception():
    # google-api-python-client / google-auth は本テスト環境に意図的に未インストール
    # （実データ許可が無い限りダッシュボードが依存しないことの裏付け）。
    # 設定だけ揃っていて実際の接続・依存関係が無い状態でも、ここで素の例外が漏れずSheetsAccessErrorに
    # 包まれることを確認する＝「権限不足・接続失敗時の安全な挙動」の監査項目に対応。
    env = {"OUTREACH_SHEETS_READONLY_CREDENTIALS": "/nonexistent/credentials.json",
          "OUTREACH_SHEETS_ID": "dummy-sheet-id"}
    with pytest.raises(SheetsAccessError):
        fetch_raw_rows(env)


def test_sheets_access_error_message_never_contains_the_credentials_path():
    # 認証情報漏えい防止: エラーメッセージに設定値（パスの中身）そのものを含めない。
    secret_looking_path = "/Users/someone/.secret/my-service-account-key.json"
    env = {"OUTREACH_SHEETS_READONLY_CREDENTIALS": secret_looking_path, "OUTREACH_SHEETS_ID": "dummy"}
    try:
        fetch_raw_rows(env)
        pytest.fail("expected SheetsAccessError")
    except SheetsAccessError as exc:
        assert secret_looking_path not in str(exc)


# --- 整合性確認（既存「集計」シートとの比較） ---
def summary_rows_for(template: str, *, sent: int, open_rate: str, reply_rate: str,
                     click_rate: str, appt_rate: str):
    return [
        ["KPI", "数値", "", "テンプレート", "送信", "開封推定率", "返信率", "クリック率", "アポ率"],
        ["フォーム受付数", "1", "", template, str(sent), open_rate, reply_rate, click_rate, appt_rate],
    ]


def test_parse_summary_sheet_template_breakdown():
    rows = summary_rows_for("1", sent=10, open_rate="50.0%", reply_rate="10.0%",
                            click_rate="20.0%", appt_rate="5.0%")
    parsed = parse_summary_sheet_template_breakdown(rows)
    assert parsed["1"] == {"sent": 10, "open_estimated_rate": 0.5, "reply_rate": 0.1,
                           "click_rate": 0.2, "appointment_rate": 0.05}


def test_parse_summary_sheet_returns_empty_dict_on_unexpected_layout():
    assert parse_summary_sheet_template_breakdown([["違う", "ヘッダー"]]) == {}
    assert parse_summary_sheet_template_breakdown([]) == {}


def test_reconcile_detects_exact_match():
    # 1件送信・返信あり・開封推定あり・クリックなし・アポなし のレコードに対応する集計シートの数値。
    records = [record(replied=True, reply_at=SENT + timedelta(hours=1),
                      open_estimated=True, open_estimated_at=SENT + timedelta(hours=1))]
    summary = summary_rows_for("1", sent=1, open_rate="100.0%", reply_rate="100.0%",
                               click_rate="0.0%", appt_rate="0.0%")
    rows = reconcile(records, summary)
    assert len(rows) == 1 and rows[0].matches is True


def test_reconcile_detects_mismatch():
    records = [record(replied=True, reply_at=SENT + timedelta(hours=1))]
    summary = summary_rows_for("1", sent=1, open_rate="100.0%", reply_rate="0.0%",  # 既存シートと食い違う
                               click_rate="0.0%", appt_rate="0.0%")
    rows = reconcile(records, summary)
    assert len(rows) == 1 and rows[0].matches is False


def test_reconcile_unbounded_window_ignores_dashboard_default_cutoff():
    # 通常のダッシュボード(14/30日)では範囲外になる返信も、整合性確認(無制限)では計上される。
    records = [record(replied=True, reply_at=SENT + timedelta(days=90))]
    summary = summary_rows_for("1", sent=1, open_rate="0.0%", reply_rate="100.0%",
                               click_rate="0.0%", appt_rate="0.0%")
    rows = reconcile(records, summary)
    assert rows[0].matches is True


def test_reconcile_empty_records_returns_empty():
    assert reconcile([], summary_rows_for("1", sent=1, open_rate="0%", reply_rate="0%",
                                          click_rate="0%", appt_rate="0%")) == []
