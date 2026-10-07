"""Supabase (PostgreSQL) WRITE adapter -- Stage4-D Gate2-A.

Implements the Protocols in src.repository.write_contracts against the migrated Postgres
schema, under the Owner Decision 1 (medical_key identity) and Owner Decision 2 (reserved
high-range numeric ID) contracts recorded in
docs/supabase_migration/22_stage4d_write_inventory_gate.md.

Hard rules enforced throughout this file (see src.repository.write_backend module docstring):
  - No silent WRITE fallback: every method either commits or raises; nothing here ever catches
    an exception and falls back to SQLite, and nothing retries a write automatically.
  - No dual-write: a successful Supabase write is never copied to SQLite by this code.
  - Transactions never span an external HTTP/API call (see SupabaseSearchWriteRepository).
  - This module is purely additive: nothing in app_v2.py / src/master/*.py imports it yet, so
    the live SQLite WRITE-primary runtime is completely unchanged by this file's existence.

Not yet implemented (explicitly, not silently): import_maps_results. google_maps.py's
import_maps_results() batch-matching/anti-downgrade logic (find_target scoring, the
PRESERVED_WEBSITE rule) was only partially read while preparing this adapter; porting it
without full fidelity would risk a silent business-logic divergence, which is worse than
leaving it unimplemented. See docs/supabase_migration/23_stage4d_gate2_offline_preparation.md.
"""
import json

from psycopg import sql
from psycopg.types.json import Jsonb

from src.master.store import now, dumps
from src.master.matching import medical_key
from src.master.identity_contract import (
    generate_medical_key_for_new_clinic, resolve_medical_key_transition,
    refresh_clinic_projection_preserving_identity,
)
from src.repository.high_range_id import assert_high_range


def _rollback_and_raise(conn, exc):
    conn.rollback()
    raise exc


class SupabaseSettingsWriteRepository:
    _ALLOWED = {"monthly_limit", "external_usage_reserve", "filter_defaults", "age_basis"}

    def __init__(self, conn):
        self._conn = conn

    def set(self, key, value):
        if key not in self._ALLOWED:
            raise ValueError("保存できない設定項目です。APIキーは保存できません。")
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app_config.settings(key,value) VALUES(%s,%s) "
                    "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
                    (key, dumps(value)),
                )
            self._conn.commit()
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)


class SupabaseResearchWriteRepository:
    def __init__(self, conn):
        self._conn = conn

    def save_research(self, clinic_id, result, pages=None):
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT result_json FROM research.research_results WHERE clinic_id=%s FOR UPDATE", (clinic_id,))
                row = cur.fetchone()
                previous = json.loads(row[0]) if row else {}
                updated = {**previous, **result}
                cur.execute(
                    "INSERT INTO research.research_results(clinic_id,result_json,updated_at) VALUES(%s,%s,%s) "
                    "ON CONFLICT(clinic_id) DO UPDATE SET result_json=EXCLUDED.result_json,updated_at=EXCLUDED.updated_at",
                    (clinic_id, dumps(updated), now()),
                )
                if pages is not None:
                    cur.execute("DELETE FROM research.hp_pages WHERE clinic_id=%s", (clinic_id,))
                    for p in pages:
                        cur.execute(
                            "INSERT INTO research.hp_pages(clinic_id,url,page_json,checked_at) VALUES(%s,%s,%s,%s) "
                            "ON CONFLICT(clinic_id,url) DO UPDATE SET page_json=EXCLUDED.page_json,checked_at=EXCLUDED.checked_at",
                            (clinic_id, p["url"], dumps(p), now()),
                        )
                cur.execute(
                    "INSERT INTO provenance.change_history(clinic_id,action,before_json,after_json,note,created_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s)",
                    (clinic_id, "自動調査", dumps(previous), dumps(updated), "", now()),
                )
            self._conn.commit()
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)

    def override(self, clinic_id, field, value, *, note="", source="手動確認"):
        allowed = {"hp_url", "hp_status", "hp_rank", "epark_url", "epark_contract",
                   "google_ads_status", "treatment_categories", "marketing_signals"}
        if field not in allowed:
            raise ValueError("手動修正の対象外です。")
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT value_json FROM provenance.manual_overrides WHERE clinic_id=%s AND field=%s FOR UPDATE",
                    (clinic_id, field),
                )
                before = cur.fetchone()
                if value is None:
                    cur.execute(
                        "DELETE FROM provenance.manual_overrides WHERE clinic_id=%s AND field=%s", (clinic_id, field)
                    )
                else:
                    cur.execute(
                        "INSERT INTO provenance.manual_overrides(clinic_id,field,value_json,source,note,updated_at) "
                        "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(clinic_id,field) DO UPDATE SET "
                        "value_json=EXCLUDED.value_json,source=EXCLUDED.source,note=EXCLUDED.note,updated_at=EXCLUDED.updated_at",
                        (clinic_id, field, Jsonb(value), source, note, now()),
                    )
                cur.execute(
                    "INSERT INTO provenance.change_history(clinic_id,action,before_json,after_json,note,created_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s)",
                    (clinic_id, "手動修正:" + field, dumps(before[0] if before else None), dumps(value), note, now()),
                )
            self._conn.commit()
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)


