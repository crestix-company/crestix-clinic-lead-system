"""Canonical Supabase Step 4 HP target selection.

The same predicate is used by the read-side count/candidate preview and the
write-side atomic job creator.  A clinic already queued in an unfinished HP
job is not a safe candidate for a second job, even when it has not reached the
HP ledger yet.
"""


UNFINISHED_HP_JOB_STATUSES = ("RUNNING", "PAUSED", "BUDGET")


def maps_hp_target_predicate(prefecture="", medical_types=None, force=False):
    conditions = [
        "c.merged_into IS NULL",
        "c.merge_hold=false",
        "c.active=true",
        "c.maps_presence_status='MAPS_MATCHED_WEBSITE'",
        "COALESCE(BTRIM(c.maps_website_url),'')<>''",
        "NOT EXISTS ("
        "SELECT 1 FROM research.research_job_items queued_i "
        "JOIN research.research_jobs queued_j ON queued_j.id=queued_i.job_id "
        "WHERE queued_i.clinic_id=c.id AND queued_i.state IN ('PENDING','RUNNING') "
        "AND queued_j.kind='hp' AND queued_j.status IN ('RUNNING','PAUSED','BUDGET')"
        ")",
    ]
    args = []
    if prefecture:
        conditions.append("c.prefecture=%s")
        args.append(prefecture)
    if medical_types:
        conditions.append("c.medical_type=ANY(%s::text[])")
        args.append(list(medical_types))
    if not force:
        conditions.append(
            "NOT EXISTS (SELECT 1 FROM hp_research.clinic_hp_research h "
            "WHERE h.clinic_id=c.id)"
        )
    return " AND ".join(conditions), args
