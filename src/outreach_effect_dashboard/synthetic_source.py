"""Synthetic SendRecord generator, shaped like the real 案件管理 sheet.

For demo/testing only while real Google Sheets access is not yet authorized. Never touches
the network or any real data. Clinic/staff names are generic placeholders, never real people.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from .models import SendRecord

STAFF_POOL = ("担当A", "担当B", "担当C", "担当D")
TEMPLATE_POOL = ("1", "2", "3", "5", "6")


def generate_synthetic_records(count: int, *, start: datetime, end: datetime, seed: int = 0) -> list[SendRecord]:
    """Generates ``count`` synthetic send records with sent_at spread across [start, end],
    and probabilistic reply/open/click/appointment events -- some inside a 14/30-day window,
    some deliberately outside it, so metrics.py's window cutoff is exercised by tests.
    """
    rng = random.Random(seed)
    span_days = max((end - start).days, 1)
    records = []
    for i in range(count):
        sent_at = start + timedelta(days=rng.randint(0, span_days), hours=rng.randint(8, 20))
        is_staff = rng.choice(STAFF_POOL)
        template_number = rng.choice(TEMPLATE_POOL)

        def offset(base: datetime, probability: float, max_offset_days: int) -> datetime | None:
            if rng.random() >= probability:
                return None
            return base + timedelta(days=rng.randint(0, max_offset_days), hours=rng.randint(0, 23))

        # 開封推定→高信頼開封→クリックの順で、必ず前段のイベントが起きていることを前提に連鎖させる
        # （高信頼開封が開封推定より先に起きる、といった不整合な合成データにしない）。
        open_estimated_at = offset(sent_at, 0.55, 5)
        open_high_confidence_at = (
            open_estimated_at if open_estimated_at and rng.random() < 0.55 else None
        )
        clicked_at = offset(open_estimated_at, 0.35, 3) if open_estimated_at else None
        replied_at = offset(sent_at, 0.15, 40)
        appointment_at = offset(replied_at, 0.30, 20) if replied_at else None

        records.append(SendRecord(
            case_id=f"{sent_at:%Y%m%d}-{sent_at:%H%M%S}-{i:03d}",
            is_staff=is_staff,
            template_number=template_number,
            sent=True,
            sent_at=sent_at,
            replied=replied_at is not None,
            reply_at=replied_at,
            open_estimated=open_estimated_at is not None,
            open_estimated_at=open_estimated_at,
            open_high_confidence=open_high_confidence_at is not None,
            open_high_confidence_at=open_high_confidence_at,
            clicked=clicked_at is not None,
            clicked_at=clicked_at,
            appointment=appointment_at is not None,
            appointment_at=appointment_at,
        ))
    return records
