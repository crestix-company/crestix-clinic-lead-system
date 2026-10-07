"""Postgres port of src.master.hp_research_batch.upsert_result() for Stage4-D Gate2.

Column renames/type differences vs the SQLite sidecar (see schema_target.sql):
  - hp_abc_candidate (SQLite) -> machine_hp_rank (Postgres), same value, different name.
  - treatment_categories / feature_json: TEXT-serialized JSON in SQLite -> jsonb in Postgres
    (parsed with json.loads before wrapping in Jsonb -- the SQLite result dict always carries
    these as json.dumps() strings, see process_one()/derive_results() in hp_research_batch.py).
  - portal_name: a column that does not exist in the SQLite sidecar at all ("ETL投入時にURLから
    確定" per schema_target.sql's comment -- it was a one-time migration-time derivation).
    For ongoing live writes this computes it the same way, at write time, reusing the existing
    portal_name_for_url() pure function from src.master.hp_site_type -- not reimplemented.

Idempotent by construction (ON CONFLICT(clinic_id) DO UPDATE, attempts computed from the prior
stored value read in the same transaction) -- matches the SQLite original's own idempotency
exactly, including under retry/duplicate submission.
"""
import json

from psycopg.types.json import Jsonb


def _rollback_and_raise(conn, exc):
    conn.rollback()
    raise exc


class SupabaseHpWriteRepository:
    def __init__(self, conn):
        self._conn = conn

    def prior_attempts(self, clinic_id):
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT attempts FROM hp_research.clinic_hp_research WHERE clinic_id=%s",
                (clinic_id,),
            )
            row = cur.fetchone()
        return row[0] if row else None

    def upsert_result(self, result):
        from src.master.hp_site_type import portal_name_for_url
        portal_name = portal_name_for_url(result.get("final_url") or result.get("hp_url") or "")
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT attempts FROM hp_research.clinic_hp_research WHERE clinic_id=%s",
                    (result["clinic_id"],),
                )
                row = cur.fetchone()
                prior_attempts = row[0] if row else 0
                cur.execute(
                    "INSERT INTO hp_research.clinic_hp_research "
                    "(clinic_id, hp_url, fetch_status, final_url, treatment_status, treatment_categories, "
                    " machine_hp_rank, hp_abc_score, candidate_rank_1, candidate_rank_2, ambiguity_reason, "
                    " feature_json, researched_at, engine_version, error_detail, attempts, elapsed_seconds, "
                    " portal_name) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT(clinic_id) DO UPDATE SET "
                    "hp_url=EXCLUDED.hp_url, fetch_status=EXCLUDED.fetch_status, final_url=EXCLUDED.final_url, "
                    "treatment_status=EXCLUDED.treatment_status, treatment_categories=EXCLUDED.treatment_categories, "
                    "machine_hp_rank=EXCLUDED.machine_hp_rank, hp_abc_score=EXCLUDED.hp_abc_score, "
                    "candidate_rank_1=EXCLUDED.candidate_rank_1, candidate_rank_2=EXCLUDED.candidate_rank_2, "
                    "ambiguity_reason=EXCLUDED.ambiguity_reason, feature_json=EXCLUDED.feature_json, "
                    "researched_at=EXCLUDED.researched_at, engine_version=EXCLUDED.engine_version, "
                    "error_detail=EXCLUDED.error_detail, attempts=EXCLUDED.attempts, "
                    "elapsed_seconds=EXCLUDED.elapsed_seconds, portal_name=EXCLUDED.portal_name",
                    (
                        result["clinic_id"], result["hp_url"], result["fetch_status"], result["final_url"],
                        result["treatment_status"], Jsonb(json.loads(result["treatment_categories"])),
                        result["hp_abc_candidate"], result["hp_abc_score"], result["candidate_rank_1"],
                        result["candidate_rank_2"], result["ambiguity_reason"],
                        Jsonb(json.loads(result["feature_json"])), result["researched_at"],
                        result["engine_version"], result["error_detail"], prior_attempts + 1,
                        result["elapsed_seconds"], portal_name,
                    ),
                )
            self._conn.commit()
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)

    def insert_missing_results(self, results):
        """Insert reconstructed ledger entries without ever overwriting an existing clinic row.

        The caller owns the surrounding transaction so a narrowly scoped reconciliation can
        validate preconditions and insert all missing entries atomically.
        """
        from src.master.hp_site_type import portal_name_for_url

        sql = (
            "INSERT INTO hp_research.clinic_hp_research "
            "(clinic_id,hp_url,fetch_status,final_url,treatment_status,treatment_categories,"
            "machine_hp_rank,hp_abc_score,candidate_rank_1,candidate_rank_2,ambiguity_reason,"
            "feature_json,researched_at,engine_version,error_detail,attempts,elapsed_seconds,portal_name) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(clinic_id) DO NOTHING RETURNING clinic_id"
        )
        inserted = []
        with self._conn.cursor() as cur:
            for result in results:
                final_url = result.get("final_url") or result.get("hp_url") or ""
                portal_name = result.get("portal_name", portal_name_for_url(final_url))
                cur.execute(sql, (
                    result["clinic_id"], result.get("hp_url") or "", result["fetch_status"],
                    result.get("final_url") or "", result.get("treatment_status") or "",
                    Jsonb(json.loads(result.get("treatment_categories") or "[]")),
                    result.get("hp_abc_candidate") or "", result.get("hp_abc_score") or "",
                    result.get("candidate_rank_1") or "", result.get("candidate_rank_2") or "",
                    result.get("ambiguity_reason") or "",
                    Jsonb(json.loads(result.get("feature_json") or "[]")),
                    result["researched_at"], result["engine_version"], result.get("error_detail") or "",
                    int(result.get("attempts", 1)), float(result.get("elapsed_seconds", 0)), portal_name,
                ))
                row = cur.fetchone()
                if row:
                    inserted.append(row[0])
        return inserted
