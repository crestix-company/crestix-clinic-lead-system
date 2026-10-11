"""営業リスト用途のためのEmail Role Classification(Rule-based First)。

背景(ユーザー要件): 1医院から複数の正しいメールアドレス(info@/recruit@/reservation@/
Gmail/独自ドメイン等)が見つかる場合がある。営業リスト用途ではこの中から「営業利用に
最も適した代表メール」を安全に選ぶ必要があり、そのための前段として、各candidateへ
用途分類(role)と営業利用可否(sales_usable)を付与するのが本モジュールの責務。

設計方針:
- ドメイン(フリーメールか否か)だけでroleやsales_usableを判定しない。
  「お問い合わせ: xxx@gmail.com」はGENERAL、「採用のお問い合わせ: xxx@gmail.com」は
  RECRUITであり、domainは判断材料として最も優先度が低い。
  判定材料の優先順位(ユーザー指定): 1.メール周辺の文脈 > 2.掲載ページ種別
  > 3.link text/heading > 4.メールlocal-part > 5.domain。
- 最初からLLM分類にしない。本モジュールはRule-basedのみで判定し、判定材料が無い場合だけ
  UNKNOWNにする(LLM分類は本フェーズのスコープ外)。
- 既存のsrc.enrichment.email_research.email_extraction.classify_email_type
  (GENERAL/RESERVATION/RECRUIT/OTHER/UNKNOWN。official_hp_email_crawler.EmailEvidence.
  email_typeで使用中)は変更しない。本モジュールは完全に独立した新しいtaxonomy
  (OFFICE/MEDICAL_COOPERATION/PERSONALを含む、より営業リスト用途に特化した分類)を
  追加するだけであり、既存の呼び出し元には一切影響しない。
- Email Discovery(pipeline.py)やThree Gates(candidate_verifier.py)は変更しない。
  本モジュールはVERIFIED判定の後段(sidecar)として、純粋関数だけで構成する。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

CLASSIFIER_VERSION = "email-role-classifier-0.2.0"

ROLE_GENERAL = "GENERAL"
ROLE_OFFICE = "OFFICE"
ROLE_RESERVATION = "RESERVATION"
ROLE_RECRUIT = "RECRUIT"
ROLE_MEDICAL_COOPERATION = "MEDICAL_COOPERATION"
ROLE_PERSONAL = "PERSONAL"
ROLE_UNKNOWN = "UNKNOWN"

SALES_YES = "YES"
SALES_NO = "NO"
SALES_REVIEW = "REVIEW"

_SALES_USABLE_BY_ROLE: dict[str, tuple[str, str]] = {
    ROLE_GENERAL: (SALES_YES, "GENERAL(お問い合わせ全般)は営業連絡に利用可能"),
    ROLE_OFFICE: (SALES_YES, "OFFICE(医院事務の連絡先)は営業連絡に利用可能"),
    ROLE_RESERVATION: (SALES_NO, "RESERVATION(予約専用)は営業連絡に不適切"),
    ROLE_RECRUIT: (SALES_NO, "RECRUIT(採用専用)は営業連絡に不適切"),
    ROLE_MEDICAL_COOPERATION: (SALES_NO, "MEDICAL_COOPERATION(医療連携/紹介患者専用)は原則営業連絡に不適切"),
    ROLE_PERSONAL: (SALES_REVIEW, "PERSONAL(医師個人宛)は人手確認が必要"),
    ROLE_UNKNOWN: (SALES_REVIEW, "役割を確定できないため人手確認が必要"),
}

# 判定の優先順位(ユーザー指定の具体的用途)は、一般的なGENERALキーワードより先に評価する
# (例: 「採用のお問い合わせ」に"お問い合わせ"が含まれていても、RECRUITを優先する)。
_ROLE_KEYWORD_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    (ROLE_RECRUIT, re.compile(r"recruit|saiyo|採用|求人|応募|career|jobs", re.I)),
    (ROLE_RESERVATION, re.compile(
        r"appointment|reservation|reserve|yoyaku|診療予約|手術予約|オンライン相談|診療相談|予約受付|予約|booking",
        re.I,
    )),
    (ROLE_MEDICAL_COOPERATION, re.compile(
        r"renkei|medical[-_]?cooperation|地域連携|医療連携|紹介患者|連携室|連携科", re.I)),
    (ROLE_OFFICE, re.compile(r"\boffice\b|jimu|\bjim\b|事務局?|admin|メディア|取材|業者|広告関係", re.I)),
    (ROLE_PERSONAL, re.compile(r"院長(直通|個人|専用)|医師(本人|直通)|担当医師", re.I)),
    (ROLE_GENERAL, re.compile(r"\binfo\b|contact|inquiry|\bmail\b|お問い合わせ|問い合わせ|問合せ", re.I)),
)

_RESERVATION_SIGNAL_RE = re.compile(
    r"appointment|reservation|reserve|yoyaku|診療予約|手術予約|オンライン相談|診療相談|予約受付|予約|booking",
    re.I,
)
_NEGATION_RE = re.compile(
    r"(?:には|では|での|としては)?(?:使用|利用)?(?:しない|できない|不可)|"
    r"(?:受け付け|受付)(?:て)?(?:いない|おりません|ません|不可)|対象外",
    re.I,
)
_NEGATED_RESERVATION_CONTEXT_RE = re.compile(
    r"(?:メール.{0,24})?(?:診療予約|手術予約|診療相談|予約).{0,48}"
    r"(?:(?:使用|利用)?(?:しない|できない|不可)|(?:受け付け|受付)(?:て)?(?:いない|おりません|ません|不可)|対象外)",
    re.I,
)

# 判定材料の優先順位(高い順)。それぞれの段で判定できた場合の確信度(role_confidence)。
# Hardening Phase 2(Context Enrichment)での優先順位:
#   1. section_heading(直前の見出し/strong等) 2. nearby_text(見出し+前後段落の集約)
#   3. anchor_text(mailtoリンクの表示文字列) 4. parent_text(直近の親要素テキスト)
#   5. URL path / page_title 6. メールlocal-part
# domainはどの段でも使わない(フリーメールという理由だけで判定・除外しない)。
_TIER_CONFIDENCE: tuple[tuple[str, float], ...] = (
    ("section_heading", 0.95),
    ("nearby_text", 0.90),
    ("anchor_text", 0.78),
    ("parent_text", 0.72),
    ("page_type", 0.65),
    ("local_part", 0.55),
)


@dataclass(frozen=True)
class RoleClassification:
    role: str
    role_reason: str
    role_confidence: float
    sales_usable: str
    sales_usable_reason: str
    classifier_version: str = CLASSIFIER_VERSION


def _match_role(text: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", text or "")
    if not normalized.strip():
        return None
    for role, pattern in _ROLE_KEYWORD_PATTERNS:
        if role == ROLE_RESERVATION:
            if _NEGATED_RESERVATION_CONTEXT_RE.search(normalized):
                continue
            positive = False
            for match in _RESERVATION_SIGNAL_RE.finditer(normalized):
                # メール近傍の「予約には使用しない」「診療相談は受け付けない」は
                # reservation用途の否定。keyword後方の短い節だけを見て局所的に無効化する。
                suffix = normalized[match.end():match.end() + 32]
                if not _NEGATION_RE.search(suffix):
                    positive = True
                    break
            if positive:
                return role
            continue
        if pattern.search(normalized):
            return role
    return None


def classify_email_role(
    email: str,
    *,
    section_heading: str = "",
    nearby_text: str = "",
    anchor_text: str = "",
    parent_text: str = "",
    context_text: str = "",
    page_url: str = "",
    page_title: str = "",
    link_text: str = "",
) -> RoleClassification:
    """Rule-based Firstでemailの用途role + 営業利用可否を判定する。

    context_text/link_textは旧callerとの後方互換用(email_context_extractorの
    nearby_text/anchor_textが未提供の場合のfallbackとして、それぞれ同じ優先度枠で使う)。
    フリーメール(Gmail/Yahoo/Outlook等)かどうかは判定材料として一切使わない
    (domainはどの段でも使わない)。どの段でも判定材料が得られなければUNKNOWN(判定不能)とする。
    """
    local_part = email.split("@", 1)[0] if "@" in email else email

    signals = (
        ("section_heading", section_heading),
        ("nearby_text", nearby_text or context_text),
        ("anchor_text", anchor_text or link_text),
        ("parent_text", parent_text),
        ("page_type", f"{page_url} {page_title}"),
        ("local_part", local_part),
    )
    confidence_by_tier = dict(_TIER_CONFIDENCE)

    for tier_name, text in signals:
        role = _match_role(text)
        if role is not None:
            sales_usable, sales_reason = _SALES_USABLE_BY_ROLE[role]
            return RoleClassification(
                role=role,
                role_reason=f"{tier_name}内のキーワードから{role}と判定(入力: {text!r})",
                role_confidence=confidence_by_tier[tier_name],
                sales_usable=sales_usable,
                sales_usable_reason=sales_reason,
            )

    sales_usable, sales_reason = _SALES_USABLE_BY_ROLE[ROLE_UNKNOWN]
    return RoleClassification(
        role=ROLE_UNKNOWN,
        role_reason="文脈/ページ種別/link text/local-partのいずれにも判定材料が無かった"
                     "(domainだけでは判定しない)",
        role_confidence=0.3,
        sales_usable=sales_usable,
        sales_usable_reason=sales_reason,
    )
