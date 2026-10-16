"""Crestix official outreach email template v2.

Pure preview rendering only. This module has no Gmail, network, or database access.
The caller must supply evidence flags and the immutable article-master URL set.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

ARTICLE_PRESENT = "ARTICLE_PRESENT"
ARTICLE_ABSENT = "ARTICLE_ABSENT"
ARTICLE_UNVERIFIED = "ARTICLE_UNVERIFIED"
TEMPLATE_VERSION = "CRESTIX_OUTREACH_V2"
SUBJECT = "【お伝えしておりましたマイナビ記事について】株式会社Crestixの高原"
PHONE_FOLLOWUP_LINE = (
    "本日、貴院へお電話させていただき、ご対応いただいた方よりメールにてお送りするよう伺いましたので、ご連絡いたしました。"
)
LISTED_PURPOSE = (
    "現在、マイナビにて掲載させていただいている下記記事について、掲載データの共有・現状のご報告と、掲載内容の確認をさせていただければと考えております。"
)
ABSENT_PURPOSE = (
    "現在、マイナビクリニックナビにて掲載されている貴院の医院情報について、掲載データの共有・現状のご報告と、掲載内容の確認をさせていただければと考えております。"
)
COMMON_ENDING = """なお、掲載内容についての行き違い防止のため、お打ち合わせ当日は院長先生にもご参加いただく形でお願いしております。

恐れ入りますが、院長先生がご参加可能な日時を、下記候補よりお知らせいただけますでしょうか。

{schedule}

上記よりオンラインにて20分～30分ほど頂戴できますと幸いです。
何卒よろしくお願い申し上げます。"""
_PLACEHOLDER = re.compile(r"(?:\{[^{}]+\}|【[^】]*(?:未確認|確認待ち|未設定)[^】]*】)")


class TemplateV2Error(ValueError):
    """Safe validation error for template input."""


@dataclass(frozen=True)
class PreviewInput:
    clinic_name: str
    director_name: str = ""
    director_name_verified: bool = False
    article_status: str = ARTICLE_UNVERIFIED
    actual_article_url: str = ""
    recommended_article_url: str = ""
    clinic_detail_url: str = ""
    clinic_detail_verified: bool = False
    article_listing_verified: bool = False
    article_absence_verified: bool = False
    schedule: tuple[str, ...] = ()
    sender_name: str = "高原"
    phone_call_date: date | None = None
    email_send_request_confirmed: bool = False
    recipient_email_verified: bool = False
    gmail_account_verified: bool = False
    human_approved: bool = False
    duplicate_check_complete: bool = False
    article_master_urls: frozenset[str] = frozenset()
    as_of: date | None = None


@dataclass(frozen=True)
class EmailPreview:
    subject: str
    body: str
    article_status: str
    preview_generated: bool
    ready_for_draft: bool
    blockers: tuple[str, ...]


def _required(value: str, label: str) -> str:
    value = (value or "").strip()
    if not value or value.casefold() in {"null", "none", "nan", "不明"}:
        raise TemplateV2Error(f"{label}未確認")
    if any(char in value for char in "\r\n"):
        raise TemplateV2Error(f"{label}に改行が含まれています")
    return value


def _article_url(value: str, master_urls: frozenset[str]) -> str:
    value = _required(value, "記事URL")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname != "clinic.mynavi.jp"
            or not parsed.path.startswith("/article/") or parsed.query or parsed.fragment
            or parsed.username or parsed.password):
        raise TemplateV2Error("記事URLが正式なマイナビ紹介記事URLではありません")
    if value not in master_urls:
        raise TemplateV2Error("記事URLが原本CSVのURLと完全一致しません")
    return value


def _detail_url(value: str) -> str:
    value = _required(value, "医院詳細ページURL")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname != "clinic.mynavi.jp"
            or not parsed.path.startswith("/clinic/") or parsed.query or parsed.fragment
            or parsed.username or parsed.password):
        raise TemplateV2Error("医院詳細ページURLが正規のマイナビ医院詳細URLではありません")
    return value


def _validate_schedule(
    schedule: tuple[str, ...], *, as_of: date, now: datetime | None = None,
) -> tuple[str, ...]:
    result = []
    current = now or datetime.now().astimezone()
    current_local = current.replace(tzinfo=None)
    for item in schedule:
        try:
            slot = datetime.strptime(item, "%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            raise TemplateV2Error("候補日時はYYYY-MM-DD HH:MM形式で設定してください") from None
        day = slot.date()
        if day < as_of or slot <= current_local:
            raise TemplateV2Error("過去の候補日時は使用できません")
        if day.weekday() >= 5:
            raise TemplateV2Error("候補日時は営業日を指定してください")
        result.append(f"{day.month}月{day.day}日（{'月火水木金土日'[day.weekday()]}）{item[11:]}")
    if len(set(result)) != len(result):
        raise TemplateV2Error("候補日時が重複しています")
    return tuple(result)


def render_preview(data: PreviewInput) -> EmailPreview:
    clinic = _required(data.clinic_name, "医院名")
    staff = _required(data.sender_name, "送信担当者名")
    if staff != "高原":
        raise TemplateV2Error("正式テンプレートv2の送信者名は「高原」固定です")
    as_of = data.as_of or date.today()
    called_today = data.phone_call_date == as_of and data.email_send_request_confirmed
    subject = SUBJECT
    blockers: list[str] = []
    if data.article_status == ARTICLE_UNVERIFIED:
        return EmailPreview(subject, "", data.article_status, False, False, ("掲載状況未確認",))
    if data.article_status not in {ARTICLE_PRESENT, ARTICLE_ABSENT}:
        raise TemplateV2Error("記事判定ステータスが不正です")

    director = data.director_name.strip() if data.director_name_verified else ""
    if director and any(char in director for char in "\r\n"):
        raise TemplateV2Error("院長名に改行が含まれています")
    salutation = f"院長 {director} 様" if director else "院長 〔院長名確認待ち〕 様"
    if not director:
        blockers.append("院長名未確認")

    if data.schedule:
        schedule_lines = _validate_schedule(data.schedule, as_of=as_of)
        schedule = "\n".join(schedule_lines)
    else:
        schedule = "〔候補日時未設定〕"
        blockers.append("候補日時未設定")

    opening = (
        f"お世話になっております。\n株式会社Crestixの{staff}と申します。\n\n"
        f"{PHONE_FOLLOWUP_LINE}"
    )
    if not called_today:
        blockers.append("本日架電およびメール送付依頼の確認が必要")

    if data.article_status == ARTICLE_PRESENT:
        if not data.article_listing_verified:
            return EmailPreview(subject, "", data.article_status, False, False,
                                tuple(blockers + ["実掲載記事の医院一致証拠未確認"]))
        actual_url = _article_url(data.actual_article_url, data.article_master_urls)
        article_block = f"【掲載いただいている記事】\n{actual_url}"
        purpose = LISTED_PURPOSE
    else:
        if not data.clinic_detail_verified:
            return EmailPreview(subject, "", data.article_status, False, False,
                                tuple(blockers + ["マイナビ医院詳細ページ未確認"]))
        if not data.article_absence_verified:
            return EmailPreview(subject, "", data.article_status, False, False,
                                tuple(blockers + ["紹介記事がないことの確認証拠未確認"]))
        recommended_url = _article_url(data.recommended_article_url, data.article_master_urls)
        detail_url = _detail_url(data.clinic_detail_url)
        article_block = (
            f"【記事例】\n{recommended_url}\n\n"
            f"【掲載いただいているページ】\n{detail_url}"
        )
        purpose = ABSENT_PURPOSE
        if data.article_listing_verified:
            raise TemplateV2Error("ARTICLE_ABSENTなのに記事掲載確認フラグが設定されています")

    body = f"""{clinic}
{salutation}

