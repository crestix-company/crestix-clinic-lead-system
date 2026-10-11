"""クリニック名/所在地/第三者組織判定の共通ユーティリティ(Gate A/C共通ロジック)。

背景: candidate_verifier.py(Gate A/C)とofficial_site_trust.py(Official Website Trust Gate)
の両方が「医院名の核が本文に存在するか」「異なる都道府県が明記されていないか」
「学会/財団/協会等の第三者組織キーワードが含まれていないか」という同じ判定を必要とする。
両モジュール間の循環import(official_site_trust -> candidate_verifier -> official_site_trust)
を避けるため、この判定ロジック自体を本モジュールへ切り出して一箇所に共通化し、
candidate_verifier.py/official_site_trust.pyの両方がここを再利用する(判定ロジックの複製禁止)。

本モジュールは純粋関数のみ(HTTP/API接続は一切行わない)。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from src.master.email_sales_eligibility import is_public_municipal_clinic, source_owner_type

# Gate A (Clinic Identity): 「医療法人社団X会 Y医院」のような空白区切りの命名規則を前提に、
# 最後のトークンを施設名の核とみなす。空白が無い場合は既知の法人格prefixだけを取り除く
# (法人の固有名称までは判別できないため、そのまま残す=厳しめに判定されるだけで安全側)。
_LEGAL_ENTITY_PREFIX_RE = re.compile(
    r"^(?:医療法人(?:社団|財団)?|一般社団法人|公益社団法人|公益財団法人|社会医療法人|"
    r"特定医療法人|独立行政法人)\s*"
)

_PREFECTURES: tuple[str, ...] = (
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県", "茨城県", "栃木県", "群馬県",
    "埼玉県", "千葉県", "東京都", "神奈川県", "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県",
    "岐阜県", "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県",
    "鳥取県", "島根県", "岡山県", "広島県", "山口県", "徳島県", "香川県", "愛媛県", "高知県", "福岡県",
    "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
)

# Gate C (Email Ownership): 学会/財団/協会/連合会/事務局/各種職能団体/自治体部署はクリニック自身ではない。
# ドメインのブロックリスト(search_candidate_adapter.is_third_party_domain)で列挙しきれない
# 任意の第三者組織を、取得したページの内容(title/本文)から判定するための補助シグナル。
# 自治体部署キーワード(市役所/介護高齢課等)はclinic_id=99522インシデント
# (あむデンタルクリニックのPrimaryが芦別市介護高齢課のEmailへ誤帰属)を受けて追加。
_THIRD_PARTY_ORG_KEYWORD_RE = re.compile(
    "学会|財団|振興財団|協会|連合会|事務局|医師会|歯科医師会|薬剤師会|"
    "市役所|町役場|村役場|区役所|県庁|道庁|府庁|都庁|"
    "介護高齢課|地域包括支援|福祉課|保健所|保健センター|社会福祉協議会"
)


def _core_clinic_name(clinic_name: str) -> str:
    normalized = unicodedata.normalize("NFKC", clinic_name or "").strip()
    parts = normalized.split()
    if len(parts) >= 2:
        return parts[-1]
    return _LEGAL_ENTITY_PREFIX_RE.sub("", normalized).strip()


def _core_name_present(page_text: str, clinic_name: str) -> bool:
    """Gate A: 医院名(正式名称の核)が再取得ページ上に文字列として存在するか。"""
    core = _core_clinic_name(clinic_name)
    if not core:
        return True
    return core in unicodedata.normalize("NFKC", page_text or "")


def _ownership_name_present(page_text: str, clinic_name: str) -> bool:
    """Ownership用の限定的な名称同義判定（脳神経外科↔脳外科のみ）。"""
    if _core_name_present(page_text, clinic_name):
        return True
    text = unicodedata.normalize("NFKC", page_text or "")
    core = _core_clinic_name(clinic_name)
    variants = {core.replace("脳神経外科", "脳外科"), core.replace("脳外科", "脳神経外科")}
    return any(v and v in text for v in variants)


def _location_contradiction(page_text: str, prefecture: str) -> bool:
    """Gate A: ページ内に都道府県名の記載があり、それが期待する都道府県と異なる場合だけ
    「所在地相違の疑い」とする(記載自体が無い場合は判断材料不足として矛盾扱いしない)。"""
    prefecture = (prefecture or "").strip()
    if not prefecture:
        return False
    text = unicodedata.normalize("NFKC", page_text or "")
    found = {p for p in _PREFECTURES if p in text}
    return bool(found) and prefecture not in found


def _is_third_party_organization_context(page_text: str) -> bool:
    """Gate C: ページ内容が学会/財団/協会等の第三者組織を示しているか。"""
    return bool(_THIRD_PARTY_ORG_KEYWORD_RE.search(unicodedata.normalize("NFKC", page_text or "")))


OWNERSHIP_MATCH = "MATCH"
OWNERSHIP_MISMATCH = "MISMATCH"
OWNERSHIP_REVIEW = "REVIEW"

_FACILITY_NAME_RE = re.compile(
    r"[^\s、。|/]{2,50}(?:病院|診療所|医院|クリニック|医師会|歯科医師会|学会|財団|協会)"
)
_OWNERSHIP_ROLE_RE = re.compile(r"研修担当|採用担当|事務局|研修医|レジデント|医師募集|職員募集")


@dataclass(frozen=True)
class EmailOwnershipAssessment:
    status: str
    reason: str


def _organization_tokens(clinic_name: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", clinic_name or "")
    tokens = set(re.findall(r"(?:[一-鿿ぁ-ゟァ-ヶA-Za-z0-9]+(?:会|法人))", normalized))
    tokens.update(t for t in re.split(r"[\s　]+", normalized) if len(t) >= 3 and t.endswith("会"))
    return {t for t in tokens if t not in {"医療法人", "医療法人社団", "医療法人財団", "社団法人", "財団法人"}}


def assess_email_ownership(*, clinic_name: str, email_context, source_url: str = "") -> EmailOwnershipAssessment:
    """Page identityと分離し、メール周辺の所有者だけをfail-closedで判定する。

    Source Owner(ページの掲載者) != Email Owner(メールの所有者)。対象医院名が
    ページ上に(ディレクトリの一覧項目として)存在することは、個々のメールが
    その医院に帰属する証拠にはならない -- 直近の第三者組織/自治体文脈が
    決定的である場合は、医院名の近接だけでMATCHへ覆さない
    (clinic_id=99522インシデント: 芦別市の医療機関ディレクトリに医院名はあったが、
    掲載Emailは介護高齢課のものだった)。
    """
    fields = (
        "nearest_heading", "section_heading", "previous_heading", "anchor_text", "immediate_text",
        "parent_text", "previous_paragraph", "next_paragraph", "nearby_text", "footer_text",
        "operator_text", "organization_names", "page_title",
    )
    context_text = " ".join(str(getattr(email_context, name, "") or "") for name in fields)
    context_text = unicodedata.normalize("NFKC", f"{context_text} {source_url}")
    target_present = _ownership_name_present(context_text, clinic_name)
    same_org_tokens = _organization_tokens(clinic_name)
    same_org = any(token in context_text for token in same_org_tokens)
    foreign_facilities = [name for name in _FACILITY_NAME_RE.findall(context_text)
                          if not _ownership_name_present(name, clinic_name)]

    if same_org:
        return EmailOwnershipAssessment(
            OWNERSHIP_MATCH,
            f"メール周辺/運営者表記で対象と同一法人({next(t for t in sorted(same_org_tokens) if t in context_text)})を確認",
        )
    # source_owner_type() already exempts known TRUSTED_PUBLIC_DIRECTORY hosts (e.g.
    # MHLW's iryou.teikyouseido.mhlw.go.jp) from GOVERNMENT classification even though
    # they share the .go.jp suffix; delegate rather than duplicate that precedence.
    if (source_owner_type(source_url=source_url, clinic_name=clinic_name) == "GOVERNMENT"
            and not is_public_municipal_clinic(clinic_name)):
        return EmailOwnershipAssessment(
            OWNERSHIP_MISMATCH,
            "掲載ページが自治体/政府ドメインであり、対象医院自身が市立/町立/村立/県立/公立等でない",
        )
    if foreign_facilities and (_OWNERSHIP_ROLE_RE.search(context_text) or not target_present):
        return EmailOwnershipAssessment(
            OWNERSHIP_MISMATCH,
            "メール周辺に対象医院と異なる組織名を確認: " + ", ".join(foreign_facilities[:3]),
        )
    local_scope = " ".join(str(getattr(email_context, name, "") or "") for name in (
        "nearest_heading", "section_heading", "previous_heading", "anchor_text", "immediate_text",
        "parent_text", "previous_paragraph", "next_paragraph", "nearby_text",
    ))
    if _is_third_party_organization_context(local_scope) and not same_org:
        return EmailOwnershipAssessment(
            OWNERSHIP_MISMATCH,
            "メール周辺が医師会/学会/協会/財団/事務局/自治体部署等の第三者組織窓口を示す"
            "(医院名がページ上の一覧項目として近接していても、第三者組織窓口の文脈が優先される)",
        )
    if _OWNERSHIP_ROLE_RE.search(context_text) and not target_present:
        return EmailOwnershipAssessment(
            OWNERSHIP_MISMATCH,
            "メール周辺が別組織の研修/採用/事務局窓口を示し、対象医院名が近傍に無い",
        )
    if target_present:
        return EmailOwnershipAssessment(OWNERSHIP_MATCH, "メール周辺に対象医院名を確認")
    return EmailOwnershipAssessment(
        OWNERSHIP_REVIEW, "メール周辺だけで対象医院/同一法人の所有を確認できない",
    )
