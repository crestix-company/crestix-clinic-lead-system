"""一括生成: 対象医院 × 記事原本（READ ONLY） → 件名・本文・判定ステータス。

No Gmail, DB, network or file writes. CSV is returned as bytes for download only.
"""
from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from .article_recommender import Article, Recommendation, recommend
from .compose import (
    ARTICLE_LISTED, ARTICLE_NOT_LISTED, ARTICLE_STATUS_LABELS, ARTICLE_UNCONFIRMED,
    TEMPLATE_IDS, TEMPLATE_LISTED, TEMPLATE_RECOMMEND, TEMPLATE_VERSIONS, Recipient, format_schedule,
    render_listed, render_recommend, subject_for,
)
from .email_template import DraftValidationError
from .targets import EMAIL_FOUND, ClinicTarget

READY, NEEDS_REVIEW, FAILED = "READY", "NEEDS_REVIEW", "FAILED"
STATUS_LABELS = {READY: "READY", NEEDS_REVIEW: "要確認", FAILED: "失敗"}


@dataclass(frozen=True)
class GenerationSettings:
    staff: str
    dates: tuple[date, ...]
    start_time: str = "9:00"
    end_time: str = "21:00"
    called_uuids: frozenset[str] = frozenset()  # 本日電話・メール案内を確認済みの医院のみ
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
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def first(self) -> Recommendation | None:
        return self.recommendations[0] if self.recommendations else None


def _region_matched(rec: Recommendation) -> bool:
    return any(r in ("市区町村一致", "都道府県一致") for r in rec.reasons)


def generate_one(target: ClinicTarget, articles: list[Article], settings: GenerationSettings,
                 *, master_urls: frozenset[str] | None = None, schedule: str | None = None) -> OutreachResult:
    urls = master_urls if master_urls is not None else frozenset(a.url for a in articles)
    called = target.clinic_uuid.casefold() in {u.casefold() for u in settings.called_uuids}
    reasons: list[str] = []
    if not target.clinic_uuid:
        return OutreachResult(target, FAILED, reasons=("Clinic UUIDなし",))
    if not target.clinic_name:
        return OutreachResult(target, FAILED, reasons=("医院名なし",))
    try:
        schedule = schedule or format_schedule(settings.dates, settings.start_time, settings.end_time)
        subject = subject_for(settings.staff)
    except DraftValidationError as exc:
        return OutreachResult(target, FAILED, reasons=(str(exc),))

    recs = tuple(recommend(articles, prefecture=target.prefecture, municipality=target.municipality,
                           departments=target.departments, treatments=target.treatments, limit=3)) \
        if (target.departments or target.treatments) else ()
    # 記事原本にないURLは使わない（recommendは原本由来だが念のため再検証）
    recs = tuple(r for r in recs if r.article.url in urls)

    if target.email_status != EMAIL_FOUND:
        reasons.append("宛先メール未取得")
    if not target.director_name:
        reasons.append("院長名未確認（院長先生宛てで生成）")
    recipient = Recipient(target.clinic_name, "院長", target.director_name)

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
            return OutreachResult(target, NEEDS_REVIEW if reasons else READY, TEMPLATE_LISTED, subject, body,
                                  recs, example, listed_url, called, tuple(reasons))
        if not recs:
            return OutreachResult(target, FAILED, recommendations=recs, called=called,
                                  reasons=tuple(reasons + ["地域・標榜科目・治療に一致する推奨記事なし"]))
        first = recs[0]
        if status_kind == ARTICLE_UNCONFIRMED:
            reasons.insert(0, "記事掲載状況が未確認: " + (target.article.reason or "根拠なし"))
        if not _region_matched(first):
            reasons.append("第一推奨記事が地域不一致")
        body = render_recommend(recipient, staff=settings.staff, article_title=first.article.title,
                                article_url=first.article.url, schedule=schedule, called=called)
        return OutreachResult(target, NEEDS_REVIEW if reasons else READY, TEMPLATE_RECOMMEND, subject, body,
                              recs, first.article.url, "", called, tuple(reasons))
    except DraftValidationError as exc:
        return OutreachResult(target, FAILED, recommendations=recs, called=called,
                              reasons=tuple(reasons + [str(exc)]))


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
            output.append(OutreachResult(target, FAILED, reasons=("Clinic UUID重複",)))
        else:
            output.append(generate_one(target, articles, settings, master_urls=urls))
        if on_progress is not None:
            on_progress(index, total)
    return output


def summarize(results: list[OutreachResult]) -> dict[str, int]:
    return {"total": len(results), **{s: sum(r.status == s for r in results) for s in (READY, NEEDS_REVIEW, FAILED)}}


CSV_COLUMNS = [
    "clinic_id", "clinic_uuid", "clinic_name", "prefecture", "municipality", "departments", "hp_rank",
    "to_email", "recipient", "is_staff", "phone_called", "article_status", "template",
    "listed_article_url", "example_article_url", "recommended_title", "recommended_url",
    "recommend_reason", "candidate_2_url", "candidate_3_url", "subject", "body", "status", "review_reasons",
    "template_id", "template_version", "generation_batch_id",
]


def to_csv_bytes(results: list[OutreachResult], staff: str, generation_batch_id: str = "") -> bytes:
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
            "clinic_id": t.clinic_id, "clinic_uuid": t.clinic_uuid, "clinic_name": t.clinic_name,
            "prefecture": t.prefecture, "municipality": t.municipality, "departments": "、".join(t.departments),
            "hp_rank": t.hp_rank, "to_email": t.email if t.email_status == EMAIL_FOUND else "",
            "recipient": f"院長 {t.director_name} 様" if t.director_name else "院長先生",
            "is_staff": staff, "phone_called": "TRUE" if r.called else "FALSE",
            "article_status": ARTICLE_STATUS_LABELS.get(t.article.status, t.article.status),
            "template": r.template, "listed_article_url": r.listed_url, "example_article_url": r.example_url,
            "recommended_title": recs[0].article.title if recs else "",
            "recommended_url": recs[0].article.url if recs else "",
            "recommend_reason": " / ".join(recs[0].reasons) if recs else "",
            "candidate_2_url": recs[1].article.url if len(recs) > 1 else "",
            "candidate_3_url": recs[2].article.url if len(recs) > 2 else "",
            "subject": r.subject, "body": r.body, "status": r.status, "review_reasons": " / ".join(r.reasons),
            "template_id": TEMPLATE_IDS.get(r.template, ""), "template_version": TEMPLATE_VERSIONS.get(r.template, ""),
            "generation_batch_id": generation_batch_id,
        })
    return buffer.getvalue().encode("utf-8-sig")


__all__ = [
    "READY", "NEEDS_REVIEW", "FAILED", "STATUS_LABELS", "GenerationSettings", "OutreachResult",
    "generate_one", "generate_batch", "summarize", "to_csv_bytes", "CSV_COLUMNS",
    "ARTICLE_NOT_LISTED",
]
