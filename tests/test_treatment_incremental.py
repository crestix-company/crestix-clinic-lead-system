from datetime import datetime, timedelta, timezone

from src.enrichment.treatment_taxonomy import EVIDENCE_ENGINE_VERSION, RULE_VERSION
from src.master.treatment_incremental import confidence_route, needs_reanalysis


def test_new_hp_requires_incremental_analysis():
    assert needs_reanalysis(hp_researched_at="2026-10-14T00:00:00+00:00")


def test_unchanged_hp_and_versions_do_not_duplicate_work():
    stamp = "2026-10-14T00:00:00+00:00"
    assert not needs_reanalysis(
        hp_researched_at=stamp, treatment_researched_at=stamp,
        taxonomy_version=RULE_VERSION, evidence_engine_version=EVIDENCE_ENGINE_VERSION,
    )


def test_updated_hp_is_reanalyzed():
    old = datetime(2026, 10, 14, tzinfo=timezone.utc)
    assert needs_reanalysis(
        hp_researched_at=old + timedelta(seconds=1), treatment_researched_at=old,
        taxonomy_version=RULE_VERSION, evidence_engine_version=EVIDENCE_ENGINE_VERSION,
    )


def test_engine_or_taxonomy_change_is_reanalyzed():
    stamp = "2026-10-14T00:00:00+00:00"
    assert needs_reanalysis(
        hp_researched_at=stamp, treatment_researched_at=stamp,
        taxonomy_version="old", evidence_engine_version=EVIDENCE_ENGINE_VERSION,
    )


def test_confidence_routes_to_auto_review_or_empty():
    assert confidence_route("CONFIRMED") == "AUTO"
    assert confidence_route("REVIEW") == "REVIEW"
    assert confidence_route("NOT_CONFIRMED") == "EMPTY"


def test_incremental_selector_and_kpi_are_select_only_and_preserve_provenance_contract():
    source = open("src/master/treatment_incremental.py", encoding="utf-8").read()
    assert "h.researched_at>t.researched_at" in source
    assert "c.uuid" in source
    assert "fetch_status='OK'" in source
    assert all(token not in source.upper() for token in ("INSERT INTO", "UPDATE ", "DELETE FROM"))
    worker = open("scripts/research_worker.py", encoding="utf-8").read()
    for field in ("source_url", "matched_alias", "evidence_engine_version",
                  "taxonomy_version", "researched_at"):
        assert field in worker


def test_manual_treatment_override_remains_separate_from_auto_sidecar():
    adapter = open("src/repository/treatment_supabase_write_adapter.py", encoding="utf-8").read()
    assert "provenance.manual_overrides" not in adapter
    assert "public.clinics" not in adapter
