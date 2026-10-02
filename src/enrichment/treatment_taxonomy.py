"""Phase 7 treatment taxonomy and conservative evidence decisions."""
from functools import lru_cache
import re
from urllib.parse import urlparse

from src.enrichment.hp_analysis import is_official_candidate, keyword_match
from src.enrichment.treatment_context import (
    HEDGE_PATTERN,
    PLACEHOLDER_TEXT_PATTERN,
    classify_page_type,
    detect_exclusion_context,
    detect_provider_context,
)
from src.utils.config import ROOT, read_config

TAXONOMY_PATH = ROOT / "config/treatment_taxonomy.yml"
STATUSES = frozenset({"CONFIRMED", "REVIEW", "NOT_CONFIRMED"})
RULE_VERSION = "7A-v3"
LEGACY_RULE_VERSION = "7A-v1"
EVIDENCE_ENGINE_VERSION = "phase7b-context-v4"
VALID_ITEM_TYPES = frozenset({"DEPARTMENT", "DISEASE", "EXAM", "TREATMENT", "PROCEDURE", "SURGERY", "OTHER"})
VALID_TAXONOMY_STATUSES = frozenset({"ACTIVE", "PROPOSED", "DEPRECATED"})

NEGATIVE_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r"(?:当院|当クリニック|当医院|当院では|当院にて)[^。\n]{0,60}(?:行っていません|行っておりません|実施していません|実施しておりません|対応していません|対応しておりません|提供していません|提供しておりません|取り扱っていません|施術していません|検査していません)",
    r"(?:他院|他の医療機関|紹介先|連携医療機関)[^。\n]{0,50}(?:紹介|受診|ご案内)",
    r"(?:行っていません|行っておりません|実施していません|実施しておりません|対応していません|対応しておりません|提供していません|提供しておりません|取り扱っていません|施術していません|検査していません)",
    # Noun-phrase / capability negation — distinct grammar from the verb-negation forms
    # above (て(い)ません), e.g. a heading/list style "Treatment名 → 実施困難なこと" or
    # "お受けすることができません". Not tied to a "当院では" prefix since these commonly
    # appear as a bare list item after DOM-boundary segmentation splits it from context.
    r"実施困難|対応困難|対応が難しい|当院では難しい|当院では対応できない|"
    r"実施できません|お受けできません|お受けすることができません|取り扱いなし",
))
GENERAL_CONTEXT = re.compile(
    r"一般的に|とは[、。]|原因は|症状として|ガイドライン|医学的に|治療法には|治療方法として|"
    r"近年は|近年、|広く利用されています|広く行われています|有効な治療方法です|有効な方法です"
)
OFFER_CONTEXT = re.compile(
    r"当院|当クリニック|当医院|当院では|当院にて|当院で|当科|当院の診療|"
    r"実施しています|実施しております|行っています|行っております|行なっています|行なっております|"
    r"対応しています|対応しております|提供しています|提供しております|"
    r"施術しています|施術しております|検査しています|検査しております|"
    r"診療しています|診療しております|"
    r"手術を行います|術を行います|検査を行います|治療を行います|"
    r"手術を行い|術を行い|検査を行い|治療を行い|施術を行い|"
    r"ご相談ください|予約を受け付け|"
    r"導入しています|導入しました|開始しました|開始しています|取り扱っています|取り扱いを開始|"
    r"を受けられます|を受けていただけます|が可能です"
)
OTHER_PROVIDER_CONTEXT = re.compile(r"他院|他の医療機関|紹介先|連携医療機関|別の医院|別の病院")
ARTICLE_CONTEXT = re.compile(r"blog|column|news|topics?|article|notice|information", re.I)


@lru_cache(maxsize=1)
def load_taxonomy():
    config = read_config(TAXONOMY_PATH)
    if config.get("taxonomy_version") != RULE_VERSION:
        raise ValueError("treatment taxonomy version is invalid")
    categories = config.get("treatment_categories")
    legacy_categories = config.get("legacy_treatment_categories_v1")
    sales = config.get("crestix_sales_categories")
    if not isinstance(categories, dict) or not isinstance(legacy_categories, dict) or not isinstance(sales, dict):
        raise ValueError("treatment taxonomy sections are missing")
    for name, definition in categories.items():
        if (definition.get("status") not in VALID_TAXONOMY_STATUSES
                or definition.get("item_type") not in VALID_ITEM_TYPES
                or definition.get("item_type") in {"DEPARTMENT", "DISEASE", "OTHER"}
                or not definition.get("aliases")
                or definition.get("sales_filter_value") not in {"HIGH", "MEDIUM", "LOW"}):
            raise ValueError(f"invalid treatment category definition: {name}")
        if definition["status"] == "ACTIVE" and definition["sales_filter_value"] == "LOW":
            raise ValueError(f"low sales-value category cannot be ACTIVE: {name}")
        if definition["status"] == "ACTIVE" and not definition.get("source_sales_items"):
            raise ValueError(f"active category needs an existing sales-item basis: {name}")
    for name, definition in legacy_categories.items():
        if definition.get("treatment_category") != name or not definition.get("aliases"):
            raise ValueError(f"invalid v1 compatibility definition: {name}")
    return config


