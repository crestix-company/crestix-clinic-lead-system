from types import SimpleNamespace

import pytest

from src.enrichment.safe_web import WebError
from src.master.email_auto_run import MeteredTransport


class SequenceTransport:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    def get(self, *args, **kwargs):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_email_transport_retries_timeout_dns_and_5xx_with_bounded_budget():
    sleeps = []
    timeout_transport = MeteredTransport(
        SequenceTransport([WebError("接続・SSL・タイムアウトのエラーです。"), SimpleNamespace(status=200)]),
        max_retries=1, sleeper=sleeps.append,
    )
    assert timeout_transport.get("url", 1, 1).status == 200
    assert timeout_transport.metrics["web_fetch_timeout"] == 1
    assert timeout_transport.metrics["web_fetch_retry_count"] == 1

    dns_transport = MeteredTransport(
        SequenceTransport([WebError("URLまたはDNSを確認できません。"), SimpleNamespace(status=200)]),
        max_retries=1, sleeper=lambda _seconds: None,
    )
    assert dns_transport.get("url", 1, 1).status == 200
    assert dns_transport.metrics["web_fetch_dns_error"] == 1

    server_transport = MeteredTransport(
        SequenceTransport([SimpleNamespace(status=503), SimpleNamespace(status=200)]),
        max_retries=1, sleeper=lambda _seconds: None,
    )
    assert server_transport.get("url", 1, 1).status == 200
    assert server_transport.metrics["web_fetch_http_5xx"] == 1


def test_email_transport_never_retries_ssl_certificate_failure():
    transport = MeteredTransport(
        SequenceTransport([WebError("SSL証明書の検証に失敗しました。"), SimpleNamespace(status=200)]),
        max_retries=1, sleeper=lambda _seconds: None,
    )
    with pytest.raises(WebError, match="SSL証明書"):
        transport.get("url", 1, 1)
    assert transport.metrics["web_fetch_ssl_error"] == 1
    assert transport.metrics["web_fetch_retry_count"] == 0