class SupabaseProvenanceWriteRepository:
    def __init__(self, conn):
        self._conn = conn

    def insert_source_record(self, clinic_id, source, record, source_hash, row_number,
                              match_status, match_reason, match_score):
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO provenance.source_records"
                    "(clinic_id,source,record_json,source_hash,row_number,match_status,match_reason,match_score,created_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (clinic_id, source, Jsonb(record), source_hash, row_number, match_status, match_reason,
                     match_score, now()),
                )
                new_id = cur.fetchone()[0]
                assert_high_range(new_id, table="provenance.source_records")
            self._conn.commit()
            return new_id
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)

    def history(self, clinic_id, action, before, after, note=""):
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO provenance.change_history(clinic_id,action,before_json,after_json,note,created_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s)",
                    (clinic_id, action, dumps(before), dumps(after), note, now()),
                )
            self._conn.commit()
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)

    def import_maps_results(self, clinic_id, result, batch_hash):
        raise NotImplementedError(
            "Supabase adapterはimport_maps_results()に未対応です(Gate2-A offline preparationの"
            "範囲外 -- google_maps.pyのfind_target/PRESERVED_WEBSITE anti-downgrade logicの"
            "完全な移植とparity testが先に必要です)。SQLite backendを使用してください。"
        )


class SupabaseClinicWriteRepository:
    def __init__(self, conn):
        self._conn = conn

    def insert_new_clinic(self, record, *, is_new=False, is_authoritative_official_source=False):
        key = generate_medical_key_for_new_clinic(record, is_authoritative_official_source=is_authoritative_official_source)
        ts = now()
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.clinics(uuid,base_json,first_seen_at,last_seen_at,source_as_of_date,is_new) "
                    "VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
                    (record.get("uuid", ""), Jsonb(record), ts, ts, record.get("as_of", ""), bool(is_new)),
                )
                new_id = cur.fetchone()[0]
                assert_high_range(new_id, table="public.clinics")
                if key:
                    cur.execute("UPDATE public.clinics SET medical_key=%s WHERE id=%s", (key, new_id))
                cur.execute(
                    "INSERT INTO provenance.change_history(clinic_id,action,before_json,after_json,note,created_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s)",
                    (new_id, "医院追加", dumps({}), dumps(record), "", ts),
                )
            self._conn.commit()
            return new_id
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)

    def update_matched_clinic_base(self, clinic_id, record, *, source, as_of):
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT base_json,uuid,source_as_of_date FROM public.clinics WHERE id=%s FOR UPDATE", (clinic_id,)
                )
                row = cur.fetchone()
                base, uuid_val, source_as_of_date = row
                before = dict(base)
                if source == "厚生局" and as_of >= source_as_of_date:
                    base.update(record)
                else:
                    base.update({k: v for k, v in record.items() if v and not base.get(k)})
                uid = uuid_val or record.get("uuid", "")
                ts = now()
                cur.execute(
                    "UPDATE public.clinics SET base_json=%s,uuid=%s,last_seen_at=%s,"
                    "source_as_of_date=CASE WHEN %s<source_as_of_date THEN source_as_of_date ELSE %s END WHERE id=%s",
                    (Jsonb(base), uid, ts, as_of, as_of, clinic_id),
                )
                if before != base:
                    cur.execute(
                        "INSERT INTO provenance.change_history(clinic_id,action,before_json,after_json,note,created_at) "
                        "VALUES(%s,%s,%s,%s,%s,%s)",
                        (clinic_id, "マスター更新", dumps(before), dumps(base), "", ts),
                    )
            self._conn.commit()
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)

    def refresh_projection(self, clinic_id, *, is_authoritative_official_source=False):
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT base_json,uuid,medical_key FROM public.clinics WHERE id=%s FOR UPDATE", (clinic_id,)
                )
                base, uuid_val, stored_key = cur.fetchone()
                cur.execute("SELECT result_json FROM research.research_results WHERE clinic_id=%s", (clinic_id,))
                research_row = cur.fetchone()
                research = json.loads(research_row[0]) if research_row else {}
                cur.execute("SELECT field,value_json FROM provenance.manual_overrides WHERE clinic_id=%s", (clinic_id,))
                manual = dict(cur.fetchall())
                data = {**base, **research, **manual}
                data["uuid"] = uuid_val
                data["manual_fields"] = list(manual)
                fields = refresh_clinic_projection_preserving_identity(data)
                candidate_key = medical_key({**data, **fields})
                resolved_key = resolve_medical_key_transition(
                    stored_key, candidate_key, authoritative=is_authoritative_official_source
                )
                fields["effective_json"] = dumps(fields["effective_json"])  # TEXT column, not JSONB
                columns = list(fields)
                values = [Jsonb(fields[c]) if c in ("departments_json", "treatments_json", "signals_json") else fields[c]
                          for c in columns]
                if resolved_key != stored_key:
                    columns.append("medical_key")
                    values.append(resolved_key)
                set_sql = sql.SQL(",").join(
                    sql.SQL("{}=%s").format(sql.Identifier(c)) for c in columns
                )
                cur.execute(
                    sql.SQL("UPDATE public.clinics SET {} WHERE id=%s").format(set_sql),
                    (*values, clinic_id),
                )
            self._conn.commit()
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)

    def resolve_review_to_new_clinic(self, record, *, is_authoritative_official_source=False):
        return self.insert_new_clinic(record, is_new=True, is_authoritative_official_source=is_authoritative_official_source)

    def resolve_review_to_existing_clinic(self, clinic_id, record, *, source):
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT base_json,uuid FROM public.clinics WHERE id=%s FOR UPDATE", (clinic_id,))
                base, uuid_val = cur.fetchone()
                if source == "厚生局":
                    base.update(record)
                else:
                    base.update({k: v for k, v in record.items() if v and not base.get(k)})
                cur.execute(
                    "UPDATE public.clinics SET uuid=%s,base_json=%s,last_seen_at=%s WHERE id=%s",
                    (uuid_val or record.get("uuid", ""), Jsonb(base), now(), clinic_id),
                )
            self._conn.commit()
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)