def treatment_category_names():
    """Legacy persisted treatments_json choices; kept for backward compatibility."""
    return list(load_taxonomy()["legacy_treatment_categories_v1"])


def active_treatment_category_names():
    return [name for name, item in load_taxonomy()["treatment_categories"].items()
            if item["status"] == "ACTIVE"]


def phase7b_research_categories():
    return {name: item for name, item in load_taxonomy()["treatment_categories"].items()
            if item["status"] == "ACTIVE"}


def proposed_treatment_categories():
    return {name: item for name, item in load_taxonomy()["treatment_categories"].items()
            if item["status"] == "PROPOSED"}


def treatment_keyword_view():
    return {name: list(item["aliases"]) for name, item in load_taxonomy()["legacy_treatment_categories_v1"].items()}


def department_category_view():
    return load_taxonomy()["legacy_department_category_map_v1"]


def _sentence_chunks(text):
    return [part.strip() for part in re.split(r"[。！？!?\n]+", str(text or "")) if part.strip()]


def _is_negative(sentence):
    return any(pattern.search(sentence) for pattern in NEGATIVE_PATTERNS)


_EXCLUSION_REASON = {
    "CURRENTLY_SUSPENDED": "CURRENTLY_SUSPENDED",
    "REFERRAL": "REFERRAL_TO_OTHER_PROVIDER",
    "OTHER_CLINIC": "OTHER_FACILITY_WITHIN_GROUP",
    "PUBLICATION": "PUBLICATION_OR_ACADEMIC_ONLY",
    "DOCTOR_HISTORY": "DOCTOR_CAREER_HISTORY_ONLY",
    "NEGATIVE_COMPOUND": "NEGATIVE_COMPOUND_CONTEXT",
}


