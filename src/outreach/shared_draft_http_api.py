"""Authenticated HTTP boundary for the shared Draft creation core.

No Gmail or Supabase connections are created here. Cloud Run composition must
inject the Phase B core, a confirmed-identity resolver, token verifier, limiter,
and audit sink. A missing dependency fails closed.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from threading import Lock
from typing import Callable, Mapping, Optional, Protocol

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.outreach.draft_creation_api import DraftApiRequest, SharedDraftCreationAPI

logger = logging.getLogger("shared_draft_api.audit")


@dataclass(frozen=True)
class Principal:
    caller_id: str
    roles: frozenset[str]
    clinic_ids: frozenset[int] | None = None
    campaign_ids: frozenset[str] | None = None


class TokenVerifier(Protocol):
    def verify(self, token: str) -> Principal | None: ...


class ClinicIdentityResolver(Protocol):
    def confirmed_uuid(self, clinic_id: int) -> str | None: ...


class RateLimiter(Protocol):
    def allow(self, key: str, *, limit: int, window_seconds: int) -> bool: ...


class MemoryRateLimiter:
    """Local test/dev limiter only. Cloud Run must inject a shared limiter."""
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.hits: dict[str, list[float]] = {}
        self.lock = Lock()

    def allow(self, key: str, *, limit: int, window_seconds: int) -> bool:
        now = self.clock()
        with self.lock:
            hits = [t for t in self.hits.get(key, []) if now - t < window_seconds]
            if len(hits) >= limit:
                self.hits[key] = hits
                return False
            hits.append(now)
            self.hits[key] = hits
            return True


class DraftRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    operationKey: str = Field(min_length=8, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")
    clinicUuid: str = Field(min_length=36, max_length=36)
    clinicId: int = Field(gt=0)
    campaignId: str = Field(min_length=1, max_length=160)
    gasCaseId: str = Field(default="", max_length=200)
    recipient: str = Field(min_length=3, max_length=320)
    subject: str = Field(min_length=1, max_length=998)
    body: str = Field(min_length=1, max_length=100_000)
    clinicName: str = Field(default="", max_length=300)
    clinicAddress: str = Field(default="", max_length=500)

    @field_validator("clinicUuid")
    @classmethod
    def valid_uuid(cls, value: str) -> str:
        return str(uuid.UUID(value))

    @field_validator("recipient")
    @classmethod
    def valid_email_shape(cls, value: str) -> str:
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("recipient must be an email address")
        return value

    @field_validator("campaignId", "gasCaseId", "clinicName", "clinicAddress", "subject")
    @classmethod
    def reject_header_controls(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("control characters are not allowed in header fields")
        return value


def _status_code(status: str) -> int:
    if status in {"DRAFT_CREATED", "DRAFT_RECOVERED"}:
        return 200
    if status == "IN_PROGRESS":
        return 202
    if status in {"REJECTED", "IDENTITY_CONFLICT"}:
        return 409
    if status == "HOLD" or status.endswith("_EXISTS"):
        return 409
    return 503


def create_app(*, core: SharedDraftCreationAPI, token_verifier: TokenVerifier,
               identity_resolver: ClinicIdentityResolver, rate_limiter: RateLimiter,
               audit: Callable[[Mapping[str, object]], None], rate_limit: int = 30,
               rate_window_seconds: int = 60) -> FastAPI:
    app = FastAPI(title="Shared Draft API", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def correlation_id(request: Request, call_next):
        supplied = request.headers.get("X-Correlation-ID", "")
        cid = supplied if re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", supplied) else str(uuid.uuid4())
        request.state.correlation_id = cid
        if request.headers.get("content-length", "0").isdigit() and int(request.headers.get("content-length", "0")) > 120_000:
            return JSONResponse({"detail": "request body too large", "correlationId": cid}, status_code=413,
                                headers={"X-Correlation-ID": cid})
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = cid
        return response

    @app.get("/healthz")
    async def healthz():
        # Liveness only. It exposes no dependency or secret state.
        return {"status": "ok"}

    @app.post("/v1/drafts")
    async def create_draft(payload: DraftRequestBody, request: Request,
                           authorization: Optional[str] = Header(default=None),
                           shared_draft_token: Optional[str] = Header(default=None, alias="X-Shared-Draft-Token")):
        cid = request.state.correlation_id
        def record_denial(event: str, caller_id: str = "") -> None:
            try:
                audit({"event": event, "correlation_id": cid, "caller_id": caller_id})
            except Exception:
                logger.exception("audit sink failed correlation_id=%s", cid)
        if shared_draft_token:
            token = shared_draft_token.strip()
        elif authorization and authorization.startswith("Bearer "):
            # Local and non-IAM test callers use the API bearer credential.
            token = authorization[7:].strip()
        else:
            record_denial("authentication_rejected")
            raise HTTPException(status_code=401, detail="authentication required")
        try:
            principal = token_verifier.verify(token) if token else None
        except Exception:
            record_denial("authentication_dependency_unavailable")
            raise HTTPException(status_code=503, detail="authentication unavailable") from None
        if principal is None:
            record_denial("authentication_rejected")
            raise HTTPException(status_code=401, detail="authentication required")
        if not ({"draft:create", "admin"} & principal.roles):
            record_denial("authorization_rejected", principal.caller_id)
            raise HTTPException(status_code=403, detail="permission denied")
        if ((principal.clinic_ids is not None and payload.clinicId not in principal.clinic_ids)
                or (principal.campaign_ids is not None and payload.campaignId not in principal.campaign_ids)):
            record_denial("scope_rejected", principal.caller_id)
            raise HTTPException(status_code=403, detail="caller scope does not allow this clinic/campaign")
        try:
            allowed = rate_limiter.allow(principal.caller_id, limit=rate_limit,
                                         window_seconds=rate_window_seconds)
        except Exception:
            record_denial("rate_limit_dependency_unavailable", principal.caller_id)
            raise HTTPException(status_code=503, detail="request protection unavailable") from None
        if not allowed:
            record_denial("rate_limit_rejected", principal.caller_id)
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        try:
            confirmed = identity_resolver.confirmed_uuid(payload.clinicId)
        except Exception:
            record_denial("identity_dependency_unavailable", principal.caller_id)
            raise HTTPException(status_code=503, detail="identity verification unavailable") from None
        if not confirmed or not hmac.compare_digest(confirmed.casefold(), payload.clinicUuid.casefold()):
            audit({"event": "identity_rejected", "correlation_id": cid,
                   "caller_id": principal.caller_id, "operation_key_hash": hashlib.sha256(
                       payload.operationKey.encode()).hexdigest()})
            raise HTTPException(status_code=409, detail="confirmed Clinic UUID mismatch")
        draft_request = DraftApiRequest(
            operation_key=payload.operationKey, clinic_uuid=payload.clinicUuid,
            clinic_id=payload.clinicId, campaign_id=payload.campaignId,
            recipient=payload.recipient, subject=payload.subject, body=payload.body,
            identity_verified=True, clinic_name=payload.clinicName,
            clinic_address=payload.clinicAddress, gas_case_id=payload.gasCaseId,
        )
        try:
            result = core.create_or_recover(draft_request)
        except Exception:
            record_denial("draft_core_unexpected_failure", principal.caller_id)
            return JSONResponse({"status": "UNKNOWN", "correlationId": cid,
                                 "operationKey": payload.operationKey,
                                 "reason": "Draft outcome uncertain; reconcile before retry"}, status_code=503)
        status = result.status if result.status in {
            "DRAFT_CREATED", "DRAFT_RECOVERED", "IN_PROGRESS", "HOLD", "REJECTED",
            "IDENTITY_CONFLICT", "SCHEDULED_EXISTS", "SENT_EXISTS", "TRASHED_EXISTS",
            "REPLIED_EXISTS",
        } else "HOLD"
        event = {"event": "draft_request_completed", "correlation_id": cid,
                 "caller_id": principal.caller_id, "operation_key_hash": hashlib.sha256(
                     payload.operationKey.encode()).hexdigest(), "clinic_id": payload.clinicId,
                 "status": status}
        try:
            audit(event)
        except Exception:
            logger.exception("audit sink failed correlation_id=%s", cid)
            # Auditing failure cannot turn an uncertain Gmail outcome into success.
            status = "HOLD"
        body = {"status": status, "correlationId": cid,
                "operationKey": payload.operationKey, "clinicUuid": payload.clinicUuid,
                "gasCaseId": payload.gasCaseId, "draftId": result.draft_id,
                "messageId": result.message_id, "threadId": result.thread_id,
                "reason": result.reason if status == result.status else "audit unavailable; reconcile before retry"}
        return JSONResponse(body, status_code=_status_code(status))

    return app


def constant_time_token_verifier(tokens: Mapping[str, tuple[str, frozenset[str]]]) -> TokenVerifier:
    """Build from Secret Manager injected values: caller_id -> (secret, roles)."""
    class _Verifier:
        def verify(self, token: str) -> Principal | None:
            match = None
            for caller_id, (secret, roles) in tokens.items():
                if hmac.compare_digest(token, secret):
                    match = Principal(caller_id, roles)
            return match
    return _Verifier()

