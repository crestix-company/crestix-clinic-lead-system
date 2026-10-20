"""Explicit local-only fake Draft API transport; it has no network/Gmail code."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class MockResponse:
    payload: Mapping[str, object]
    status_code: int = 200

    def json(self):
        return dict(self.payload)


class LocalMockDraftTransport:
    """In-memory idempotent Draft fixture keyed by operation and clinic/campaign."""

    def __init__(self, records: dict):
        self.records = records

    def __call__(self, url: str, *, json: dict, headers: dict, timeout: float):
        if not url.startswith("mock://"):
            raise RuntimeError("local mock transport accepts mock:// endpoints only")
        if headers.get("Authorization") != "Bearer local-mock-only":
            raise RuntimeError("local mock token mismatch")
        operation_key = str(json.get("operationKey") or "")
        clinic_uuid = str(json.get("clinicUuid") or "")
        campaign_id = str(json.get("campaignId") or "")
        if not operation_key or not clinic_uuid or not campaign_id:
            return MockResponse({"status": "HOLD", "reason": "操作キーまたは医院/キャンペーン識別子がありません"}, 400)
        scope = f"{clinic_uuid}:{campaign_id}"
        by_operation = self.records.setdefault("by_operation", {})
        by_scope = self.records.setdefault("by_scope", {})
        if operation_key in by_operation:
            return MockResponse(dict(by_operation[operation_key]))
        if scope in by_scope:
            return MockResponse({"status": "HOLD", "reason": "同一医院・同一キャンペーンに既存Mock Draftがあります。重複作成を保留しました。",
                                 "operationKey": operation_key})
        index = len(by_operation) + 1
        row = {"status": "DRAFT", "reason": "LOCAL MOCK: Gmailへ接続していません",
               "draftId": f"mock-draft-{index:04d}", "messageId": f"mock-message-{index:04d}",
               "threadId": f"mock-thread-{index:04d}", "correlationId": f"mock-correlation-{index:04d}",
               "operationKey": operation_key}
        by_operation[operation_key] = row
        by_scope[scope] = operation_key
        return MockResponse(row)