def evaluate_treatment_evidence(treatment_category, evidence_items, *, clinic_id=None, checked_at=None,
                                 clinic_name=""):
    """Evaluate supplied official-page excerpts; clinic department is intentionally not an input.

    Each item accepts url, page_title, text (or heading-scoped "blocks", see
    treatment_context.build_evidence_blocks), and source_type. Candidate keyword hits are
    evidence for REVIEW only unless the same sentence clearly attributes an offer to this
    clinic, is not undercut by a stronger exclusion context (referral, a differently-named
    facility, doctor career history, academic publications, or a suspended service), and
    isn't a bare "broad" alias standing alone. clinic_name is optional and used only to spot
    a *different* facility being credited with the treatment (never to boost confidence via
    name-based keyword inference). The function performs no HTTP requests.
    """
    taxonomy = load_taxonomy()
    if treatment_category not in taxonomy["treatment_categories"]:
        raise ValueError(f"unknown or v1-only treatment category: {treatment_category}")
    definition = taxonomy["treatment_categories"][treatment_category]
    if definition["status"] != "ACTIVE":
        raise ValueError(f"treatment category is not active for Phase 7-B: {treatment_category}")
    aliases = definition["aliases"]
    excluded_aliases = set(definition.get("exclude_terms", ()))
    broad_aliases = set(definition.get("broad_aliases", ()))
    context_required_aliases = definition.get("context_required_aliases", {})
    hits = []
    ambiguous = []
    excluded = []
    specific_alias_hit = False
    for item in evidence_items or ():
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", ""))
        if not url or not is_official_candidate(url):
            continue
        page_title = str(item.get("page_title", ""))[:200]
        blocks = item.get("blocks") or [{"heading_path": [], "text": item.get("text", "")}]
        for block in blocks:
            heading_path = block.get("heading_path") or []
            page_type = classify_page_type(url, page_title, heading_path)
            for sentence in _sentence_chunks(block.get("text", "")):
                if PLACEHOLDER_TEXT_PATTERN.search(sentence):
                    continue
                alias = next((term for term in sorted(aliases, key=len, reverse=True) if keyword_match(term, sentence)), None)
                if not alias:
                    continue
                if alias in excluded_aliases:
                    continue
                required_keywords = context_required_aliases.get(alias, ())
                context_satisfied = any(kw in sentence for kw in required_keywords) if required_keywords else True
                needs_corroboration = (alias in broad_aliases and not specific_alias_hit) or (
                    required_keywords and not context_satisfied)
                if alias not in broad_aliases and (not required_keywords or context_satisfied):
                    specific_alias_hit = True
                # A section heading that itself signals suspension/not-offered (e.g. "当院で
                # 実施困難なこと" / "対応が難しい治療") applies to every item listed under it,
                # even though the item's own text (e.g. a bare "体外受精" list entry) carries
                # no negation wording of its own — the heading IS the negation for this section.
                is_negative = _is_negative(sentence) or _is_negative(" > ".join(heading_path))
                exclusion_context = detect_exclusion_context(
                    sentence, heading_path=heading_path, page_type=page_type,
                    clinic_name=clinic_name, is_negative=is_negative, alias=alias)
                provider_context = detect_provider_context(sentence, exclusion_context, OFFER_CONTEXT)
                evidence = {
                    "clinic_id": clinic_id,
                    "treatment_category": treatment_category,
                    "status": "REVIEW",
                    "evidence_text": sentence[:500],
                    "evidence_url": url,
                    "evidence_page_title": page_title,
                    "evidence_source_type": str(item.get("source_type", "OFFICIAL_HP")),
                    "checked_at": checked_at,
                    "rule_version": RULE_VERSION,
                    "evidence_engine_version": EVIDENCE_ENGINE_VERSION,
                    "matched_alias": alias,
                    "reason": "AMBIGUOUS_CONTEXT",
                    "page_type": page_type,
                    "section_heading": " > ".join(heading_path),
                    "provider_context": provider_context,
                    "exclusion_context": exclusion_context,
                }
                if exclusion_context == "NOT_OFFERED":
                    evidence["status"] = "NOT_CONFIRMED"
                    evidence["reason"] = "EXPLICIT_NEGATIVE_OR_REFERRAL"
                    excluded.append(evidence)
                elif exclusion_context in _EXCLUSION_REASON:
                    evidence["status"] = "NOT_CONFIRMED"
                    evidence["reason"] = _EXCLUSION_REASON[exclusion_context]
                    excluded.append(evidence)
                elif OTHER_PROVIDER_CONTEXT.search(sentence):
                    evidence["reason"] = "OTHER_PROVIDER_CONTEXT"
                    ambiguous.append(evidence)
                elif ARTICLE_CONTEXT.search(urlparse(url).path) or re.search(r"ブログ|コラム|お知らせ|ニュース", page_title) or page_type in {"NEWS", "BLOG"}:
                    evidence["reason"] = "ARTICLE_OR_ARCHIVE_CONTEXT"
                    ambiguous.append(evidence)
                elif GENERAL_CONTEXT.search(sentence):
                    evidence["reason"] = "GENERAL_INFORMATION_CONTEXT"
                    ambiguous.append(evidence)
                elif needs_corroboration:
                    evidence["reason"] = "BROAD_ALIAS_WITHOUT_SPECIFIC_CORROBORATION"
                    ambiguous.append(evidence)
                elif HEDGE_PATTERN.search(sentence):
                    evidence["reason"] = "FUTURE_OR_HEDGED_OFFER"
                    ambiguous.append(evidence)
                elif OFFER_CONTEXT.search(sentence):
                    evidence["status"] = "CONFIRMED"
                    evidence["reason"] = "OFFICIAL_CLINIC_OFFER_STATEMENT"
                    hits.append(evidence)
                else:
                    ambiguous.append(evidence)
    if hits:
        return hits[0]
    if ambiguous:
        return ambiguous[0]
    if excluded:
        return excluded[0]
    return {
        "clinic_id": clinic_id,
        "treatment_category": treatment_category,
        "status": "NOT_CONFIRMED",
        "evidence_text": "",
        "evidence_url": "",
        "evidence_page_title": "",
        "evidence_source_type": "OFFICIAL_HP",
        "checked_at": checked_at,
        "rule_version": RULE_VERSION,
        "evidence_engine_version": EVIDENCE_ENGINE_VERSION,
        "matched_alias": "",
        "reason": "NO_QUALIFYING_OFFICIAL_HP_EVIDENCE",
        "page_type": "",
        "section_heading": "",
        "provider_context": "UNKNOWN",
        "exclusion_context": "NONE",
    }


TREATMENT_EVIDENCE_SCHEMA_SQL = """
CREATE TABLE clinic_treatment_evidence (
    clinic_id TEXT NOT NULL,
    treatment_category TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('CONFIRMED', 'REVIEW', 'NOT_CONFIRMED')),
    evidence_text TEXT NOT NULL DEFAULT '',
    evidence_url TEXT NOT NULL DEFAULT '',
    evidence_page_title TEXT NOT NULL DEFAULT '',
    evidence_source_type TEXT NOT NULL DEFAULT 'OFFICIAL_HP',
    checked_at TEXT,
    rule_version TEXT NOT NULL,
    PRIMARY KEY (clinic_id, treatment_category),
    CHECK (status != 'CONFIRMED' OR (evidence_text != '' AND evidence_url != ''))
)
""".strip()


CLINICAL_FOCUS_SCHEMA_SQL = """
CREATE TABLE clinic_clinical_focus (
    clinic_id TEXT NOT NULL,
    clinical_focus TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('MHLW_DEPARTMENT', 'OFFICIAL_HP')),
    source_value TEXT NOT NULL DEFAULT '',
    evidence_url TEXT NOT NULL DEFAULT '',
    evidence_page_title TEXT NOT NULL DEFAULT '',
    checked_at TEXT,
    rule_version TEXT NOT NULL,
    PRIMARY KEY (clinic_id, clinical_focus, source_type, source_value)
)
""".strip()
