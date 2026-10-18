"""Real HTTP transport for the GAS Clinic Lead bridge (stdlib `urllib` only).

Not wired to any production Web App -- no GAS Web App has been deployed yet
(see ~/ClinicLeadGASIntegration/bridge/docs/connection_auth_design.md: the
deployment itself is still pending operator approval). This module exists so
`GasBridgeClient` has a concrete, ready-to-use production implementation once
a URL and shared secret are provisioned. It is never imported by the test
suite's mock-connection tests (those use a pure in-memory transport).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .gas_bridge_client import GasBridgeTransport, GasBridgeTransportError, build_auth_fields


@dataclass(frozen=True)
class GasBridgeHttpConfig:
    """`shared_secret` must come from a secret store / env var, never a literal
    in code or a committed file (see connection_auth_design.md)."""

    endpoint_url: str
    shared_secret: str


def make_http_transport(config: GasBridgeHttpConfig) -> GasBridgeTransport:
    """Returns a transport callable usable by `GasBridgeClient`.

    Auth fields are sent as query parameters (not headers), because the GAS
    `doPost` side can only read `e.parameter` -- see connection_auth_design.md's
    own caveat about this being provisional. The POST body remains the exact,
    unmodified contract JSON (`python_integration_contract.md`).
    """
    if not config.endpoint_url:
        raise ValueError("endpoint_url is required")
    if not config.shared_secret:
        raise ValueError("shared_secret is required")
    parsed_endpoint = urllib.parse.urlsplit(config.endpoint_url)
    if parsed_endpoint.scheme.lower() != "https":
        raise ValueError("endpoint_url must use HTTPS")
    if not parsed_endpoint.netloc or parsed_endpoint.username or parsed_endpoint.password:
        raise ValueError("endpoint_url must not contain user information")
    if parsed_endpoint.query or parsed_endpoint.fragment:
        raise ValueError("endpoint_url must not contain a query or fragment")
    if config.shared_secret in config.endpoint_url:
        raise ValueError("endpoint_url must not contain the shared secret")

    def transport(payload: dict, *, timeout_seconds: float) -> tuple[int, str]:
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        auth = build_auth_fields(payload_json, config.shared_secret)
        envelope = {"auth": auth, "payload": payload_json}
        body = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

        request = urllib.request.Request(
            config.endpoint_url,
            data=body.encode("utf-8"),
            headers={"Content-Type": "application/json; charset=UTF-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - internal bridge only
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            # urllib errors can embed request URLs; keep transport diagnostics
            # free of endpoint, payload, and credential material.
            raise GasBridgeTransportError(f"HTTP transport failed ({type(error).__name__})") from error

    return transport
