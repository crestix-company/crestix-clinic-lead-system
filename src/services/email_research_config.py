"""Runtime-tunable Email Research settings (no production action by importing)."""
from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class EmailResearchConfig:
    workers: int = 8
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 20.0
    retry_count: int = 1
    search_query_limit: int = 2
    claim_lease_seconds: int = 300
    idle_poll_seconds: float = 2.0
    target_sync_interval_seconds: float = 30.0
    primary_minimum_confidence: str = "HIGH"

    def __post_init__(self):
        if not 1 <= self.workers <= 64:
            raise ValueError("workers must be between 1 and 64")
        if self.search_query_limit != 2:
            raise ValueError("formal Email Research search_query_limit must be 2")
        if (self.read_timeout_seconds <= 0 or not 0 <= self.retry_count <= 5
                or not 30 <= self.claim_lease_seconds <= 3600
                or self.idle_poll_seconds <= 0 or self.target_sync_interval_seconds <= 0):
            raise ValueError("timeout/retry configuration is invalid")


def load_email_research_config(environ=None) -> EmailResearchConfig:
    env = os.environ if environ is None else environ
    return EmailResearchConfig(
        workers=int(env.get("EMAIL_RESEARCH_WORKERS", "8")),
        connect_timeout_seconds=float(env.get("EMAIL_RESEARCH_CONNECT_TIMEOUT_SECONDS", "10")),
        read_timeout_seconds=float(env.get("EMAIL_RESEARCH_READ_TIMEOUT_SECONDS", "20")),
        retry_count=int(env.get("EMAIL_RESEARCH_RETRY_COUNT", "1")),
        search_query_limit=int(env.get("EMAIL_RESEARCH_SEARCH_QUERY_LIMIT", "2")),
        claim_lease_seconds=int(env.get("EMAIL_RESEARCH_CLAIM_LEASE_SECONDS", "300")),
        idle_poll_seconds=float(env.get("EMAIL_RESEARCH_IDLE_POLL_SECONDS", "2")),
        target_sync_interval_seconds=float(env.get("EMAIL_RESEARCH_TARGET_SYNC_INTERVAL_SECONDS", "30")),
        primary_minimum_confidence=env.get("EMAIL_RESEARCH_PRIMARY_MINIMUM_CONFIDENCE", "HIGH").upper(),
    )
