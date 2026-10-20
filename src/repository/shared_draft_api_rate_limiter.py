"""Shared rate limiter backed by the proposed Supabase Postgres function."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class SupabaseSharedDraftRateLimiter:
    def __init__(self, connection: Any, *, clock=None):
        self.connection = connection
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def allow(self, key: str, *, limit: int, window_seconds: int) -> bool:
        if not key or not 1 <= int(limit) <= 1000 or not 1 <= int(window_seconds) <= 3600:
            return False
        now = int(self.clock().timestamp())
        start = datetime.fromtimestamp(now - now % int(window_seconds), timezone.utc)
        try:
            with self.connection.transaction():
                with self.connection.cursor() as cursor:
                    cursor.execute("SELECT outreach.consume_shared_draft_api_rate(%s,%s,%s)",
                                   (key, start, int(limit)))
                    row = cursor.fetchone()
            return bool(row and row[0])
        except Exception:
            # Signal outage separately from an actual exhausted window.
            raise RuntimeError("shared rate limiter unavailable") from None

