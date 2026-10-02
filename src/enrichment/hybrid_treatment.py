"""HYBRID Treatment Research v1.

Combines legacy structural signals with the Phase 7 evidence safety layer.  This
module is deliberately side-effect free and does not know about the production
sidecar schema.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from urllib.parse import urljoin, urlparse

from src.enrichment.candidate_map import candidate_union, department_candidates
from src.enrichment.hp_analysis import host, is_official_candidate, keyword_match, page_soup
from src.enrichment.treatment_context import build_evidence_blocks
from src.enrichment.treatment_taxonomy import (
    EVIDENCE_ENGINE_VERSION,
    RULE_VERSION,
    evaluate_treatment_evidence,
    load_taxonomy,
    phase7b_research_categories,
)

HYBRID_RULE_VERSION = "hybrid-treatment-v1"
HYBRID_STATUSES = frozenset({"CONFIRMED", "MENTIONED", "REVIEW", "NOT_CONFIRMED", "CANDIDATE_ONLY"})
SIGNAL_SOURCES = frozenset({"CLINIC_NAME", "HOME_MENU", "INTRO_MENU", "DEDICATED_PAGE", "OFFICIAL_HP_TEXT", "DEPARTMENT_CANDIDATE"})
SIGNAL_RANKS = frozenset({"S", "A", "B", "C", "D", "X"})

_ARTICLE_PATH = re.compile(r"/(?:blog|column|news|topics?|article|information|notice)(?:/|$)", re.I)
_INTRO_PATH = re.compile(r"/(?:about(?:[-_]?us)?|clinic|medical|service|treatment|guide|shinryo|department)(?:/|$)", re.I)
_INTRO_HEADING = re.compile(r"医院紹介|クリニック紹介|当院について|診療案内|診療内容|治療内容|専門外来|medical|service|treatment|department|guide", re.I)
_EXCLUDED_LABEL = re.compile(r"採用|求人|学会|論文|終了|休止|中止|行っていません|行っておりません|実施していません|ブログ|コラム|ニュース|お知らせ")
_GENERIC_PAGE = re.compile(r"アクセス|院長紹介|医師紹介|スタッフ|料金|費用|問い合わせ|採用|ブログ|ニュース|サイトマップ")
_STATUS_PRIORITY = {"CONFIRMED": 5, "MENTIONED": 4, "REVIEW": 3, "NOT_CONFIRMED": 2, "CANDIDATE_ONLY": 1}
_RANK_PRIORITY = {"S": 5, "A": 4, "B": 3, "C": 2, "D": 1, "X": 0}


@dataclass(frozen=True)
class HybridTreatmentResult:
    clinic_id: int | str | None
    treatment_category: str
    hybrid_status: str
    signal_source: str
    signal_rank: str
    matched_alias: str = ""
    evidence_text: str = ""
    evidence_url: str = ""
    page_title: str = ""
    provider_context: str = "UNKNOWN"
    exclusion_context: str = "NONE"
    department_context: str = ""
    confidence: float = 0.0
    hybrid_rule_version: str = HYBRID_RULE_VERSION
    taxonomy_version: str = RULE_VERSION
    evidence_engine_version: str = EVIDENCE_ENGINE_VERSION
    checked_at: str | None = None

    def __post_init__(self):
        if self.hybrid_status not in HYBRID_STATUSES or self.signal_source not in SIGNAL_SOURCES or self.signal_rank not in SIGNAL_RANKS:
            raise ValueError("invalid HYBRID Treatment result")

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class ClinicalFocusResult:
    clinic_id: int | str | None
    clinical_focus: str
    source_type: str
    source_value: str
    evidence_url: str = ""
    confidence: float = 0.0
    rule_version: str = HYBRID_RULE_VERSION


def project_hybrid_status(status: str) -> str:
    """Lossless-enough projection into the unchanged production three-status schema."""
    try:
        return {"CONFIRMED": "CONFIRMED", "MENTIONED": "REVIEW", "REVIEW": "REVIEW",
                "NOT_CONFIRMED": "NOT_CONFIRMED", "CANDIDATE_ONLY": "NOT_CONFIRMED"}[status]
    except KeyError as exc:
        raise ValueError(f"unknown hybrid status: {status}") from exc


def _terms(definition):
    return sorted(set(definition.get("aliases", ())), key=len, reverse=True)


def _matched_alias(category, text):
    definition = phase7b_research_categories()[category]
    if keyword_match(category, text):
        return category
    return next((term for term in _terms(definition) if keyword_match(term, text)), "")


def _safe_alias_categories(text, departments=()):
    evidence = [{"url": "https://signal.invalid/", "blocks": [{"text": text}]}]
    guarded = candidate_union(departments, evidence)
    return {category for category in guarded if _matched_alias(category, text)}


def _page_evidence(page, source_type="OFFICIAL_HP"):
    return {"url": page.url, "page_title": page.title, "blocks": build_evidence_blocks(page), "source_type": source_type}


def _structural_context_ok(category, alias, text, departments):
    required = phase7b_research_categories()[category].get("cross_department_guard", {}).get(
        "context_required_aliases", {}
    ).get(alias, ())
    return not required or any(keyword in f"{text} {' '.join(departments)}" for keyword in required)


def _intro_page(page, index):
    if re.fullmatch(r"/(?:index\.html?)?", urlparse(page.url).path or "/", re.I):
        return True
    path = urlparse(page.url).path
    heading = " ".join(x.get_text(" ", strip=True) for x in page_soup(page).select("h1,h2")[:2])
    return bool(_INTRO_PATH.search(path) or _INTRO_HEADING.search(heading))


def _structural_result(category, source, rank, text, page, clinic_id, departments, checked_at, identity_verified=True):
    safety = evaluate_treatment_evidence(category, [_page_evidence(page, source)], clinic_id=clinic_id, checked_at=checked_at)
    excluded = safety["status"] == "NOT_CONFIRMED" and safety["exclusion_context"] != "NONE"
    alias = _matched_alias(category, text)
    context_ok = _structural_context_ok(category, alias, text, departments)
    confirmed = context_ok and identity_verified
    return HybridTreatmentResult(
        clinic_id, category, "NOT_CONFIRMED" if excluded else ("CONFIRMED" if confirmed else "REVIEW"), source,
        "X" if excluded or not identity_verified else (rank if context_ok else "C"), alias, text[:500], page.url,
        page.title[:200], safety.get("provider_context", "UNKNOWN"), safety.get("exclusion_context", "NONE"),
        " / ".join(departments), 0.0 if excluded else ((0.99 if rank == "S" else 0.97) if confirmed else .25), checked_at=checked_at,
    )


def evaluate_hybrid_treatments(*, clinic_id=None, clinic_name="", departments=(), pages=(), checked_at=None,
                               identity_verified=True, include_supporting_signals=False):
    """Return Treatment results and Clinical Focus results without I/O or persistence."""
    pages = [p for p in pages if is_official_candidate(p.url)]
    departments = tuple(dict.fromkeys(departments or ()))
    results = []

    # Concrete clinic-name signals only. Broad/context-required aliases stay out.
    for category, definition in phase7b_research_categories().items():
        alias = _matched_alias(category, clinic_name)
        if not alias or alias in definition.get("broad_aliases", ()) or alias in definition.get("context_required_aliases", {}):
            continue
        if category not in _safe_alias_categories(clinic_name):
            continue
        results.append(HybridTreatmentResult(
            clinic_id, category, "CONFIRMED", "CLINIC_NAME", "S", alias, clinic_name[:500],
            "", clinic_name[:200], "PROVIDED", "NONE", " / ".join(departments), .99,
            checked_at=checked_at,
        ))

    linked = {}
    for index, page in enumerate(pages):
        if not _intro_page(page, index):
            continue
        for anchor in page_soup(page).select("a[href]"):
            if anchor.find_parent("footer") is not None:
                continue
            label = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
            if not label or len(label) > 120 or _EXCLUDED_LABEL.search(label):
                continue
            href = urljoin(page.url, anchor.get("href", ""))
            if host(href) != host(page.url) or _ARTICLE_PATH.search(urlparse(href).path):
                continue
            source = "HOME_MENU" if re.fullmatch(r"/(?:index\.html?)?", urlparse(page.url).path or "/", re.I) else "INTRO_MENU"
            for category in _safe_alias_categories(label, departments):
                results.append(_structural_result(category, source, "S", label, page, clinic_id, departments, checked_at, identity_verified))
                linked.setdefault(href.rstrip("/"), set()).add(category)

    for page in pages:
        categories = linked.get(page.url.rstrip("/"), set())
        if not categories or _ARTICLE_PATH.search(urlparse(page.url).path):
            continue
        soup = page_soup(page)
        heading = " ".join(x.get_text(" ", strip=True) for x in soup.select("h1,h2")[:2]).strip()
        if not heading or _GENERIC_PAGE.search(heading):
            continue
        for category in categories:
            if _matched_alias(category, heading):
                results.append(_structural_result(category, "DEDICATED_PAGE", "A", heading, page, clinic_id, departments, checked_at, identity_verified))

    evidence_items = [_page_evidence(page) for page in pages]
    for category in candidate_union(departments, evidence_items):
        evidence = evaluate_treatment_evidence(category, evidence_items, clinic_id=clinic_id, checked_at=checked_at, clinic_name=clinic_name)
        if not evidence.get("matched_alias") and category in department_candidates(departments):
            continue
        if evidence["status"] == "CONFIRMED": status, rank, confidence = "CONFIRMED", "B", .95
        elif evidence["status"] == "NOT_CONFIRMED": status, rank, confidence = "NOT_CONFIRMED", "X", 0.0
        elif evidence.get("reason") == "OTHER_PROVIDER_CONTEXT": status, rank, confidence = "NOT_CONFIRMED", "X", 0.0
        elif evidence.get("reason") == "AMBIGUOUS_CONTEXT" and evidence.get("matched_alias"):
            status, rank, confidence = "MENTIONED", "C", .75
        else:
            status, rank, confidence = "REVIEW", "C", .5
        if not identity_verified and status in {"CONFIRMED", "MENTIONED"}:
            status, rank, confidence = "REVIEW", "X", .25
        results.append(HybridTreatmentResult(
            clinic_id, category, status, "OFFICIAL_HP_TEXT", rank, evidence.get("matched_alias", ""),
            evidence.get("evidence_text", ""), evidence.get("evidence_url", ""), evidence.get("evidence_page_title", ""),
            evidence.get("provider_context", "UNKNOWN"), evidence.get("exclusion_context", "NONE"),
            " / ".join(departments), confidence, checked_at=checked_at,
        ))

    present = {r.treatment_category for r in results}
    for category in department_candidates(departments) - present:
        results.append(HybridTreatmentResult(
            clinic_id, category, "CANDIDATE_ONLY", "DEPARTMENT_CANDIDATE", "D",
            department_context=" / ".join(departments), confidence=.1, checked_at=checked_at,
        ))

    best = {}
    for result in results:
        key = result.treatment_category
        score = (_STATUS_PRIORITY[result.hybrid_status], _RANK_PRIORITY[result.signal_rank], result.confidence)
        if key not in best or score > best[key][0]:
            best[key] = (score, result)
    selected = results if include_supporting_signals else [best[name][1] for name in sorted(best)]
    return selected, clinical_focus_signals(clinic_id, clinic_name, departments)


def clinical_focus_signals(clinic_id=None, clinic_name="", departments=()):
    """Separate disease/department focus axis; never produces Treatment status."""
    config = load_taxonomy().get("clinical_focus", {})
    found = {}
    for focus, definition in config.items():
        aliases = definition.get("aliases", ())
        alias = next((a for a in aliases if keyword_match(a, clinic_name)), "")
        if alias:
            found[focus] = ClinicalFocusResult(clinic_id, focus, "CLINIC_NAME", alias, confidence=.95)
        for department in departments or ():
            if any(keyword_match(a, department) for a in aliases) or keyword_match(focus, department):
                found.setdefault(focus, ClinicalFocusResult(clinic_id, focus, "MHLW_DEPARTMENT", department, confidence=.9))
    # Departmental focuses not represented by the legacy disease-focus dictionary.
    for department in departments or ():
        if department and department not in found and any(x in department for x in ("眼科", "消化器", "循環器", "美容皮膚", "血管外科")):
            found.setdefault(department, ClinicalFocusResult(clinic_id, department, "MHLW_DEPARTMENT", department, confidence=.85))
    return [found[name] for name in sorted(found)]
