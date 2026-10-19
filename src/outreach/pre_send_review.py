"""Explicit, auditable pre-send checklist for Gmail draft creation.

The internal meaning of "I3" is intentionally not inferred here. A draft can be
created only after an approved I3 reference is configured and the reviewer records
the operator-specified checklist below.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit


I3_DEFINITION_ID_ENV = "OUTREACH_I3_DEFINITION_ID"
I3_DEFINITION_URL_ENV = "OUTREACH_I3_DEFINITION_URL"
REQUIRED_CHECKS = (
    "clinic_identity",
    "template_body",
    "article_reference",
    "clinic_detail_page",
    "duplicate_clear",
)


@dataclass(frozen=True)
class I3Reference:
    definition_id: str
    definition_url: str

    @property
    def configured(self) -> bool:
        part = urlsplit(self.definition_url)
        return bool(self.definition_id and part.scheme == "https" and part.netloc)


def configured_i3_reference(environ=None) -> I3Reference:
    env = os.environ if environ is None else environ
    return I3Reference(
        (env.get(I3_DEFINITION_ID_ENV) or "").strip(),
        (env.get(I3_DEFINITION_URL_ENV) or "").strip(),
    )


def build_pre_send_review(*, result, reviewer: str, checks: dict[str, bool],
                          i3_reference: I3Reference, i3_acknowledged: bool,
                          operation_key: str = "",
                          campaign_id: str = "",
                          reviewed_at: datetime | None = None) -> tuple[dict, tuple[str, ...]]:
    """Return a JSON-safe review record and blockers; this function has no I/O."""
    target = result.target
    blockers = []
    if result.status != "READY":
        blockers.append("生成判定がREADYではありません")
    if not target.clinic_name.strip() or not target.director_name.strip() or not target.director_name_confirmed:
        blockers.append("医院名・確認済み院長名が揃っていません")
    if not target.email.strip():
        blockers.append("宛先がありません")
    if not result.subject.strip() or not result.body.strip():
        blockers.append("正式テンプレートの件名・本文がありません")
    article_url = result.listed_url or result.example_url or (
        result.first.article.url if result.first else ""
    )
    if not article_url:
        blockers.append("実掲載記事または類似記事URLがありません")
    if not target.article.clinic_profile_url.strip():
        blockers.append("医院詳細ページURLがありません")
    if not reviewer.strip():
        blockers.append("確認担当者が未入力です")
    if not i3_reference.configured:
        blockers.append("I3の正式定義・承認済み参照先が未設定です")
    if i3_reference.configured and not i3_acknowledged:
        blockers.append("I3の正式定義・参照先を確認していません")
    for key in REQUIRED_CHECKS:
        if checks.get(key) is not True:
            blockers.append(f"送信前確認が未完了: {key}")

    stamp = reviewed_at or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        blockers.append("確認日時にtimezoneがありません")
    payload = {
        "reviewed_by": reviewer.strip(),
        "reviewed_at": stamp.astimezone(timezone.utc).isoformat(),
        "operation_key": operation_key.strip() or f"clinic:{target.clinic_id}:campaign:{campaign_id.strip()}",
        "i3_definition_status": "CONFIRMED" if i3_reference.configured and i3_acknowledged else "UNCONFIRMED",
        "i3_definition_id": i3_reference.definition_id,
        "i3_definition_url": i3_reference.definition_url,
        "checks": {key: checks.get(key) is True for key in REQUIRED_CHECKS},
        "protocol": "operator_required_fields_v1",
    }
    return payload, tuple(blockers)


__all__ = ["I3Reference", "REQUIRED_CHECKS", "build_pre_send_review", "configured_i3_reference"]
