"""Sanity-check: compare this dashboard's own aggregation against the existing「集計」sheet's
own numbers, for the same テンプレート breakdown. READ ONLY, no writes to either sheet.

Per docs/outreach_ab_tracking_design.md §6: before trusting the dashboard's full output against
real data, run this once with a small amount of real data and confirm the two sources roughly
agree. The existing 集計 sheet has no evaluation-window concept (all-time counts), so the
dashboard side uses an unbounded window (evaluation_window_days=None) to match it -- this is
NOT the normal 14/30-day dashboard view, only a like-for-like comparison.

Known limitation: the existing 集計 sheet has no 高信頼開封率 column (§2.2 /
docs/outreach_ab_tracking_design.md), so that indicator cannot be reconciled -- it is dashboard
-only. This is documented, not silently skipped.
"""
from __future__ import annotations

from dataclasses import dataclass

from .metrics import breakdown_by_template
from .models import SendRecord

SUMMARY_SHEET_RANGE = "集計!A:I"
_SUMMARY_HEADERS = ("テンプレート", "送信", "開封推定率", "返信率", "クリック率", "アポ率")


@dataclass(frozen=True)
class ReconciliationRow:
    template_number: str
    sheet_sent: int | None
    dashboard_sent: int
    sheet_open_estimated_rate: float | None
    dashboard_open_estimated_rate: float
    sheet_reply_rate: float | None
    dashboard_reply_rate: float
    sheet_click_rate: float | None
    dashboard_click_rate: float
    sheet_appointment_rate: float | None
    dashboard_appointment_rate: float

    @property
    def matches(self) -> bool:
        """Sent counts must match exactly; rates allow small float/rounding tolerance."""
        if self.sheet_sent is None:
            return False
        if self.sheet_sent != self.dashboard_sent:
            return False
        pairs = (
            (self.sheet_open_estimated_rate, self.dashboard_open_estimated_rate),
            (self.sheet_reply_rate, self.dashboard_reply_rate),
            (self.sheet_click_rate, self.dashboard_click_rate),
            (self.sheet_appointment_rate, self.dashboard_appointment_rate),
        )
        return all(sheet is not None and abs(sheet - dash) < 0.01 for sheet, dash in pairs)


def _parse_percent(value: str) -> float | None:
    value = (value or "").strip().rstrip("%")
    if not value:
        return None
    try:
        return float(value) / 100
    except ValueError:
        return None


def parse_summary_sheet_template_breakdown(rows: list[list[str]]) -> dict[str, dict]:
    """Parses 集計 sheet's テンプレート別 pivot by header name (same defensive approach as
    sheets_source.parse_rows_to_records). Returns {} if the expected headers aren't found,
    rather than raising, so an unexpected real layout doesn't crash the reconciliation check.
    """
    if not rows:
        return {}
    header = rows[0]
    index = {name: i for i, name in enumerate(header)}
    if not all(h in index for h in _SUMMARY_HEADERS):
        return {}

    def cell(row: list[str], name: str) -> str:
        i = index[name]
        return row[i] if i < len(row) else ""

    parsed = {}
    for row in rows[1:]:
        template = cell(row, "テンプレート").strip()
        if not template:
            continue
        sent_raw = cell(row, "送信").strip()
        parsed[template] = {
            "sent": int(sent_raw) if sent_raw.isdigit() else None,
            "open_estimated_rate": _parse_percent(cell(row, "開封推定率")),
            "reply_rate": _parse_percent(cell(row, "返信率")),
            "click_rate": _parse_percent(cell(row, "クリック率")),
            "appointment_rate": _parse_percent(cell(row, "アポ率")),
        }
    return parsed


def reconcile(records: list[SendRecord], summary_rows: list[list[str]]) -> list[ReconciliationRow]:
    """Compares this module's unbounded (all-time) テンプレート別 aggregation against the
    existing 集計 sheet's own numbers for the same templates.
    """
    if not records:
        return []
    sent_dates = [r.sent_at.date() for r in records if r.sent_at]
    if not sent_dates:
        return []
    dashboard_breakdown = {
        result.label: result
        for result in breakdown_by_template(
            records, date_from=min(sent_dates), date_to=max(sent_dates), evaluation_window_days=None)
    }
    sheet_breakdown = parse_summary_sheet_template_breakdown(summary_rows)

    templates = sorted(set(dashboard_breakdown) | set(sheet_breakdown), key=lambda x: (len(x), x))
    output = []
    for template in templates:
        dash = dashboard_breakdown.get(template)
        sheet = sheet_breakdown.get(template, {})
        output.append(ReconciliationRow(
            template_number=template,
            sheet_sent=sheet.get("sent"),
            dashboard_sent=dash.sent_count if dash else 0,
            sheet_open_estimated_rate=sheet.get("open_estimated_rate"),
            dashboard_open_estimated_rate=dash.open_estimated_rate if dash else 0.0,
            sheet_reply_rate=sheet.get("reply_rate"),
            dashboard_reply_rate=dash.reply_rate if dash else 0.0,
            sheet_click_rate=sheet.get("click_rate"),
            dashboard_click_rate=dash.clicked_rate if dash else 0.0,
            sheet_appointment_rate=sheet.get("appointment_rate"),
            dashboard_appointment_rate=dash.appointment_rate if dash else 0.0,
        ))
    return output
