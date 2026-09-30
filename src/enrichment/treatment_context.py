"""Lightweight page-structure and provenance context for Phase 7-B treatment evidence.

Answers three questions about an alias hit without any extra HTTP requests or heavy
NLP: (1) what kind of page/section is this ("page_type"), (2) is the treatment
attributed to THIS clinic or some other facility/person ("exclusion_context"), and
(3) does the sentence itself read as a self-offer statement ("provider_context").
Reuses the already-fetched, already-parsed Page/soup — no new crawling.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from src.enrichment.hp_analysis import page_soup, sanitize_text
from src.normalizer.clinic_name import normalize_clinic_name

HEADING_LEVELS = {"h1": 1, "h2": 2, "h3": 3}
NON_CONTENT_TAGS = {"script", "style", "noscript", "template"}
# Block-level containers whose boundary becomes a hard segment break when flattening
# text to a string (see build_evidence_blocks). Deliberately excludes generic wrappers
# like div/span, which are often pure styling hooks around otherwise-flowing text —
# only tags that reliably mark a distinct list-item/paragraph/table-cell in real markup.
_BLOCK_BOUNDARY_TAGS = {"p", "li", "dt", "dd", "td", "th", "tr", "br"}

PAGE_TYPE_RULES = (
    ("RESERVATION", re.compile(r"reserv|予約|yoyaku", re.I)),
    ("PRICE", re.compile(r"price|fee|料金|費用|価格")),
    ("TREATMENT_MENU", re.compile(r"施術一覧|治療メニュー|診療メニュー|施術メニュー|メニュー|menu", re.I)),
    ("TREATMENT_DETAIL", re.compile(r"treatment|surgery|therapy|施術詳細|治療詳細|/(?:gastroscopy|colonoscopy|icl|cataract|liposuction)", re.I)),
    ("MEDICAL_GUIDE", re.compile(r"guide|診療案内|診療内容|治療案内|検査案内|手術案内|専門外来", re.I)),
    ("FAQ", re.compile(r"faq|よくある質問|q&a|q＆a", re.I)),
    ("NEWS", re.compile(r"news|お知らせ|ニュース|topics?", re.I)),
    ("BLOG", re.compile(r"blog|column|コラム|ブログ", re.I)),
    # Restricted to actual bio pages; deliberately does NOT match 院内紹介/設備紹介/検査案内
    # (facility-tour and exam-guide pages), which must not be treated as doctor-history pages.
    ("DOCTOR_PROFILE", re.compile(r"医師紹介|院長紹介|スタッフ紹介|医師プロフィール|経歴|略歴|greeting|挨拶|doctor|staff|profile", re.I)),
    ("PUBLICATION", re.compile(r"論文|学会|学術集会|publication|research", re.I)),
)
_HOME_PATH = re.compile(r"^/(?:index\.html?)?$", re.I)

REFERRAL_ACTION_PATTERN = re.compile(
    r"(?:専門(?:の)?(?:クリニック|医療機関|施設|外来|病院)|他院|他の医療機関|連携医療機関|提携医療機関|提携病院)"
    r"[^。\n]{0,40}(?:ご紹介|紹介|ご案内|案内|受診をおすすめ|受診を勧め)"
)
# Referral to a same-group/affiliated facility, e.g. "同グループクリニックへの...紹介が可能です".
# Requires an actual referral action nearby — "当院は○○グループです" alone (a corporate
# description with no referral verb) must not match.
GROUP_REFERRAL_PATTERN = re.compile(
    r"(?:同(?:一)?グループ|グループ内|系列(?:院|クリニック|病院)?|関連(?:医院|クリニック|病院)|分院|姉妹院)"
    r"(?:の|クリニック|病院|施設)?"
    r"[^。\n]{0,40}(?:ご紹介|紹介|ご案内|受診|治療を依頼|手術を依頼|が可能|できます|していただけます|いただけます)"
)
# Publication/presentation evidence only; deliberately does NOT match bare "学会" inside a
# board-certification title like "○○学会専門医/認定医/所属" (see _CREDENTIAL_PATTERN guard).
PUBLICATION_PATTERN = re.compile(
    r"論文|著書|演題|研究業績|業績一覧|publication|paper|conference\s*presentation|"
    r"学会.{0,6}(?:発表|報告|講演)|学術集会|Award|Congress|Symposium",
    re.I,
)
DOCTOR_HISTORY_PATTERN = re.compile(
    r"経歴|略歴|前職|出身|勤務歴|担当していました|執刀してきました|"
    r"経験させていただきました|研修医時代|以前勤務|で勤務していました"
)
HEDGE_PATTERN = re.compile(
    r"検討しています|導入予定|将来的に|他院で行っています|紹介しています|一般的には|という治療があります"
)
SUSPENDED_PATTERN = re.compile(r"休診中|休止中|受付を停止|現在お休み")

_SELF_REFERENCE_PREFIXES = ("当院", "当クリニック", "当医院", "当科", "こちら")
_FACILITY_SUFFIX = r"(?:眼科|皮フ科|皮膚科|クリニック|医院|病院|レディースクリニック|センター)"
_FACILITY_NAME_PATTERN = re.compile(
    rf"([一-龥ぁ-んァ-ヶー0-9A-Za-z]{{2,20}}{_FACILITY_SUFFIX})\s*(?:にて|で|において)"
)
_TREATMENT_VERB_NEARBY = re.compile(r"行|実施|手術|治療|検査")

# Named-facility referral: "○○クリニックへの紹介をさせていただきます" style — a proper-noun
# facility name (not a generic keyword like 他院/専門クリニック) followed by a referral verb.
# No specific facility name is hard-coded; any name matching the suffix list is a candidate.
_FACILITY_REFERRAL_PATTERN = re.compile(rf"([一-龥ぁ-んァ-ヶー0-9A-Za-z]{{2,20}}{_FACILITY_SUFFIX})(?:へ|に)")
_REFERRAL_VERB_NEARBY = re.compile(r"紹介|受診|お願いします|お願いしています")

# A continuative (連用形+て) self-offer verb sitting directly after the alias, e.g. the
# "検査を行い、" in "...胃カメラ検査を行い、さらに詳しい検査が必要な場合は...ご紹介します".
# Signals the alias itself was just affirmed as performed, before the sentence moves on
# to a separate, escalated clause.
_SELF_OFFER_CONTINUATIVE_PATTERN = re.compile(
    r"^(?:検査|治療|手術|施術)?を?(?:行い|行って|実施し|実施して|提供し|提供して|施術し|検査し)"
)
# Connective marking a shift to a conditional/escalated follow-up action ("if more is
# needed, then..."), as opposed to a direct continuation about the same alias.
_ESCALATION_MARKER_PATTERN = re.compile(
    r"さらに|それでも|なお|もし|場合は|必要に応じて|より詳しい|追加で|改善しない場合|変わらない場合"
)


def _referral_targets_escalated_action(sentence: str, alias: str, match: "re.Match[str]") -> bool:
    """True when a REFERRAL/GROUP_REFERRAL trigger match targets a different, escalated
    follow-up action introduced after the alias — not the alias's own provision.

    Requires BOTH (a) a continuative self-offer verb immediately after the alias (proof
    the clinic just affirmed doing the alias itself) AND (b) an escalation/conditional
    connective between the alias and the referral trigger. Without the self-offer verb
    right after the alias, a bare "○○を必要とする場合は専門施設をご紹介しています" (where
    the alias IS the referral target) is correctly left excluded.
    """
    alias_match = re.search(re.escape(alias), sentence) if alias else None
    if not alias_match or match.start() < alias_match.end():
        return False
    right_after = sentence[alias_match.end():alias_match.end() + 15]
    if not _SELF_OFFER_CONTINUATIVE_PATTERN.match(right_after):
        return False
    between = sentence[alias_match.end():match.start()]
    return bool(_ESCALATION_MARKER_PATTERN.search(between))

_COMPOUND_SUFFIX_PATTERN = re.compile(r"^(?:前後|後|歴|前)")
PLACEHOLDER_TEXT_PATTERN = re.compile(
    r"ここに.{0,10}(?:説明文|テキスト|文章).{0,5}入力してください|"
    r"テキストを入力|ダミーテキスト|サンプルテキスト|Lorem ipsum",
    re.I,
)


def is_negative_compound_alias_mention(sentence: str, alias: str) -> bool:
    """True only when EVERY occurrence of the alias in the sentence is a non-provision
    compound, e.g. "白内障手術後の診察" (a post-procedure follow-up, not the procedure
    itself) or "手術歴" / "治療歴" (a past-history record). A single bad occurrence must
    not veto a sentence that also has a clean, separate mention of the same alias
    (common in large nav dumps that list both "○○切除" as a service and "○○切除後の
    注意事項" as patient guidance) — so this requires ALL occurrences to be compound.
    Purely structural around wherever `alias` matched — no specific treatment name is
    hard-coded. Deliberately narrower than "another clinic performs X" scenarios, which
    stay with the existing OTHER_PROVIDER_CONTEXT/mentions_other_facility handling
    (REVIEW-level ambiguity, not an outright rejection).
    """
    if not alias:
        return False
    matches = list(re.finditer(re.escape(alias), sentence))
    if not matches:
        return False
    return all(_COMPOUND_SUFFIX_PATTERN.match(sentence[m.end():m.end() + 2]) for m in matches)


def classify_page_type(url: str, title: str, heading_path: list[str]) -> str:
    """Cheap URL/title/heading keyword classification; first matching rule wins."""
    path = urlparse(str(url or "")).path or "/"
    haystack = " ".join([path, str(title or ""), " ".join(heading_path or [])])
    for page_type, pattern in PAGE_TYPE_RULES:
        if pattern.search(haystack):
            return page_type
    if _HOME_PATH.match(path):
        return "HOME"
    return "OTHER"


def _nearest_boundary_ancestor(node):
    """The closest ancestor that is a block-boundary tag (see _BLOCK_BOUNDARY_TAGS),
    or None if the text sits directly under something else (e.g. a bare div/span)."""
    parent = node.parent
    while parent is not None:
        if getattr(parent, "name", None) in _BLOCK_BOUNDARY_TAGS:
            return parent
        parent = parent.parent
    return None


def build_evidence_blocks(page) -> list[dict]:
    """Heading-scoped text blocks for one fetched page, reusing the cached soup.

    Unlike Page.main_text (which strips nav/header/footer/aside for identity checks),
    this walks the full body: treatment menus are sometimes marked up as nav/list
    structures on the clinic's own site, and dropping them would hide legitimate
    evidence (e.g. a TOP-page "施術一覧" menu).

    Text crossing a block-boundary tag (li/p/dt/dd/td/th/tr/br) gets a hard newline
    inserted, so the downstream sentence splitter (which already splits on \\n) treats
    separate list items/paragraphs/table cells as separate sentences instead of merging
    them into one run-on string. This matters because real clinic pages often lack
    Japanese sentence-ending punctuation inside nav/list markup: without this boundary,
    an unrelated item's negation/referral/suspension wording can leak onto a different,
    genuinely-offered item that just happens to sit in the same flattened text (and vice
    versa — a genuine self-offer statement can wrongly "rescue" an unrelated excluded
    item). Text within the SAME block tag (e.g. inline <span>/<a> inside one <li>) still
    joins without a break.
    """
    soup = page_soup(page)
    body = soup.body or soup
    blocks: list[dict] = []
    stack: list[tuple[int, str]] = []
    segments: list[list[str]] = [[]]  # list of segments; each segment is a list of text parts
    last_boundary = object()  # sentinel distinct from any real node/None

    def flush():
        pieces = [" ".join(part for part in seg if part.strip()) for seg in segments]
        text = sanitize_text("\n".join(piece for piece in pieces if piece.strip()))
        if text.strip():
            blocks.append({"heading_path": [h for _, h in stack], "text": text})
        segments.clear()
        segments.append([])

    def start_new_segment():
        if segments[-1]:
            segments.append([])

    for node in body.descendants:
        name = getattr(node, "name", None)
        if name in HEADING_LEVELS:
            flush()
            level = HEADING_LEVELS[name]
            heading_text = sanitize_text(node.get_text(" ", strip=True))
            stack[:] = [(lv, h) for lv, h in stack if lv < level]
            stack.append((level, heading_text))
            last_boundary = object()
        elif name in NON_CONTENT_TAGS:
            continue
        elif name == "br":
            start_new_segment()
            last_boundary = object()
        elif name is None:
            parent_name = getattr(node.parent, "name", "")
            if parent_name in NON_CONTENT_TAGS:
                continue
            boundary = _nearest_boundary_ancestor(node)
            if boundary is not last_boundary:
                start_new_segment()
                last_boundary = boundary
            segments[-1].append(str(node))
    flush()
    return blocks


def mentions_other_facility(sentence: str, clinic_name: str) -> bool:
    """True when the sentence attributes the treatment to a differently-named facility.

    Guards against the common "当院/当クリニックにて" self-reference (which matches the
    facility-suffix pattern too) by skipping candidates that start with a self-deictic
    word. Only counts as a mismatch when a treatment verb appears nearby, so incidental
    facility mentions elsewhere in a long sentence don't trigger false positives.
    """
    own = normalize_clinic_name(clinic_name)
    for match in _FACILITY_NAME_PATTERN.finditer(sentence):
        candidate = match.group(1)
        if candidate.startswith(_SELF_REFERENCE_PREFIXES):
            continue
        normalized_candidate = normalize_clinic_name(candidate)
        if not normalized_candidate:
            continue
        if own and (normalized_candidate in own or own in normalized_candidate):
            continue
        window = sentence[match.end():match.end() + 20]
        if _TREATMENT_VERB_NEARBY.search(window):
            return True
    return False


def mentions_named_facility_referral(sentence: str, clinic_name: str) -> bool:
    """True for referrals naming a specific other facility, e.g. "真鍋クリニックへの紹介
    をさせていただきます" — REFERRAL_ACTION_PATTERN only covers generic phrases (他院/専門
    クリニック/...), not proper nouns, so this catches named targets without hard-coding
    any specific clinic name.
    """
    own = normalize_clinic_name(clinic_name)
    for match in _FACILITY_REFERRAL_PATTERN.finditer(sentence):
        candidate = match.group(1)
        if candidate.startswith(_SELF_REFERENCE_PREFIXES):
            continue
        normalized_candidate = normalize_clinic_name(candidate)
        if not normalized_candidate:
            continue
        if own and (normalized_candidate in own or own in normalized_candidate):
            continue
        window = sentence[match.end():match.end() + 15]
        if _REFERRAL_VERB_NEARBY.search(window):
            return True
    return False


def detect_exclusion_context(sentence: str, *, heading_path: list[str] | None = None,
                              page_type: str = "OTHER", clinic_name: str = "",
                              is_negative: bool = False, alias: str = "") -> str:
    """Priority-ordered exclusion classification. NONE means no exclusion applies.

    page_type alone is deliberately NOT a sufficient trigger for PUBLICATION or
    DOCTOR_HISTORY: a page being titled "院長紹介" or "論文" doesn't mean every sentence
    on it is career/publication content — the sentence itself must show that language.
    """
    if is_negative:
        return "NOT_OFFERED"
    if is_negative_compound_alias_mention(sentence, alias):
        return "NEGATIVE_COMPOUND"
    # A section heading itself signaling suspension (e.g. an h3 "休診中のお知らせ" whose
    # list items don't repeat that wording) applies to everything filed under it, the same
    # section-wide exception used for NOT_OFFERED headings (see the taxonomy caller).
    if SUSPENDED_PATTERN.search(sentence) or SUSPENDED_PATTERN.search(" > ".join(heading_path or [])):
        return "CURRENTLY_SUSPENDED"
    referral_match = REFERRAL_ACTION_PATTERN.search(sentence)
    if referral_match and not _referral_targets_escalated_action(sentence, alias, referral_match):
        return "REFERRAL"
    group_referral_match = GROUP_REFERRAL_PATTERN.search(sentence)
    if group_referral_match and not _referral_targets_escalated_action(sentence, alias, group_referral_match):
        return "REFERRAL"
    if mentions_named_facility_referral(sentence, clinic_name):
        return "REFERRAL"
    if mentions_other_facility(sentence, clinic_name):
        return "OTHER_CLINIC"
    if PUBLICATION_PATTERN.search(sentence):
        return "PUBLICATION"
    if DOCTOR_HISTORY_PATTERN.search(sentence):
        return "DOCTOR_HISTORY"
    return "NONE"


def detect_provider_context(sentence: str, exclusion_context: str, offer_context_pattern) -> str:
    if exclusion_context in {"NOT_OFFERED", "CURRENTLY_SUSPENDED", "NEGATIVE_COMPOUND"}:
        return "NOT_PROVIDED"
    if HEDGE_PATTERN.search(sentence):
        return "POSSIBLY_PROVIDED"
    if offer_context_pattern.search(sentence):
        return "PROVIDED"
    return "POSSIBLY_PROVIDED"
