"""一括生成: 対象医院 × 記事原本（READ ONLY） → 件名・本文・判定ステータス。

No Gmail, DB, network or file writes. CSV is returned as bytes for download only.
"""
from __future__ import annotations

import csv
import io
import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Callable

from .article_recommender import Article, Recommendation, recommend
from .compose import (
    ARTICLE_LISTED, ARTICLE_NOT_LISTED, ARTICLE_STATUS_LABELS, ARTICLE_UNCONFIRMED,
    TEMPLATE_IDS, TEMPLATE_LISTED, TEMPLATE_RECOMMEND, TEMPLATE_VERSIONS, Recipient, format_schedule,
    render_listed, render_recommend, subject_for,
)
from .email_template import DraftValidationError
from .gmail_oauth import expected_gmail_account
from .targets import DIRECTOR_OWNER_UNVERIFIED, EMAIL_FOUND, ClinicTarget
from src.utils.date_utils import today_japan

READY, NEEDS_REVIEW, FAILED = "READY", "NEEDS_REVIEW", "FAILED"
STATUS_LABELS = {READY: "READY", NEEDS_REVIEW: "要確認", FAILED: "失敗"}
REQUIRED_STAFF = "髙原"
EXPECTED_GMAIL_ACCOUNT = expected_gmail_account()
SCHEDULE_UNSET = "（候補日時未設定）"
_UNRESOLVED_VARIABLE = re.compile(r"\{[^{}\r\n]+\}")


@dataclass(frozen=True)
class GenerationSettings:
    staff: str
    dates: tuple[date, ...]
    start_time: str = "9:00"
    end_time: str = "21:00"
    called_uuids: frozenset[str] = frozenset()  # 本日電話・メール案内を確認済みの医院のみ
    calendar_confirmed: bool = False  # Google Calendarまたは本人確認済みの候補日時だけREADYにできる
    provisional_template_approved: bool = False  # 関連記事型はGmail保存直前の最終承認が必要
    approved_general_addressee_uuids: frozenset[str] = frozenset()
    authenticated_gmail_account: str = ""  # Phase 3 OAuth確認結果。推測や自動フォールバックは禁止。
    duplicate_check_complete: bool = False  # Gmail既存下書きとの照合完了フラグ
    as_of: date = field(default_factory=today_japan)
    # 将来のA/Bテスト・送信トラッキング集計を見据えた識別子。今回はA/B選択ロジックや送信記録は実装しない。
    # 同一の一括生成（ボタン1クリック）内の全件で共通のIDになる。
    generation_batch_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True)
class OutreachResult:
    target: ClinicTarget
    status: str
    template: str = ""
    subject: str = ""
    body: str = ""
    recommendations: tuple[Recommendation, ...] = ()
    example_url: str = ""
    listed_url: str = ""
    called: bool = False
    candidate_schedule: str = ""
    reasons: tuple[str, ...] = field(default_factory=tuple)
    gmail_ready: bool = False
    gmail_blockers: tuple[str, ...] = field(default_factory=tuple)
    general_addressee_approved: bool = False

    @property
    def first(self) -> Recommendation | None:
        return self.recommendations[0] if self.recommendations else None


def _region_matched(rec: Recommendation) -> bool:
    return any(r in ("市区町村一致", "都道府県一致") for r in rec.reasons)


def _finish(result: OutreachResult, settings: GenerationSettings) -> OutreachResult:
    """Apply final content and Gmail preflight guards without performing Gmail operations."""
    reasons = list(result.reasons)
    if result.status == READY and _UNRESOLVED_VARIABLE.search(result.subject + "\n" + result.body):
        reasons.append("件名または本文に未置換変数あり")
        result = replace(result, status=NEEDS_REVIEW, reasons=tuple(reasons))

    blockers: list[str] = []
    if result.status != READY:
        blockers.append("本文生成がREADYではない")
    if settings.authenticated_gmail_account.strip().casefold() != expected_gmail_account().casefold():
        blockers.append("指定GmailアカウントのOAuth一致確認が未完了")
    if not settings.duplicate_check_complete:
        blockers.append("Gmail既存下書きとの重複検査が未完了")
    if result.template == TEMPLATE_RECOMMEND and not settings.provisional_template_approved:
        blockers.append("関連記事紹介型テンプレートの保存前最終確認が未完了")
    return replace(result, gmail_ready=not blockers, gmail_blockers=tuple(blockers))