class SupabaseJobsWriteRepository:
    def __init__(self, conn):
        self._conn = conn

    def create_job(self, clinic_ids, kind, options, max_searches):
        import uuid
        jid = uuid.uuid4().hex
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO research.research_jobs(id,kind,options_json,max_searches,created_at,updated_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s)",
                    (jid, kind, Jsonb(options), max_searches, now(), now()),
                )
                cur.executemany(
                    "INSERT INTO research.research_job_items(job_id,clinic_id) VALUES(%s,%s)",
                    [(jid, i) for i in clinic_ids],
                )
            self._conn.commit()
            return jid
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)

    def pause_job(self, job_id):
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE research.research_jobs SET status='PAUSED',updated_at=%s WHERE id=%s AND status<>'COMPLETED'",
                    (now(), job_id),
                )
            self._conn.commit()
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)

    def reset_job(self, job_id):
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT status FROM research.research_jobs WHERE id=%s FOR UPDATE", (job_id,))
                row = cur.fetchone()
                if not row:
                    raise ValueError("調査履歴が見つかりません。")
                if row[0] == "RUNNING":
                    raise ValueError("実行中の調査はリセットできません。先に一時停止し、停止完了を待ってください。")
                cur.execute(
                    "UPDATE research.research_job_items SET state='CANCELLED' WHERE job_id=%s AND state='PENDING'",
                    (job_id,),
                )
                cur.execute("UPDATE research.research_jobs SET status='RESET',updated_at=%s WHERE id=%s", (now(), job_id))
            self._conn.commit()
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)

    def job_limit(self, job_id, limit):
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE research.research_jobs SET max_searches=%s,updated_at=%s WHERE id=%s",
                    (max(0, int(limit)), now(), job_id),
                )
            self._conn.commit()
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)

    def claim_next_pending_item(self, job_id):
        """Atomic claim without an explicit BEGIN IMMEDIATE-style whole-table lock: the
        FOR UPDATE SKIP LOCKED subquery takes a row lock on exactly one candidate row, so two
        concurrent claimants never receive the same clinic_id (Postgres-native equivalent of
        the SQLite BEGIN IMMEDIATE + single-row UPDATE pattern in src/master/jobs.py)."""
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE research.research_job_items SET state='RUNNING' "
                    "WHERE (job_id,clinic_id) = ("
                    "  SELECT job_id,clinic_id FROM research.research_job_items "
                    "  WHERE job_id=%s AND state='PENDING' ORDER BY clinic_id LIMIT 1 FOR UPDATE SKIP LOCKED"
                    ") RETURNING clinic_id",
                    (job_id,),
                )
                row = cur.fetchone()
            self._conn.commit()
            return row[0] if row else None
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)

    def mark_item_state(self, job_id, clinic_id, state, *, result=None, note=""):
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE research.research_job_items SET state=%s,result=%s,note=%s WHERE job_id=%s AND clinic_id=%s",
                    (state, result, note, job_id, clinic_id),
                )
            self._conn.commit()
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)

    def mark_job_status(self, job_id, status):
        try:
            with self._conn.cursor() as cur:
                cur.execute("UPDATE research.research_jobs SET status=%s,updated_at=%s WHERE id=%s", (status, now(), job_id))
            self._conn.commit()
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)


