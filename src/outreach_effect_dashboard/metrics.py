"""Pure aggregation logic: period x evaluation-window metrics over SendRecord rows.

No I/O here -- this module only computes. Population (denominator) = count of records with
sent=True and sent_at within [date_from, date_to] (plus optional IS担当/テンプレート filters).
An event (reply/open/click/appointment) counts only if its timestamp exists and falls within
evaluation_window_days of that record's sent_at (inclusive), per
docs/outreach_ab_tracking_design.md §6.2.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .models import SendRecord

EVALUATION_WINDOW_OPTIONS = (14, 30)


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _within_window(sent_at: datetime | None, event_at: datetime | None, window_days: int | None) -> bool:
    if sent_at is None or event_at is None:
        return False
    if window_days is None:  # 無制限（整合性確認専用。ダッシュボードのUI上は選べない）
        return event_at >= sent_at
    delta_days = (event_at - sent_at).days
    return 0 <= delta_days <= window_days


@dataclass(frozen=True)
class MetricsResult:
    label: str  # 全体 / IS担当者名 / テンプレート番号など、このグループを表す表示名
    sent_count: int
    replied_count: int
    reply_rate: float
    open_estimated_count: int
    open_estimated_rate: float
    open_high_confidence_count: int
    open_high_confidence_rate: float
    clicked_count: int
    clicked_rate: float
    appointment_count: int
    appointment_rate: float


def _in_population(record: SendRecord, *, date_from: date, date_to: date,
                   is_staff: str | None, template_number: str | None) -> bool:
    if not record.sent or record.sent_at is None:
        return False
    if not (date_from <= record.sent_at.date() <= date_to):
        return False
    if is_staff is not None and record.is_staff != is_staff:
        return False
    if template_number is not None and record.template_number != template_number:
        return False
    return True


def aggregate(records: list[SendRecord], *, date_from: date, date_to: date,
             evaluation_window_days: int | None, is_staff: str | None = None,
             template_number: str | None = None, label: str = "全体") -> MetricsResult:
    """``evaluation_window_days=None`` means unbounded (no cutoff). The dashboard UI never
    offers this; it exists only for reconciliation.py, to match the existing 集計 sheet's
    own all-time semantics when sanity-checking this module's math against it.
    """
    if evaluation_window_days is not None and evaluation_window_days not in EVALUATION_WINDOW_OPTIONS:
        raise ValueError(f"evaluation_window_days は {EVALUATION_WINDOW_OPTIONS} または None のいずれかにしてください")
    population = [r for r in records
                 if _in_population(r, date_from=date_from, date_to=date_to,
                                   is_staff=is_staff, template_number=template_number)]
    sent_count = len(population)

    def count_event(predicate_flag: str, at_field: str) -> int:
        return sum(
            1 for r in population
            if getattr(r, predicate_flag) and _within_window(r.sent_at, getattr(r, at_field), evaluation_window_days)
        )

    replied = count_event("replied", "reply_at")
    open_est = count_event("open_estimated", "open_estimated_at")
    open_hc = count_event("open_high_confidence", "open_high_confidence_at")
    clicked = count_event("clicked", "clicked_at")
    appt = count_event("appointment", "appointment_at")

    return MetricsResult(
        label=label, sent_count=sent_count,
        replied_count=replied, reply_rate=_rate(replied, sent_count),
        open_estimated_count=open_est, open_estimated_rate=_rate(open_est, sent_count),
        open_high_confidence_count=open_hc, open_high_confidence_rate=_rate(open_hc, sent_count),
        clicked_count=clicked, clicked_rate=_rate(clicked, sent_count),
        appointment_count=appt, appointment_rate=_rate(appt, sent_count),
    )


def breakdown_by_is_staff(records: list[SendRecord], *, date_from: date, date_to: date,
                          evaluation_window_days: int) -> list[MetricsResult]:
    staff_names = sorted({r.is_staff for r in records if r.is_staff})
    return [
        aggregate(records, date_from=date_from, date_to=date_to,
                 evaluation_window_days=evaluation_window_days, is_staff=name, label=name)
        for name in staff_names
    ]


def breakdown_by_template(records: list[SendRecord], *, date_from: date, date_to: date,
                          evaluation_window_days: int | None) -> list[MetricsResult]:
    templates = sorted({r.template_number for r in records if r.template_number}, key=lambda x: (len(x), x))
    return [
        aggregate(records, date_from=date_from, date_to=date_to,
                 evaluation_window_days=evaluation_window_days, template_number=t, label=t)
        for t in templates
    ]