def generate_one(target: ClinicTarget, articles: list[Article], settings: GenerationSettings,
                 *, master_urls: frozenset[str] | None = None, schedule: str | None = None) -> OutreachResult:
    urls = master_urls if master_urls is not None else frozenset(a.url for a in articles)
    called = target.clinic_uuid.casefold() in {u.casefold() for u in settings.called_uuids}
    reasons: list[str] = []
    if not target.clinic_uuid:
        return _finish(OutreachResult(target, FAILED, reasons=("Clinic UUIDなし",)), settings)
    if not target.clinic_name:
        return _finish(OutreachResult(target, FAILED, reasons=("医院名なし",)), settings)
    try:
        if schedule is None:
            if settings.dates:
                schedule = format_schedule(
                    settings.dates, settings.start_time, settings.end_time, not_before=settings.as_of,
                )
            else:
                schedule = SCHEDULE_UNSET
        subject = subject_for(settings.staff, called=called)
    except DraftValidationError as exc:
        return _finish(OutreachResult(target, FAILED, reasons=(str(exc),)), settings)

    recs = tuple(recommend(articles, prefecture=target.prefecture, municipality=target.municipality,
                           departments=target.departments, treatments=target.treatments, limit=3)) \
        if (target.departments or target.treatments) else ()
    # 記事原本にないURLは使わない（recommendは原本由来だが念のため再検証）
    recs = tuple(r for r in recs if r.article.url in urls)

    if target.email_status != EMAIL_FOUND:
        reasons.append("宛先メール未取得")
    general_addressee = target.clinic_uuid.casefold() in {
        value.casefold() for value in settings.approved_general_addressee_uuids
    }
    if (not target.director_name_confirmed or not target.director_name) and not general_addressee:
        if target.director_name_source == DIRECTOR_OWNER_UNVERIFIED:
            reasons.append("開設者名はあるが院長本人との同一性が未確認")
        else:
            reasons.append("院長名未確認（院長先生宛てで生成）")
    if not settings.dates:
        reasons.append("候補日時が未設定")
    elif not settings.calendar_confirmed:
        reasons.append("候補日時がGoogleカレンダーまたは本人予定と未照合")
    recipient = Recipient(
        target.clinic_name,
        "院長" if target.director_name or not general_addressee else "ご担当者",
        target.director_name,
    )

    status_kind = target.article.status
    listed_url = target.article.listed_url
    if status_kind == ARTICLE_LISTED and listed_url not in urls:
        reasons.append("掲載記事URLが記事原本に存在しないため未確認扱い")
        status_kind, listed_url = ARTICLE_UNCONFIRMED, ""

    try:
        if status_kind == ARTICLE_LISTED:
            example = target.article.example_url if target.article.example_url in urls else ""
            if not example:
                example = recs[0].article.url if recs else listed_url
            body = render_listed(recipient, staff=settings.staff, example_url=example,
                                 listed_url=listed_url, schedule=schedule, called=called)
            result = OutreachResult(
                target, NEEDS_REVIEW if reasons else READY, TEMPLATE_LISTED, subject, body,
                recs, example, listed_url, called, schedule, tuple(reasons),
                general_addressee_approved=general_addressee,
            )
            return _finish(result, settings)
        if not recs:
            return _finish(OutreachResult(
                target, NEEDS_REVIEW, recommendations=recs, called=called,
                candidate_schedule=schedule,
                reasons=tuple(reasons + ["地域・標榜科目・治療に一致する推奨記事なし"]),
                general_addressee_approved=general_addressee,
            ), settings)
        first = recs[0]
        if not _region_matched(first):
            reasons.append("第一推奨記事が地域不一致")
        body = render_recommend(recipient, staff=settings.staff, article_title=first.article.title,
                                article_url=first.article.url, schedule=schedule, called=called)
        result = OutreachResult(
            target, NEEDS_REVIEW if reasons else READY, TEMPLATE_RECOMMEND, subject, body,
            recs, first.article.url, "", called, schedule, tuple(reasons),
            general_addressee_approved=general_addressee,
        )
        return _finish(result, settings)
    except DraftValidationError as exc:
        return _finish(OutreachResult(
            target, FAILED, recommendations=recs, called=called, candidate_schedule=schedule,
            reasons=tuple(reasons + [str(exc)]),
        ), settings)


def generate_batch(targets: list[ClinicTarget], articles: list[Article], settings: GenerationSettings,
                   *, on_progress: Callable[[int, int], None] | None = None) -> list[OutreachResult]:
    """``on_progress(done, total)`` is called after each target, for UI progress bars. Optional."""
    urls = frozenset(a.url for a in articles)
    counts: dict[str, int] = {}
    for t in targets:
        counts[t.clinic_uuid.casefold()] = counts.get(t.clinic_uuid.casefold(), 0) + 1
    total = len(targets)
    output = []
    for index, target in enumerate(targets, start=1):
        if target.duplicate_uuid or (target.clinic_uuid and counts[target.clinic_uuid.casefold()] > 1):
            output.append(_finish(OutreachResult(target, FAILED, reasons=("Clinic UUID重複",)), settings))
        else:
            output.append(generate_one(target, articles, settings, master_urls=urls))
        if on_progress is not None:
            on_progress(index, total)
    return output


def summarize(results: list[OutreachResult]) -> dict[str, int]:
    return {"total": len(results), **{s: sum(r.status == s for r in results) for s in (READY, NEEDS_REVIEW, FAILED)}}


