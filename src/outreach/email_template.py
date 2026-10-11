"""Deterministic outreach email drafts. No network, DB writes, or Gmail side effects."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urlsplit

SUBJECT = "【お伝えしておりましたマイナビ記事について】株式会社Crestix {staff}"
SIGNATURE = """******************************************
株式会社Crestix 営業窓口
Phone：080-7323-8071
Mail：info@crestix-inc.com

〒101-0054
東京都千代田区神田錦町1丁目21−2
大手町モダンビルディング 4F
******************************************"""
WEEKDAYS = "月火水木金土日"


class DraftValidationError(ValueError):
    """A field requires verification before a draft can be produced."""


def _required(label: str, value: str) -> str:
    value = (value or "").strip()
    if not value or value.lower() in {"null", "none", "不明"}:
        raise DraftValidationError(f"{label}が未確認です")
    if any(c in value for c in "\r\n"):
        raise DraftValidationError(f"{label}に改行が含まれています")
    return value


def _mynavi_url(value: str) -> str:
    value = _required("マイナビURL", value)
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != "clinic.mynavi.jp" or not parsed.path.startswith(("/article/", "/clinic/")):
        raise DraftValidationError("マイナビURLが許可されていません")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise DraftValidationError("マイナビURLは正規のURLを指定してください")
    return value


@dataclass(frozen=True)
class DraftInput:
    clinic_name: str
    recipient_role: str
    recipient_name: str
    is_staff_name: str
    example_article_url: str
    listing_url: str
    listing_kind: str  # VERIFIED_ARTICLE or VERIFIED_PROFILE
    listing_verified: bool
    called_today_and_told_to_email: bool
    director_attendance_requested: bool
    candidate_dates: tuple[date, ...]
    candidate_start: str = "9:00"
    candidate_end: str = "21:00"


@dataclass(frozen=True)
class DraftContent:
    subject: str
    body: str


def five_weekdays(start: date) -> tuple[date, ...]:
    """Return five weekdays starting at start; does not claim Google Calendar availability."""
    dates = []
    cursor = start
    while len(dates) < 5:
        if cursor.weekday() < 5:
            dates.append(cursor)
        cursor += timedelta(days=1)
    return tuple(dates)


def render_draft(data: DraftInput) -> DraftContent:
    name = _required("医院名", data.clinic_name)
    role = _required("役職", data.recipient_role)
    director = _required("宛名", data.recipient_name)
    staff = _required("IS担当者", data.is_staff_name)
    example = _mynavi_url(data.example_article_url)
    listed = _mynavi_url(data.listing_url)
    if not example.startswith("https://clinic.mynavi.jp/article/"):
        raise DraftValidationError("記事例はarticle URLのみ指定できます")
    if data.listing_kind not in {"VERIFIED_ARTICLE", "VERIFIED_PROFILE"} or not data.listing_verified:
        raise DraftValidationError("掲載記事・医院プロフィールの確認が必要です")
    expected = "/article/" if data.listing_kind == "VERIFIED_ARTICLE" else "/clinic/"
    if not urlsplit(listed).path.startswith(expected):
        raise DraftValidationError("掲載種別とURLが一致しません")
    if not data.called_today_and_told_to_email:
        raise DraftValidationError("電話連絡・メール案内の事実が未確認です")
    if not data.director_attendance_requested:
        raise DraftValidationError("院長参加依頼文の使用可否が未確認です")
    if len(data.candidate_dates) != 5 or len(set(data.candidate_dates)) != 5:
        raise DraftValidationError("日程候補は重複しない5日を指定してください")
    lines = []
    for day in data.candidate_dates:
        if day.weekday() >= 5:
            raise DraftValidationError("日程候補に土日が含まれています")
        lines.append(f"{day.month}月{day.day}日（{WEEKDAYS[day.weekday()]}）{data.candidate_start}～{data.candidate_end}")
    schedule = "\n".join(lines)
    body = f"""{name}
{role} {director} 様

お世話になっております。
株式会社Crestixの{staff}と申します。

本日、貴院へお電話させていただき、ご対応いただいた方よりメールにてお送りするよう伺いましたので、ご連絡いたしました。

現在、マイナビにて掲載させていただいている下記記事について、掲載データの共有・現状のご報告と、掲載内容の確認をさせていただければと考えております。

【記事例】
{example}

【掲載いただいている記事】
{listed}

なお、掲載内容についての行き違い防止のため、お打ち合わせ当日は院長先生にもご参加いただく形でお願いしております。

恐れ入りますが、院長先生がご参加可能な日時を下記よりお知らせいただけますでしょうか。

{schedule}

上記よりオンラインにて20分～30分ほど頂戴できますと幸いです。
何卒よろしくお願い申し上げます。

{SIGNATURE}"""
    return DraftContent(subject=SUBJECT.format(staff=staff), body=body)
