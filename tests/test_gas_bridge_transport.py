"""Tests the HTTP transport's request construction only. No real network
call is made -- `urllib.request.urlopen` is monkeypatched."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from io import BytesIO
from unittest.mock import patch

import pytest

from src.outreach.gas_bridge_client import GasBridgeTransportError
from src.outreach.gas_bridge_transport import GasBridgeHttpConfig, make_http_transport


class _FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_transport_sends_auth_as_query_params_and_body_as_untouched_json():
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = request.data
        captured["headers"] = dict(request.header_items())
        captured["timeout"] = timeout
        return _FakeResponse(200, json.dumps({"status": "REGISTERED"}).encode("utf-8"))

    config = GasBridgeHttpConfig(endpoint_url="https://script.google.com/macros/s/fake/exec", shared_secret="s3cret")
    transport = make_http_transport(config)

    with patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
        status, body = transport({"operationKey": "a" * 64}, timeout_seconds=5.0)

    assert status == 200
    assert json.loads(body) == {"status": "REGISTERED"}
    assert "authSecret=s3cret" in captured["url"]
    assert "authTimestamp=" in captured["url"]
    assert "authSignature=" in captured["url"]
    # The body itself must be the untouched contract JSON -- no auth fields mixed in.
    assert json.loads(captured["body"]) == {"operationKey": "a" * 64}
    assert captured["timeout"] == 5.0


def test_transport_surfaces_http_error_status_and_body():
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "forbidden", {}, BytesIO(b'{"status":"REJECTED"}'))

    config = GasBridgeHttpConfig(endpoint_url="https://example.invalid/exec", shared_secret="s3cret")
    transport = make_http_transport(config)

    with patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
        status, body = transport({"operationKey": "a" * 64}, timeout_seconds=5.0)

    assert status == 403
    assert "REJECTED" in body


def test_transport_wraps_network_errors_as_transport_error():
    def fake_urlopen(request, timeout):
        raise TimeoutError("no response")

    config = GasBridgeHttpConfig(endpoint_url="https://example.invalid/exec", shared_secret="s3cret")
    transport = make_http_transport(config)

    with patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
        with pytest.raises(GasBridgeTransportError):
            transport({"operationKey": "a" * 64}, timeout_seconds=5.0)


def test_config_requires_endpoint_url_and_secret():
    with pytest.raises(ValueError):
        make_http_transport(GasBridgeHttpConfig(endpoint_url="", shared_secret="s"))
    with pytest.raises(ValueError):
        make_http_transport(GasBridgeHttpConfig(endpoint_url="https://example.invalid", shared_secret=""))
