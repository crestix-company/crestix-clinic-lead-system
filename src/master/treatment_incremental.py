"""Incremental candidate and KPI contract for the existing Treatment worker.

The canonical taxonomy/evidence engine and persistence remain in
``treatment_taxonomy`` and ``scripts.research_worker``.  This module only decides
which successfully refreshed HP records need that existing analysis again.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.enrichment.treatment_taxonomy import EVIDENCE_ENGINE_VERSION, RULE_VERSION


def _time(value):
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def needs_reanalysis(*, hp_researched_at, treatment_researched_at=None,
                     taxonomy_version="", evidence_engine_version=""):
    """True only for new/updated HP content or an explicit engine version change."""
    return (
        treatment_researched_at is None
        or _time(hp_researched_at) > _time(treatment_researched_at)
        or str(taxonomy_version or "") != RULE_VERSION
        or str(evidence_engine_version or "") != EVIDENCE_ENGINE_VERSION
    )


def confidence_route(research_status):
    """Map the existing canonical status vocabulary to AUTO/REVIEW/EMPTY."""
    status = str(research_status or "").upper()
    if status == "CONFIRMED":
        return "AUTO"
    if status == "REVIEW":
        return "REVIEW"
    return "EMPTY"


def select_incremental_candidates(conn, *, limit=500):
    """SELECT-only delta: verified HP newer than its Treatment checkpoint/version."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id,c.uuid,c.clinic_name,c.phone,c.address,c.departments_json,
                   COALESCE(NULLIF(h.final_url,''),h.hp_url),h.researched_at
            FROM hp_research.clinic_hp_research h
            JOIN public.clinics c ON c.id=h.clinic_id AND c.merged_into IS NULL
            LEFT JOIN treatment.clinic_research_status t ON t.clinic_id=c.id
            WHERE h.fetch_status='OK'
              AND COALESCE(NULLIF(h.final_url,''),h.hp_url)<>''
              AND (t.clinic_id IS NULL OR h.researched_at>t.researched_at
                   OR t.taxonomy_version<>%s OR t.evidence_engine_version<>%s)
            ORDER BY h.researched_at,c.id
            LIMIT %s
            """,
            (RULE_VERSION, EVIDENCE_ENGINE_VERSION, min(1000, max(1, int(limit)))),
        )
        columns = [item.name for item in cur.description]
        rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    conn.commit()
    return rows


def coverage_kpis(conn):
    """SELECT-only coverage for HP-verified clinics using existing Treatment tables."""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH verified AS (
              SELECT clinic_id,researched_at FROM hp_research.clinic_hp_research
              WHERE fetch_status='OK'
            ), flags AS (
              SELECT v.clinic_id,v.researched_at AS hp_researched_at,
                     s.research_status,s.researched_at AS treatment_researched_at,
                     EXISTS(SELECT 1 FROM treatment.clinic_treatment_research r
                       WHERE r.clinic_id=v.clinic_id AND r.research_status='CONFIRMED') AS found,
                     EXISTS(SELECT 1 FROM treatment.clinic_treatment_research r
                       WHERE r.clinic_id=v.clinic_id AND r.research_status='REVIEW') AS review
              FROM verified v LEFT JOIN treatment.clinic_research_status s
                ON s.clinic_id=v.clinic_id
            )
            SELECT count(*),
              count(*) FILTER(WHERE research_status='DONE'),
              count(*) FILTER(WHERE found),
              count(*) FILTER(WHERE review),
              count(*) FILTER(WHERE research_status='FETCH_FAILED'),
              count(*) FILTER(WHERE research_status IS NULL
                OR hp_researched_at>treatment_researched_at)
            FROM flags
            """
        )
        values = cur.fetchone()
    conn.commit()
    keys = ("hp_verified", "treatment_researched", "treatment_found",
            "treatment_review", "treatment_error", "treatment_unresearched")
    return {key: int(value or 0) for key, value in zip(keys, values)}
