"""Phase 7 treatment taxonomy and conservative evidence decisions."""
from functools import lru_cache
import re
from urllib.parse import urlparse

from src.enrichment.hp_analysis import is_official_candidate, keyword_match
from src.utils.config import ROOT, read_config

TAXONOMY_PATH = ROOT / "config/treatment_taxonomy.yml"
STATUSES = frozenset({"CONFIRMED", "REVIEW", "NOT_CONFIRMED"})
RULE_VERSION = "7A-v2"
LEGACY_RULE_VERSION = "7A-v1"
VALID_ITEM_TYPES = frozenset({"DEPARTMENT", "DISEASE", "EXAM", "TREATMENT", "PROCEDURE", "SURGERY", "OTHER"})
VALID_TAXONOMY_STATUSES = frozenset({"ACTIVE", "PROPOSED", "DEPRECATED"})

NEGATIVE_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r"(?:当院|当クリニック|当医院|当院では|当院にて)[^。\n]{0,60}(?:行っていません|行っておりません|実施していません|実施しておりません|対応していません|対応しておりません|提供していません|取り扱っていません|施術していません|検査していません)",
    r"(?:他院|他の医療機関|紹介先|連携医療機関)[^。\n]{0,50}(?:紹介|受診|ご案内)",
    r"(?:行っていません|行っておりません|実施していません|実施しておりません|対応していません|対応しておりません|提供していません|取り扱っていません|施術していません|検査していません)",
))
GENERAL_CONTEXT = re.compile(r"一般的に|とは[、。]|原因は|症状として|ガイドライン|医学的に|治療法には|治療方法として")
OFFER_CONTEXT = re.compile(r"当院|当クリニック|当医院|当院では|当院にて|当院で|当科|当院の診療|実施しています|行っています|対応しています|提供しています|施術しています|検査しています|手術を行います|診療しています|ご相談ください|予約を受け付け")
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


def evaluate_treatment_evidence(treatment_category, evidence_items, *, clinic_id=None, checked_at=None):
    """Evaluate supplied official-page excerpts; clinic departments/name are intentionally not inputs.

    Each item accepts url, page_title, text and source_type. Candidate keyword hits are
    evidence for REVIEW only unless the same sentence clearly attributes an offer to
    this clinic. The function performs no HTTP requests.
    """
    taxonomy = load_taxonomy()
    if treatment_category not in taxonomy["treatment_categories"]:
        raise ValueError(f"unknown or v1-only treatment category: {treatment_category}")
    definition = taxonomy["treatment_categories"][treatment_category]
    if definition["status"] != "ACTIVE":
        raise ValueError(f"treatment category is not active for Phase 7-B: {treatment_category}")
    aliases = definition["aliases"]
    excluded_aliases = set(definition.get("exclude_terms", ()))
    hits = []
    ambiguous = []
    for item in evidence_items or ():
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", ""))
        if not url or not is_official_candidate(url):
            continue
        for sentence in _sentence_chunks(item.get("text", "")):
            alias = next((term for term in sorted(aliases, key=len, reverse=True) if keyword_match(term, sentence)), None)
            if not alias:
                continue
            if alias in excluded_aliases:
                continue
            evidence = {
                "clinic_id": clinic_id,
                "treatment_category": treatment_category,
                "status": "REVIEW",
                "evidence_text": sentence[:500],
                "evidence_url": url,
                "evidence_page_title": str(item.get("page_title", ""))[:200],
                "evidence_source_type": str(item.get("source_type", "OFFICIAL_HP")),
                "checked_at": checked_at,
                "rule_version": RULE_VERSION,
                "matched_alias": alias,
                "reason": "AMBIGUOUS_CONTEXT",
            }
            if _is_negative(sentence):
                evidence["status"] = "NOT_CONFIRMED"
                evidence["reason"] = "EXPLICIT_NEGATIVE_OR_REFERRAL"
                ambiguous.append(evidence)
            elif OTHER_PROVIDER_CONTEXT.search(sentence):
                evidence["reason"] = "OTHER_PROVIDER_CONTEXT"
                ambiguous.append(evidence)
            elif ARTICLE_CONTEXT.search(urlparse(url).path) or re.search(r"ブログ|コラム|お知らせ|ニュース", evidence["evidence_page_title"]):
                evidence["reason"] = "ARTICLE_OR_ARCHIVE_CONTEXT"
                ambiguous.append(evidence)
            elif GENERAL_CONTEXT.search(sentence):
                evidence["reason"] = "GENERAL_INFORMATION_CONTEXT"
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
        "matched_alias": "",
        "reason": "NO_QUALIFYING_OFFICIAL_HP_EVIDENCE",
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
