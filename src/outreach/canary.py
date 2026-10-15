"""50医院Canary監査（純関数）。生成結果を元データ・記事原本と突き合わせる。"""
from __future__ import annotations

import re
from collections import Counter

from .compose import ARTICLE_LISTED
from .generation import FAILED, NEEDS_REVIEW, GenerationSettings, OutreachResult

_URL = re.compile(r"https?://[^\s）)]+")
LISTING_CLAIMS = ("掲載させていただいている", "掲載いただいている")
CALL_CLAIMS = ("お電話", "本日")


def audit(results: list[OutreachResult], *, master_urls: set[str], source_uuids: dict[int, str],
          emails: dict[int, str], settings: GenerationSettings) -> dict:
    issues: list[dict] = []

    def fail(check, r, detail=""):
        issues.append({"check": check, "clinic_uuid": r.target.clinic_uuid, "clinic_name": r.target.clinic_name,
                       "detail": detail})

    uuids = Counter(r.target.clinic_uuid.casefold() for r in results)
    for r in results:
        t = r.target
        # 1. Clinic UUID整合性
        if not t.clinic_uuid or uuids[t.clinic_uuid.casefold()] != 1 or source_uuids.get(t.clinic_id) != t.clinic_uuid:
            fail("clinic_uuid", r, "欠損・重複・元データ不一致")
        if r.status == FAILED:
            continue
        body = r.body
        if r.status == NEEDS_REVIEW and not body:
            # Fixed listed copy is intentionally not rendered until all factual gates pass.
            continue
        # 2. 記事URLの原本一致（本文中の記事URL・推奨候補すべて）
        body_urls = [u for u in _URL.findall(body) if "mynavi" in u]
        for url in body_urls + [x.article.url for x in r.recommendations]:
            if url not in master_urls:
                fail("article_url_in_master", r, url)
        # 3. 記事と診療科の適合性
        if r.first and not any(x.startswith(("標榜科一致", "治療・検査一致")) for x in r.first.reasons):
            fail("article_specialty_fit", r, " / ".join(r.first.reasons))
        # 4. 宛先・医院名・院長名
        expected_to = emails.get(t.clinic_id, "")
        if t.email != expected_to:
            fail("recipient_email", r, f"{t.email!r} != {expected_to!r}")
        if not body.startswith(t.clinic_name + "\n"):
            fail("clinic_name", r)
        if t.director_name and f"院長 {t.director_name} 様" not in body:
            fail("director_name", r)
        # 5. IS担当者・日程
        if not r.subject.endswith(settings.staff) or f"株式会社Crestixの{settings.staff}と申します" not in body:
            fail("is_staff", r)
        for d in settings.dates:
            if f"{d.month}月{d.day}日" not in body:
                fail("schedule", r, d.isoformat())
        # 6. 事実関係（掲載断定・電話連絡）
        if t.article.status != ARTICLE_LISTED and r.listed_url == "" and any(p in body for p in LISTING_CLAIMS):
            fail("fact_listing_claim", r)
        if not r.called and any(p in body for p in CALL_CLAIMS):
            fail("fact_phone_claim", r)
        # 7. 要確認への振り分け。掲載未確認は中立文ならContent READYを許可する。
        if not t.email and r.status != NEEDS_REVIEW:
            fail("review_routing", r, r.status)
    by_check = Counter(i["check"] for i in issues)
    return {
        "total": len(results),
        "status_counts": dict(Counter(r.status for r in results)),
        "article_status_counts": dict(Counter(r.target.article.status for r in results)),
        "issue_counts": dict(by_check),
        "issues": issues,
        "passed": not issues,
    }
