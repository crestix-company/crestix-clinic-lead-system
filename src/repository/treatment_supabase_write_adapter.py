"""Postgres port of scripts.research_worker.write_clinic_atomic() for Stage4-D Gate2.

Same semantics, same single-transaction atomicity as the SQLite original:
  - DONE: treatment.clinic_treatment_research fully replaced for clinic_id (DELETE+INSERT).
  - FETCH_FAILED: treatment.clinic_treatment_research left untouched for clinic_id.
  - treatment.clinic_research_status always upserted (ON CONFLICT(clinic_id) DO UPDATE).
No silent fallback: raises and rolls back on any failure; never retries automatically and
never falls back to writing SQLite.
"""


def _rollback_and_raise(conn, exc):
    conn.rollback()
    raise exc


class SupabaseTreatmentWriteRepository:
    def __init__(self, conn):
        self._conn = conn

    def write_clinic_result(self, clinic_id, rows, status_row):
        try:
            with self._conn.cursor() as cur:
                if status_row["research_status"] == "DONE":
                    cur.execute("DELETE FROM treatment.clinic_treatment_research WHERE clinic_id=%s", (clinic_id,))
                    if rows:
                        cur.executemany(
                            "INSERT INTO treatment.clinic_treatment_research "
                            "(clinic_id, treatment_category_id, treatment_category_name, research_status, "
                            " matched_alias, source_url, page_title, provider_context, exclusion_context, "
                            " evidence_engine_version, taxonomy_version, researched_at, git_commit_sha, manifest_id) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            [(
                                clinic_id, r["treatment_category_name"], r["treatment_category_name"],
                                r["research_status"], r.get("matched_alias", ""), r.get("source_url", ""),
                                r.get("page_title", ""), r.get("provider_context", ""), r.get("exclusion_context", ""),
                                r["evidence_engine_version"], r["taxonomy_version"], r["researched_at"],
                                r["git_commit_sha"], r["manifest_id"],
                            ) for r in rows],
                        )
                cur.execute(
                    "INSERT INTO treatment.clinic_research_status "
                    "(clinic_id, research_status, candidate_count, source_url, final_url, identity_verified, "
                    " attempts, last_error, taxonomy_version, evidence_engine_version, manifest_id, git_commit_sha, "
                    " researched_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT(clinic_id) DO UPDATE SET "
                    "research_status=EXCLUDED.research_status, candidate_count=EXCLUDED.candidate_count, "
                    "source_url=EXCLUDED.source_url, final_url=EXCLUDED.final_url, "
                    "identity_verified=EXCLUDED.identity_verified, attempts=EXCLUDED.attempts, "
                    "last_error=EXCLUDED.last_error, taxonomy_version=EXCLUDED.taxonomy_version, "
                    "evidence_engine_version=EXCLUDED.evidence_engine_version, manifest_id=EXCLUDED.manifest_id, "
                    "git_commit_sha=EXCLUDED.git_commit_sha, researched_at=EXCLUDED.researched_at",
                    (
                        clinic_id, status_row["research_status"], status_row["candidate_count"],
                        status_row["source_url"], status_row["final_url"], bool(status_row["identity_verified"]),
                        status_row["attempts"], status_row["last_error"], status_row["taxonomy_version"],
                        status_row["evidence_engine_version"], status_row["manifest_id"],
                        status_row["git_commit_sha"], status_row["researched_at"],
                    ),
                )
            self._conn.commit()
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)