{opening}

{purpose}

{article_block}

{COMMON_ENDING.format(schedule=schedule)}"""
    if _PLACEHOLDER.search(body):
        blockers.append("本文に確認待ちプレースホルダーあり")
    if not data.director_name_verified or not director:
        blockers.append("院長名確認が必要")
    if not data.schedule:
        blockers.append("候補日時の設定が必要")
    if not data.recipient_email_verified:
        blockers.append("承認済みPrimary Email未確認")
    if not data.gmail_account_verified:
        blockers.append("指定Gmailアカウント未確認")
    if not data.human_approved:
        blockers.append("人間承認未完了")
    if not data.duplicate_check_complete:
        blockers.append("Gmail下書き重複検査未完了")
    blockers = list(dict.fromkeys(blockers))
    return EmailPreview(subject, body, data.article_status, True, not blockers, tuple(blockers))


def load_settings(path: str | Path) -> dict:
    """Load non-secret sender/date settings. Missing or empty dates remain non-sendable."""
    settings_path = Path(path)
    try:
        value = json.loads(settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"sender_name": "高原", "candidate_dates": []}
    except (OSError, json.JSONDecodeError) as exc:
        raise TemplateV2Error("メールv2設定ファイルを読み込めません") from exc
    if not isinstance(value, dict):
        raise TemplateV2Error("メールv2設定の形式が不正です")
    sender = _required(value.get("sender_name", "高原"), "送信担当者名")
    if sender != "高原":
        raise TemplateV2Error("正式テンプレートv2の送信者名は「高原」固定です")
    dates = value.get("candidate_dates", [])
    if not isinstance(dates, list):
        raise TemplateV2Error("candidate_datesは配列で指定してください")
    return {
        "sender_name": sender,
        "candidate_dates": tuple(str(item) for item in dates),
    }
