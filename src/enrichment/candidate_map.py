"""Treatment Candidate Map (Phase 7 Full Run optimization, Step 2).

Department -> candidate Treatment category pre-filter. This is a speed optimization
only: it narrows which ACTIVE treatment categories the heavy v4 evidence engine
(evaluate_treatment_evidence) evaluates for a given clinic. It must never itself
decide CONFIRMED/REVIEW/NOT_CONFIRMED, and an empty result must never be read as
"nothing to research" -- see candidate_union(), which always unions in the global
alias rescue scan (src/enrichment/alias_rescue.py) so a clinic with no department
signal still gets full ACTIVE-taxonomy alias coverage.

config/treatment_taxonomy.yml (ACTIVE treatment_categories' crestix_departments) is
the Source of Truth; this module does not duplicate or hardcode the mapping.
"""
import re

from src.enrichment.alias_rescue import global_alias_hits
from src.enrichment.hp_analysis import keyword_match
from src.enrichment.treatment_taxonomy import phase7b_research_categories


def build_department_candidate_map():
    """{department: {category_name, ...}} for all ACTIVE treatment categories."""
    mapping: dict[str, set[str]] = {}
    for name, item in phase7b_research_categories().items():
        for dept in item.get("crestix_departments", []):
            mapping.setdefault(dept, set()).add(name)
    return mapping


def department_candidates(departments, department_map=None):
    """Candidate category names for the given department strings.

    Pre-filter only. An empty return means "no department signal for this clinic",
    not "no treatments to research" -- callers must still consult global_alias_hits.
    """
    department_map = department_map if department_map is not None else build_department_candidate_map()
    candidates: set[str] = set()
    for dept in departments or ():
        candidates.update(department_map.get(dept, ()))
    return candidates


def evidence_text(evidence):
    """Flatten the same evidence-block structure evaluate_treatment_evidence consumes
    ([{"blocks": [{"text": ...}, ...]}, ...]) into one string for the cheap alias scan."""
    if isinstance(evidence, str):
        return evidence
    return "\n".join(
        block.get("text", "")
        for page in evidence or ()
        for block in page.get("blocks", ())
    )


def _evidence_sentences(evidence):
    """Yield sentence-sized chunks so broad aliases cannot borrow context from an
    unrelated section elsewhere on the page."""
    if isinstance(evidence, str):
        texts = [evidence]
    else:
        texts = [
            block.get("text", "")
            for page in evidence or ()
            for block in page.get("blocks", ())
        ]
    for text in texts:
        for sentence in re.split(r"[。！？!?\\n]+", str(text or "")):
            sentence = sentence.strip()
            if sentence:
                yield sentence


def _alias_has_required_context(alias, required_keywords, sentences):
    """True only when alias + one of its required context terms occur together."""
    for sentence in sentences:
        if keyword_match(alias, sentence) and any(kw in sentence for kw in required_keywords):
            return True
    return False


def _guarded_alias_hits(departments, evidence, department_map=None, alias_index=None):
    """Alias rescue with a department-context false-positive guard.

    Department is *never* evidence that a treatment is provided. It is used only
    to decide whether an otherwise-broad alias is safe to rescue as a candidate.

    Rules:
      * department-matched categories retain the existing rescue behavior;
      * an exact canonical treatment name is always a valid cross-department rescue;
      * a specific alias is a valid cross-department rescue;
      * only aliases explicitly listed in cross_department_guard must carry one
        of their required context terms in the same sentence when the department
        does not match. Evidence-engine context rules are intentionally separate.

    This preserves legitimate cross-specialty offers (e.g. a dermatology clinic
    explicitly advertising ED treatment) while suppressing cases such as
    ophthalmology "レーザー治療" -> 下肢静脈瘤血管内治療.
    """
    text = evidence_text(evidence)
    raw_hits = global_alias_hits(text, alias_index)
    if not raw_hits and not text:
        return set()

    dept_hits = department_candidates(departments, department_map)
    sentences = list(_evidence_sentences(evidence))
    active = phase7b_research_categories()
    guarded = set()

    for category in raw_hits | {
        name for name in active
        if any(keyword_match(name, sentence) for sentence in sentences)
    }:
        if category in dept_hits:
            guarded.add(category)
            continue

        definition = active[category]

        # Canonical treatment names are deliberately stronger than broad aliases.
        if any(keyword_match(category, sentence) for sentence in sentences):
            guarded.add(category)
            continue

        context_required = definition.get("cross_department_guard", {}).get(
            "context_required_aliases", {}
        )
        matched_aliases = [
            alias for alias in definition.get("aliases", ())
            if any(keyword_match(alias, sentence) for sentence in sentences)
        ]

        for alias in matched_aliases:
            required_keywords = tuple(context_required.get(alias, ()))
            if required_keywords:
                if _alias_has_required_context(alias, required_keywords, sentences):
                    guarded.add(category)
                    break
                continue

            # No context requirement means this alias is specific enough to rescue
            # even outside the mapped department.
            guarded.add(category)
            break

    return guarded


def candidate_union(departments, evidence, department_map=None, alias_index=None):
    """candidate_union = department_candidates ∪ guarded_alias_hits.

    The only categories passed to the heavy evidence engine. Department context
    remains a candidate pre-filter only and never implies treatment delivery.
    Cross-department alias rescue remains supported for specific treatment names,
    while broad aliases require local treatment-specific context.
    """
    dept_hits = department_candidates(departments, department_map)
    return dept_hits | _guarded_alias_hits(
        departments, evidence, department_map=department_map, alias_index=alias_index
    )
