"""Clinic Lead UI boundary for the shared HTTP API; it never sends mail."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping


@dataclass(frozen=True)
class SharedDraftUiContext:
    clinic_id: int
    clinic_uuid: str
    campaign_id: str
    operation_key: str
    recipient: str
    clinic_name: str
    clinic_address: str
    subject: str
    body: str
    article_url: str = ""
    gas_case_id: str = ""
    director_name: str = ""
    article_status: str = ""


def context_from_outreach_result(result, *, campaign_id: str, operation_key: str,
                                 gas_case_id: str = "") -> SharedDraftUiContext:
    target = result.target
    article_url = result.listed_url or result.example_url or (
        result.first.article.url if result.first else ""
    )
    return SharedDraftUiContext(
        clinic_id=int(target.clinic_id), clinic_uuid=str(target.clinic_uuid),
        campaign_id=str(campaign_id), operation_key=str(operation_key),
        recipient=str(target.email), clinic_name=str(target.clinic_name),
        clinic_address=str(target.clinic_address), subject=str(result.subject), body=str(result.body),
        article_url=str(article_url or ""), gas_case_id=str(gas_case_id or ""),
        director_name=str(target.director_name or ""),
        article_status=str(getattr(target.article, "status", "")),
    )


class SharedDraftApiClient:
    def __init__(self, *, endpoint: str, token_provider: Callable[[], str],
                 transport: Callable[..., object], timeout_seconds: float = 20,
                 identity_token_provider: Callable[[], str] | None = None):
        self.endpoint = endpoint.rstrip("/") + "/v1/drafts"
        self.token_provider = token_provider
        self.transport = transport
        self.timeout_seconds = max(1, min(float(timeout_seconds), 60))
        self.identity_token_provider = identity_token_provider

    def create_or_recover(self, context: SharedDraftUiContext) -> Mapping[str, object]:
        payload = {
            "operationKey": context.operation_key, "clinicUuid": context.clinic_uuid,
            "clinicId": context.clinic_id, "campaignId": context.campaign_id,
            "gasCaseId": context.gas_case_id, "recipient": context.recipient,
            "subject": context.subject, "body": context.body,
            "clinicName": context.clinic_name, "clinicAddress": context.clinic_address,
        }
        try:
            headers = {"Authorization": "Bearer " + self.token_provider()}
            if self.identity_token_provider:
                headers = {"Authorization": "Bearer " + self.identity_token_provider(),
                           "X-Shared-Draft-Token": self.token_provider()}
            response = self.transport(self.endpoint, json=payload,
                                      headers=headers,
                                      timeout=self.timeout_seconds)
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("invalid API response")
            if getattr(response, "status_code", 500) >= 500:
                return {**data, "status": "UNKNOWN", "reason": "API outcome is uncertain; reconcile before retry"}
            return data
        except Exception as exc:
            # In particular, a client-side timeout does not imply Gmail failed.
            return {"status": "UNKNOWN", "reason": f"API outcome uncertain ({type(exc).__name__}); reconcile before retry",
                    "operationKey": context.operation_key}


def display_fields(context: SharedDraftUiContext, result: Mapping[str, object]) -> dict[str, str]:
    return {
        "医院": context.clinic_name,
        "院長名": context.director_name,
        "Clinic UUID": context.clinic_uuid,
        "宛先": context.recipient,
        "記事判定": context.article_status,
        "メール本文": context.body,
        "記事URL": context.article_url,
        "状態": str(result.get("status", "UNKNOWN")),
        "Gmail状態": str(result.get("status", "UNKNOWN")),
        "保存結果": str(result.get("status", "UNKNOWN")),
        "HOLD理由": str(result.get("reason", "")),
        "Draft ID": str(result.get("draftId", "")),
        "Message ID": str(result.get("messageId", "")),
        "Thread ID": str(result.get("threadId", "")),
        "相関ID": str(result.get("correlationId", "")),
    }

