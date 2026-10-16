"""「メール送付文章」本文テンプレート（記事あり／記事なし／記事未確認）。

Pure functions only: no network, DB, Gmail or file side effects.
- 記事あり: 共有済みの州会クリニック向け固定テンプレート（email_template.render_draftと同一文面）。
- 記事なし: 掲載済みと誤認させない、おすすめ記事紹介型の暫定テンプレート。
- 記事未確認: 記事なしと同じ安全な文面で生成し、呼び出し側でNEEDS_REVIEWに分類する。
電話連絡の事実が確認できない医院には「本日お電話した」等の文を入れない。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from urllib.parse import urlsplit

from .email_template import SIGNATURE, SUBJECT, WEEKDAYS, DraftValidationError

ARTICLE_LISTED = "LISTED"          # 記事あり（掲載確認済み）
ARTICLE_NOT_LISTED = "NOT_LISTED"  # 記事なし（未掲載確認済み）
ARTICLE_UNCONFIRMED = "UNCONFIRMED"  # 記事未確認
ARTICLE_STATUS_LABELS = {
    ARTICLE_LISTED: "記事あり", ARTICLE_NOT_LISTED: "記事なし", ARTICLE_UNCONFIRMED: "未確認",
}

TEMPLATE_LISTED = "LISTED_FIXED_V1"
TEMPLATE_RECOMMEND = "RECOMMEND_RELATED_V2"

# A/Bテスト・送信トラッキングは未実装（今回のスコープ外）。将来の集計をCSV単位で可能にするため、
# テンプレート名を id/version に分解した対応表のみ用意しておく。新テンプレート追加時はここに追記する。
TEMPLATE_IDS = {TEMPLATE_LISTED: "LISTED_FIXED", TEMPLATE_RECOMMEND: "RECOMMEND_RELATED"}
TEMPLATE_VERSIONS = {TEMPLATE_LISTED: "v1", TEMPLATE_RECOMMEND: "v2"}

CALLED_LINE = "本日、貴院へお電話させていただき、ご対応いただいた方よりメールにてお送りするよう伺いましたので、ご連絡いたしました。"
NOT_CALLED_LINE = "この度、マイナビクリニックナビにて公開されている記事について、貴院の診療科目や地域に関連する内容がございましたので、ご紹介のためご連絡いたしました。"
NEUTRAL_SUBJECT = "【マイナビクリニックナビの関連記事について】株式会社Crestix {staff}"


def _line(label: str, value: str) -> str:
    value = (value or "").strip()
    if not value or value.lower() in {"null", "none", "nan", "不明"}:
        raise DraftValidationError(f"{label}が未確認です")
    if any(c in value for c in "\r\n"):
        raise DraftValidationError(f"{label}に改行が含まれています")
    return value


def _article_url(value: str) -> str:
    value = _line("記事URL", value)
    part = urlsplit(value)
    if (part.scheme != "https" or part.hostname != "clinic.mynavi.jp" or not part.path.startswith("/article/")
            or part.username or part.password or part.query or part.fragment):
        raise DraftValidationError("記事URLはclinic.mynavi.jpの/article/のみ使用できます")
    return value


def format_schedule(dates: tuple[date, ...], start: str, end: str, *,
                    not_before: date | None = None) -> str:
    if not dates:
        raise DraftValidationError("日程候補が指定されていません")
    if any(not isinstance(day, date) for day in dates):
        raise DraftValidationError("日程候補に不正な日付が含まれています")
    if len(set(dates)) != len(dates):
        raise DraftValidationError("日程候補が重複しています")
    if not_before is not None and any(day < not_before for day in dates):
        raise DraftValidationError("日程候補に過去の日付が含まれています")
    start, end = _line("開始時刻", start), _line("終了時刻", end)
    def parse_clock(label, value):
        try:
            hour_text, minute_text = value.split(":", 1)
            return time(int(hour_text), int(minute_text))
        except (AttributeError, TypeError, ValueError):
            raise DraftValidationError(f"{label}はH:MM形式の正しい時刻を指定してください") from None
    if parse_clock("開始時刻", start) >= parse_clock("終了時刻", end):
        raise DraftValidationError("終了時刻は開始時刻より後を指定してください")
    lines = []
    for day in sorted(dates):
        if day.weekday() >= 5:
            raise DraftValidationError("日程候補に土日が含まれています")
        lines.append(f"{day.month}月{day.day}日（{WEEKDAYS[day.weekday()]}）{start}～{end}")
    return "\n".join(lines)


@dataclass(frozen=True)
class Recipient:
    clinic_name: str
    role: str = "院長"
    name: str = ""  # 空なら「院長先生」宛てにする（要確認扱いは呼び出し側）


def _header(recipient: Recipient, staff: str, called: bool) -> str:
    clinic = _line("医院名", recipient.clinic_name)
    name = (recipient.name or "").strip()
    if any(c in name for c in "\r\n"):
        raise DraftValidationError("宛名に改行が含まれています")
    role = (recipient.role or "院長").strip()
    addressee = f"{role} {name} 様" if name else ("ご担当者様" if role == "ご担当者" else f"{role}先生")
    return f"""{clinic}
{addressee}

お世話になっております。
株式会社Crestixの{staff}と申します。

{CALLED_LINE if called else NOT_CALLED_LINE}"""


def subject_for(staff: str, *, called: bool = True) -> str:
    staff = _line("IS担当者", staff)
    return (SUBJECT if called else NEUTRAL_SUBJECT).format(staff=staff)


def render_listed(recipient: Recipient, *, staff: str, example_url: str, listed_url: str,
                  schedule: str, called: bool) -> str:
    """記事あり: 州会クリニック向け固定テンプレート。掲載確認済み記事URLが必須。"""
    staff = _line("IS担当者", staff)
    example, listed = _article_url(example_url), _article_url(listed_url)
    return f"""{_header(recipient, staff, called)}

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


def render_recommend(recipient: Recipient, *, staff: str, article_title: str, article_url: str,
                     schedule: str, called: bool) -> str:
    """記事なし／未確認: 掲載済みと誤認させない関連記事紹介型テンプレート。"""
    staff = _line("IS担当者", staff)
    _line("記事タイトル", article_title)  # CSV/監査用。本文は承認済み仕様どおりURLのみを掲載する。
    url = _article_url(article_url)
    return f"""{_header(recipient, staff, called)}

【関連記事】
{url}

今回、記事の内容やマイナビクリニックナビでの情報発信について、貴院にもご参考いただける情報を共有できればと考えております。

可能でしたら、院長先生にもご参加いただく形で、オンラインにて20分～30分ほどお時間を頂戴できますと幸いです。

【候補日時】
{schedule}

ご都合のよい日時がございましたら、お知らせいただけますと幸いです。

何卒よろしくお願い申し上げます。

{SIGNATURE}"""
