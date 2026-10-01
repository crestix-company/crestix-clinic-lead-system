"""Global Alias Rescue (Phase 7 Full Run optimization, Step 3).

A cheap keyword scan across ALL ACTIVE treatment-category aliases, independent of
department. This is what guarantees a clinic is never skipped purely because
department/Navi classification data is unavailable for it: candidate_map.py's
department pre-filter can legitimately return an empty set, but this module always
checks every ACTIVE43 alias against the fetched HP text regardless.

Still only a pre-filter. A hit here only adds a category to candidate_union(); the
actual CONFIRMED/REVIEW/NOT_CONFIRMED decision is made exclusively by the frozen v4
evidence engine (evaluate_treatment_evidence in treatment_taxonomy.py), unchanged.
"""
from src.enrichment.hp_analysis import keyword_match
from src.enrichment.treatment_taxonomy import phase7b_research_categories


def build_active_alias_index():
    """{category_name: (alias, ...)} for all ACTIVE treatment categories."""
    return {name: tuple(item.get("aliases", ())) for name, item in phase7b_research_categories().items()}


def global_alias_hits(text, alias_index=None):
    """Category names with at least one alias keyword hit in `text`.

    Cheap regex/keyword scan only -- no HTTP requests, no context/negation rules.
    Those live in the v4 evidence engine, which runs only on the resulting
    candidate_union, never on this function's output directly.
    """
    if not text:
        return set()
    alias_index = alias_index if alias_index is not None else build_active_alias_index()
    hits: set[str] = set()
    for name, aliases in alias_index.items():
        if any(keyword_match(alias, text) for alias in aliases):
            hits.add(name)
    return hits
