"""Safe, bounded, one-click Gmail draft batch coordination.

No external API client, DB connection, Gmail send or schedule operation lives here.
The runner delegates every individual write to the existing fail-closed
gmail_workflow.create_gmail_draft implementation.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Callable, Sequence

from src.outreach.compose import ARTICLE_LISTED, ARTICLE_UNCONFIRMED
from src.outreach.generation import READY, OutreachResult
from src.outreach.targets import EMAIL_FOUND, valid_email
from src.outreach.gmail_workflow import DraftWorkflowResult

MAX_AUTO_DRAFTS = 25
STOP_STATUSES = frozenset({"UNKNOWN", "REVIEW", "DISABLED"})


@dataclass(frozen=True)
class BatchHold:
    result: OutreachResult
    reason: str


@dataclass(frozen=True)
class DraftBatchPlan:
    eligible: tuple[OutreachResult, ...]
    held: tuple[BatchHold, ...]
    overflow: int = 0

    @property
    def total(self) -> int:
        return len(self.eligible) + len(self.held) + self.overflow


@dataclass(frozen=True)
class DraftBatchRun:
    outcomes: tuple[DraftWorkflowResult, ...]
    stopped_reason: str = ""

    @property
    def stopped_early(self) -> bool:
        return bool(self.stopped_reason)


def plan_auto_drafts(results: Sequence[OutreachResult], *, limit: int = MAX_AUTO_DRAFTS) -> DraftBatchPlan:
    """Pick eligible results deterministically; reject ambiguity in the *whole* input.

    This is a preview and never authorizes Gmail writing. No recipient may occur
    in more than one row even if the Clinic UUID differs.
    """
    if not 1 <= limit <= MAX_AUTO_DRAFTS:
        raise ValueError(f"batch limit must be between 1 and {MAX_AUTO_DRAFTS}")
    id_count = Counter(str(r.target.clinic_id) for r in results)
    uuid_count = Counter(r.target.clinic_uuid.strip().casefold() for r in results if r.target.clinic_uuid)
    email_count = Counter(r.target.email.strip().casefold() for r in results if r.target.email)
    eligible: list[OutreachResult] = []
    held: list[BatchHold] = []
    overflow = 0
    for r in results:
        t = r.target
        problems: list[str] = []
        if r.status != READY:
            problems.append("本文または確認情報がREADYではない")
        if not t.clinic_id or not t.clinic_uuid.strip() or not t.clinic_name.strip():
            problems.append("医院ID・UUID・医院名が不足")
        if id_count[str(t.clinic_id)] > 1 or (t.clinic_uuid and uuid_count[t.clinic_uuid.strip().casefold()] > 1):
            problems.append("同じ医院ID・UUIDが候補内で重複")
        if t.duplicate_uuid:
            problems.append("医院UUIDの重複フラグあり")
        if not t.email or email_count[t.email.strip().casefold()] > 1:
            problems.append("宛先未取得または複数医院でメールを共有")
        if t.email_status != EMAIL_FOUND or not valid_email(t.email):
            problems.append("確認済みEmail Researchアドレスが必要")
        if not t.director_name.strip() or not t.director_name_confirmed:
            problems.append("院長名未確認")
        if t.article.status == ARTICLE_UNCONFIRMED:
            problems.append("マイナビ掲載状況が未確認")
        if t.article.status == ARTICLE_LISTED and t.article.article_type != "NORMAL":
            problems.append("PR・監修・記事種別不明の掲載は自動下書きから除外")
        if not r.subject.strip() or not r.body.strip():
            problems.append("件名または本文が空欄")
        if problems:
            held.append(BatchHold(r, " / ".join(problems)))
        elif len(eligible) < limit:
            eligible.append(r)
        else:
            overflow += 1
    return DraftBatchPlan(tuple(eligible), tuple(held), overflow)


def run_approved_batch(
    selected: Sequence[OutreachResult],
    *,
    create_one: Callable[[OutreachResult], DraftWorkflowResult],
) -> DraftBatchRun:
    """Run at most 25 in order, without retry and with ambiguous-state stop.

    An explicit UI approval and feature-flag check MUST occur before calling.
    All draft, Gmail and DB checks are re-run by create_one for every clinic.
    """
    if not selected or len(selected) > MAX_AUTO_DRAFTS:
        raise ValueError("Batch size must be 1 to 25")
    check = plan_auto_drafts(selected, limit=MAX_AUTO_DRAFTS)
    if check.held or check.overflow or len(check.eligible) != len(selected):
        raise ValueError("Unsafe or duplicated target included in batch")
    outcomes: list[DraftWorkflowResult] = []
    for result in selected:
        try:
            outcome = create_one(result)
        except Exception:
            return DraftBatchRun(tuple(outcomes), "下書き処理中の例外。以降の医院は未処理として停止")
        outcomes.append(outcome)
        if outcome.status in STOP_STATUSES:
            return DraftBatchRun(tuple(outcomes), f"{outcome.status}のため残りの医院を自動処理せず停止")
    return DraftBatchRun(tuple(outcomes))
