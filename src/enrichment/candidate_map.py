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
from src.enrichment.alias_rescue import global_alias_hits
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
    return "\n".join(
        block.get("text", "")
        for page in evidence or ()
        for block in page.get("blocks", ())
    )


def candidate_union(departments, evidence, department_map=None, alias_index=None):
    """candidate_union = department_candidates ∪ global_alias_hits.

    The only categories passed to the heavy v4 evidence engine. Guarantees full
    ACTIVE-taxonomy recall even when department/Navi mapping is unavailable, since
    global_alias_hits scans all ACTIVE43 aliases independently of department.
    """
    text = evidence if isinstance(evidence, str) else evidence_text(evidence)
    return department_candidates(departments, department_map) | global_alias_hits(text, alias_index)