CSV_COLUMNS = [
    "clinic_id", "clinic_uuid", "outreach_id", "campaign_id", "generation_batch_id", "comdesk_uuid",
    "gmail_draft_id", "gmail_message_id", "sent_at", "owner", "source", "outreach_status",
    "clinic_name", "clinic_address", "prefecture", "municipality", "departments", "hp_rank",
    "to_email", "email_candidate_id", "recipient", "director_name_source", "director_name_confirmed",
    "general_addressee_approved", "is_staff", "phone_called", "phone_history_status", "calendar_confirmed",
    "article_status", "article_evidence", "article_checked_at", "article_processing_version",
    "article_type", "clinic_profile_url", "template", "listed_article_url", "example_article_url", "recommended_title", "recommended_url",
    "recommend_reason", "candidate_2_url", "candidate_3_url", "subject", "body", "candidate_schedule",
    "status", "blocker_reasons", "review_reasons", "content_ready", "gmail_ready", "gmail_blockers",
    "template_id", "template_version",
]


def to_csv_bytes(results: list[OutreachResult], staff: str, generation_batch_id: str = "",
                 campaign_id: str = "", *, calendar_confirmed: bool = False) -> bytes:
    """``generation_batch_id`` is optional metadata only (see GenerationSettings) -- no A/B
    selection or send-tracking is implemented; this only lets a future analysis group rows
    that came from the same 一括生成 run.
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for r in results:
        t, recs = r.target, r.recommendations
        writer.writerow({
            "clinic_id": t.clinic_id, "clinic_uuid": t.clinic_uuid,
            "outreach_id": uuid.uuid5(uuid.NAMESPACE_URL,
                                       f"clinic-outreach:{t.clinic_id}:{campaign_id}").hex if campaign_id else "",
            "campaign_id": campaign_id, "generation_batch_id": generation_batch_id,
            "comdesk_uuid": t.comdesk_uuid, "clinic_name": t.clinic_name,
            "gmail_draft_id": "", "gmail_message_id": "", "sent_at": "", "owner": staff,
            "source": "generation_csv", "outreach_status": "GENERATED" if r.status == READY else "HOLD",
            "clinic_address": t.clinic_address, "prefecture": t.prefecture, "municipality": t.municipality,
            "departments": "、".join(t.departments),
            "hp_rank": t.hp_rank, "to_email": t.email if t.email_status == EMAIL_FOUND else "",
            "email_candidate_id": t.email_candidate_id,
            "recipient": f"院長 {t.director_name} 様" if t.director_name else "院長先生",
            "director_name_source": t.director_name_source,
            "director_name_confirmed": "TRUE" if t.director_name_confirmed and t.director_name else "FALSE",
            "general_addressee_approved": "TRUE" if r.general_addressee_approved else "FALSE",
            "is_staff": staff, "phone_called": "TRUE" if r.called else "FALSE",
            "phone_history_status": "VERIFIED_TODAY" if r.called else "UNVERIFIED",
            "calendar_confirmed": "TRUE" if calendar_confirmed else "FALSE",
            "article_status": ARTICLE_STATUS_LABELS.get(t.article.status, t.article.status),
            "article_evidence": t.article.evidence or t.article.reason,
            "article_checked_at": t.article.checked_at,
            "article_processing_version": t.article.processing_version,
            "article_type": t.article.article_type,
            "clinic_profile_url": t.article.clinic_profile_url,
            "template": r.template, "listed_article_url": r.listed_url, "example_article_url": r.example_url,
            "recommended_title": recs[0].article.title if recs else "",
            "recommended_url": recs[0].article.url if recs else "",
            "recommend_reason": " / ".join(recs[0].reasons) if recs else "",
            "candidate_2_url": recs[1].article.url if len(recs) > 1 else "",
            "candidate_3_url": recs[2].article.url if len(recs) > 2 else "",
            "subject": r.subject, "body": r.body, "candidate_schedule": r.candidate_schedule,
            "status": r.status, "blocker_reasons": " / ".join(r.reasons),
            "review_reasons": " / ".join(r.reasons),
            "content_ready": "TRUE" if r.status == READY else "FALSE",
            "gmail_ready": "TRUE" if r.gmail_ready else "FALSE",
            "gmail_blockers": " / ".join(r.gmail_blockers),
            "template_id": TEMPLATE_IDS.get(r.template, ""), "template_version": TEMPLATE_VERSIONS.get(r.template, ""),
        })
    return buffer.getvalue().encode("utf-8-sig")


__all__ = [
    "READY", "NEEDS_REVIEW", "FAILED", "STATUS_LABELS", "GenerationSettings", "OutreachResult",
    "generate_one", "generate_batch", "summarize", "to_csv_bytes", "CSV_COLUMNS",
    "ARTICLE_NOT_LISTED",
]