class SupabaseSearchWriteRepository:
    """CachedSearch's two-phase contract: reserve_attempt() commits BEFORE any external HTTP
    call the caller makes in between; store_cache_result() commits AFTER. No transaction here
    ever spans the external call -- matching src/enrichment/search_provider.py exactly."""

    def __init__(self, conn):
        self._conn = conn

    def reserve_attempt(self, job_id, query_key, month):
        try:
            with self._conn.cursor() as cur:
                if job_id:
                    cur.execute("UPDATE research.research_jobs SET search_count=search_count+1 WHERE id=%s", (job_id,))
                cur.execute(
                    "INSERT INTO research.search_usage(month,job_id,query_key,attempted_at) VALUES(%s,%s,%s,%s)",
                    (month, job_id, query_key, now()),
                )
            self._conn.commit()
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)

    def store_cache_result(self, query_key, query, result):
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO research.search_cache(query_key,query,result_json,searched_at) VALUES(%s,%s,%s,%s) "
                    "ON CONFLICT(query_key) DO UPDATE SET query=EXCLUDED.query,result_json=EXCLUDED.result_json,"
                    "searched_at=EXCLUDED.searched_at",
                    (query_key, query, Jsonb(result), now()),
                )
            self._conn.commit()
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)

    def get_cached(self, query_key):
        with self._conn.cursor() as cur:
            cur.execute("SELECT result_json FROM research.search_cache WHERE query_key=%s", (query_key,))
            row = cur.fetchone()
        return row[0] if row else None

    def monthly_usage_count(self, month):
        with self._conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM research.search_usage WHERE month=%s", (month,))
            return cur.fetchone()[0]
