"""In-memory mock of the (not-yet-deployed) GAS Web App, for tests only.

This is NOT a reimplementation of `registerClinicLeadCase_`'s business logic
for production use -- the real decision logic lives in
~/ClinicLeadGASIntegration/bridge/clinic_lead_gas_bridge.js (22 tests passing
there already). This mock exists solely so this test suite can exercise
`GasBridgeClient` end-to-end (including the real HMAC auth signing code)
without a network call or a deployed Web App, per
`~/ClinicLeadGASIntegration/bridge/docs/connection_auth_design.md`.
"""
from __future__ import annotations

import json
import hashlib
import hmac
import time
from dataclasses import dataclass, field

from src.outreach.gas_bridge_client import build_auth_fields


@dataclass
class MockGasBridgeServer:
    shared_secret: str
    cases_by_operation_key: dict = field(default_factory=dict)
    known_draft_ids: set = field(default_factory=set)
    pending_operation_keys: set = field(default_factory=set)
    fail_next_n_sheet_writes: int = 0
    next_case_id_counter: int = 0
    always_timeout: bool = False
    always_return_http_status: int | None = None
    call_count: int = 0
    accepted_nonces: set[str] = field(default_factory=set)
    now: float | None = None

    def handle_wire(self, wire_body: str) -> tuple[int, str]:
        self.call_count += 1

        if self.always_timeout:
            raise TimeoutError("mock GAS server did not respond in time")
        if self.always_return_http_status is not None:
            return self.always_return_http_status, ""
        try:
            envelope = json.loads(wire_body)
            auth = envelope["auth"]
            payload_json = envelope["payload"]
            payload = json.loads(payload_json)
            timestamp = auth["timestamp"]
            nonce = auth["nonce"]
            signature = auth["signature"]
        except (TypeError, ValueError, KeyError):
            return 400, json.dumps({"status": "REJECTED"})

        if not isinstance(payload_json, str) or not isinstance(payload, dict):
            return 400, json.dumps({"status": "REJECTED"})
        # Require payload's exact canonical representation, including Unicode.
        if json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) != payload_json:
            return 400, json.dumps({"status": "REJECTED"})
        clock = self.now if self.now is not None else time.time()
        if not isinstance(timestamp, str) or not timestamp.isdigit() or len(timestamp) != 10:
            return 401, json.dumps({"status": "REJECTED"})
        if abs(int(clock) - int(timestamp)) > 300:
            return 401, json.dumps({"status": "REJECTED"})
        if not isinstance(nonce, str) or len(nonce) < 24 or not all(c.isalnum() or c in "_-" for c in nonce):
            return 401, json.dumps({"status": "REJECTED"})
        if nonce in self.accepted_nonces:
            return 401, json.dumps({"status": "REJECTED"})
        signed = f"{timestamp}\n{nonce}\n{payload_json}".encode("utf-8")
        expected = hmac.new(self.shared_secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
        if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
            return 401, json.dumps({"status": "REJECTED"})
        # A valid nonce is consumed before business side effects, just as GAS
        # must do under ScriptLock/PropertiesService.
        self.accepted_nonces.add(nonce)

        operation_key = payload.get("operationKey", "")

        existing = self.cases_by_operation_key.get(operation_key)
        if existing:
            already = dict(existing)
            already["status"] = "ALREADY_REGISTERED"
            return 200, json.dumps(already)

        draft_id = (payload.get("existingDraft") or {}).get("id", "")
        if draft_id not in self.known_draft_ids:
            return 200, json.dumps({"status": "REJECTED", "reason": "draft could not be verified"})

        if self.fail_next_n_sheet_writes > 0:
            self.fail_next_n_sheet_writes -= 1
            self.pending_operation_keys.add(operation_key)
            return 200, json.dumps({"status": "PENDING", "reason": "sheet write failed; retry later", "draftId": draft_id})

        self.next_case_id_counter += 1
        case_id = f"20261018-090000-{self.next_case_id_counter:03d}"
        tracking_token = f"{self.next_case_id_counter:032d}"
        status = "RECOVERED" if operation_key in self.pending_operation_keys else "REGISTERED"
        result = {
            "status": status,
            "caseId": case_id,
            "draftId": draft_id,
            "threadId": f"thread-{draft_id}",
            "trackingToken": tracking_token,
        }
        self.cases_by_operation_key[operation_key] = result
        return 200, json.dumps(result)


def make_mock_transport(server: MockGasBridgeServer, *, shared_secret: str):
    """Builds a `GasBridgeTransport`-shaped callable backed by `server`.

    Uses the real `build_auth_fields()` signing code (not a shortcut), so
    tests exercise the actual client-side auth implementation.
    """

    def transport(payload: dict, *, timeout_seconds: float) -> tuple[int, str]:
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        auth = build_auth_fields(payload_json, shared_secret)
        wire_body = json.dumps(
            {"auth": auth, "payload": payload_json}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return server.handle_wire(wire_body)

    return transport
