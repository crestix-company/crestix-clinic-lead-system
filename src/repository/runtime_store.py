"""Supabase-only facade used by the production application runtime.

This deliberately has no SQLite store, path, fallback, comparator, or sidecar attachment.
SQLite repositories remain available to explicit migration/admin/test callers through the
repository factories, but production UI and workers receive this object.
"""
from __future__ import annotations

import json
from functools import wraps
import threading

from src.io.output_writer import csv_bytes, xlsx_bytes
from src.master.comdesk import COMDESK_HEADERS, COMDESK_EXPORT_HEADERS
from src.master.fixed_export import fixed_row
from src.master.filters import Filters


from src.repository.hp_targets import maps_hp_target_predicate as _maps_hp_target_predicate
from src.repository.connection_diagnostics import retryable_read_connection_error


# These person/schedule fields remain outside the Cloud Run READ ONLY projection until their
# data-use approval is recorded. Blank only the READ ONLY export; ordinary Mac exports retain the
# existing Comdesk behavior and source values.
READ_ONLY_PENDING_EXPORT_HEADERS = frozenset({
    "院長名", "休診日", "診療日", "午前始", "午前終", "午後始", "午後終",
})


def _verified_email_map(rows):
    """Build deterministic, de-duplicated Step 5 email values by clinic_id."""
    buckets = {}
    for clinic_id, email, status, verified_on_official in rows:
        if status != "VERIFIED_EMAIL" or verified_on_official is not True:
            continue
        value = str(email or "").strip()
        if not value:
            continue
        # Email domains are case-insensitive and real enrichment feeds may repeat the same
        # address with casing differences. Keep one deterministic representation.
        key = value.casefold()
        choices = buckets.setdefault(int(clinic_id), {})
        previous = choices.get(key)
        if previous is None or value < previous:
            choices[key] = value
    return {
        clinic_id: ";".join(sorted(values.values(), key=lambda value: (value.casefold(), value)))
        for clinic_id, values in buckets.items()
    }


def _retry_closed_read_connection(method):
    """Retry one read operation after rebuilding a stale runtime connection.

    Streamlit caches SupabaseRuntimeStore for the process lifetime. A direct/session Postgres
    connection can still be closed by the network or server while the app remains alive.
    Reads are safe to retry once; write transactions deliberately use a separate repository
    bundle and are never retried here.
    """
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        self._ensure_read_connection()
        failed_conn = self._conn
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:
            if not self._is_retryable_connection_error(exc):
                raise
            self._reconnect_read(expected_conn=failed_conn, force=True)
            return method(self, *args, **kwargs)
    return wrapped


class SupabaseRuntimeStore:
    is_supabase_runtime = True
    runtime_identity = "supabase"

    def __init__(self, repositories=None):
        self._owns_runtime_connection = repositories is None
        self._reconnect_lock = threading.Lock()
        if repositories is None:
            from src.repository.backend import build_repositories
            repositories = build_repositories("supabase")
        self.repositories = repositories
        self._conn = repositories.clinics._conn

    def _reconnect_read(self, *, expected_conn=None, force=False):
        if not getattr(self, "_owns_runtime_connection", False):
            return
        with self._reconnect_lock:
            old_conn = self._conn
            if expected_conn is not None and old_conn is not expected_conn:
                return
            if not force and old_conn is not None and not (
                getattr(old_conn, "closed", False) or getattr(old_conn, "broken", False)
            ):
                return
            from src.repository.backend import build_repositories
            repositories = build_repositories("supabase")
            self.repositories = repositories
            self._conn = repositories.clinics._conn

    def _ensure_read_connection(self):
        if not getattr(self, "_owns_runtime_connection", False):
            return
        if getattr(self._conn, "closed", False) or getattr(self._conn, "broken", False):
            self._reconnect_read(expected_conn=self._conn)

    def _is_retryable_connection_error(self, exc):
        if not getattr(self, "_owns_runtime_connection", False):
            return False
        return retryable_read_connection_error(exc)

    @property
    def path(self):
        """Compatibility identity only; never represents or opens a filesystem path."""
        return self.runtime_identity

    @_retry_closed_read_connection
    def get(self, clinic_id): return self.repositories.clinics.get(clinic_id)
    @_retry_closed_read_connection
    def get_hp_research_clinic(self, clinic_id):
        return self.repositories.clinics.get_hp_research_clinic(clinic_id)
    @_retry_closed_read_connection
    def get_by_uuid(self, value): return self.repositories.clinics.get_by_uuid(value)
    @_retry_closed_read_connection
    def get_by_medical_key(self, value): return self.repositories.clinics.get_by_medical_key(value)
    @_retry_closed_read_connection
    def query(self, filters=None, limit=100, offset=0, as_of=None):
        return self.repositories.clinics.query(filters, limit, offset, as_of)
    @_retry_closed_read_connection
    def query_read_only_ui(self, filters=None, limit=100, offset=0, as_of=None):
        return self.repositories.clinics.query(
            filters, limit, offset, as_of, read_only_ui=True
        )
    @_retry_closed_read_connection
    def records_by_ids(self, clinic_ids, *, page_size=500):
        return self.repositories.clinics.records_by_ids(clinic_ids, page_size=page_size)
    @_retry_closed_read_connection
    def count(self, filters=None, as_of=None): return self.repositories.clinics.count(filters, as_of)
    @_retry_closed_read_connection
    def count_read_only_ui(self, filters=None, as_of=None):
        return self.repositories.clinics.count(filters, as_of, read_only_ui=True)
    @_retry_closed_read_connection
    def funnel(self, filters, as_of=None): return self.repositories.clinics.funnel(filters, as_of)
    @_retry_closed_read_connection
    def metrics(self, *, read_only_ui=False):
        if read_only_ui:
            return self.repositories.clinics.metrics(read_only_ui=True)
        return self.repositories.clinics.metrics()
    @_retry_closed_read_connection
    def treatment_status_for_ids(self, ids): return self.repositories.treatment.status_for_ids(ids)
    @_retry_closed_read_connection
    def treatment_category_options(self):
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT treatment_category_name FROM treatment.clinic_treatment_research "
                "WHERE treatment_category_name<>'' ORDER BY treatment_category_name"
            )
            return [r[0] for r in cur.fetchall()]
    @_retry_closed_read_connection
    def web_research_metrics(self, *, read_only_ui=False):
        return self.repositories.hp_research.batch_metrics(read_only_ui=read_only_ui)

    @_retry_closed_read_connection
    def email_research_status_metrics(self):
        """Aggregate the allowlisted Email status projection; never read candidate addresses."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT research_status,count(*) FROM email_research.clinic_email_status_readonly "
                "GROUP BY research_status ORDER BY research_status"
            )
            rows = cur.fetchall()
        return {str(status): int(count) for status, count in rows}
    @_retry_closed_read_connection
    def history(self, clinic_id, limit=30):
        return self.repositories.provenance.history_for_clinic(clinic_id, limit)
    @_retry_closed_read_connection
    def templates(self): return self.repositories.provenance.templates()
    @_retry_closed_read_connection
    def reviews(self, limit=100): return self.repositories.provenance.reviews(limit)
    @_retry_closed_read_connection
    def comdesk_linkage_state(self): return self.repositories.provenance.comdesk_linkage_state()

    @_retry_closed_read_connection
    def comdesk_identity_for_clinics(self, clinic_ids):
        """Read common IDs and canonical Comdesk UUIDs for CSV export; no clinic write."""
        from src.master.clinic_identity import common_identity_enabled
        if not common_identity_enabled():
            return {}
        ids = sorted({int(cid) for cid in clinic_ids if int(cid) > 0})
        if not ids:
            return {}
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.sales_entity_clinics'), "
                "to_regclass('public.sales_entity_comdesk_identity')"
            )
            if not all(cur.fetchone()):
                return {}
            cur.execute(
                "SELECT m.clinic_id,m.common_clinic_id,i.comdesk_uuid "
                "FROM public.sales_entity_clinics m "
                "LEFT JOIN public.sales_entity_comdesk_identity i USING(common_clinic_id) "
                "WHERE m.clinic_id=ANY(%s::bigint[])",
                (ids,),
            )
            return {
                int(clinic_id): {
                    "common_clinic_id": int(common_id),
                    "canonical_comdesk_uuid": str(comdesk_uuid or "").strip(),
                }
                for clinic_id, common_id, comdesk_uuid in cur.fetchall()
            }
    @_retry_closed_read_connection
    def setting(self, key, default=None): return self.repositories.settings.get(key, default)

    @_retry_closed_read_connection
    def prefectures(self, *, read_only_ui=False):
        source = "public.clinic_cloudrun_readonly" if read_only_ui else "public.clinics"
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT DISTINCT prefecture FROM {source} WHERE prefecture<>'' ORDER BY prefecture")
            return [r[0] for r in cur.fetchall()]

    @_retry_closed_read_connection
    def municipalities(self):
        # Keep the canonical Python extraction used by the Supabase filter post-pass.
        from src.normalizer.address import extract_municipality
        with self._conn.cursor() as cur:
            cur.execute("SELECT DISTINCT address FROM public.clinics WHERE address<>''")
            values = {extract_municipality(r[0]) for r in cur.fetchall()}
        return sorted(v for v in values if v)

    @_retry_closed_read_connection
    def ad_count_max(self, *, read_only_ui=False):
        from src.scoring.research_scoring import AD_SIGNAL_NAMES
        signal_names = [name for name in AD_SIGNAL_NAMES
                        if not read_only_ui or name != "EPARK課金済み確認"]
        source = "public.clinic_cloudrun_readonly" if read_only_ui else "public.clinics"
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX((SELECT count(DISTINCT v) FROM "
                "jsonb_array_elements_text(signals_json) v WHERE v=ANY(%s))),0) "
                f"FROM {source}", (signal_names,),
            )
            return cur.fetchone()[0]

    @_retry_closed_read_connection
    def has_maps(self):
        with self._conn.cursor() as cur:
            cur.execute("SELECT EXISTS(SELECT 1 FROM public.clinics WHERE maps_presence_status<>'')")
            return bool(cur.fetchone()[0])

    @_retry_closed_read_connection
    def maps_hp_candidate_ids(self, prefecture="", medical_types=None, force=False, limit=500):
        predicate, args = _maps_hp_target_predicate(
            prefecture, medical_types, force,
            clinic_relation="public.clinic_cloudrun_readonly",
        )
        args.append(min(500, max(1, int(limit))))
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT c.id FROM public.clinic_cloudrun_readonly c WHERE " + predicate +
                " ORDER BY c.is_new DESC,c.id LIMIT %s", args,
            )
            return [r[0] for r in cur.fetchall()]

    @_retry_closed_read_connection
    def maps_hp_available_count(self, prefecture="", medical_types=None, force=False):
        predicate, args = _maps_hp_target_predicate(
            prefecture, medical_types, force,
            clinic_relation="public.clinic_cloudrun_readonly",
        )
        with self._conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.clinic_cloudrun_readonly c WHERE " + predicate, args)
            return cur.fetchone()[0]

    @_retry_closed_read_connection
    def maps_hp_candidate_preview(self, prefecture="", medical_types=None, force=False, limit=10):
        predicate, args = _maps_hp_target_predicate(
            prefecture, medical_types, force,
            clinic_relation="public.clinic_cloudrun_readonly",
        )
        args.append(min(10, max(1, int(limit))))
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT c.id,c.clinic_name,c.address,c.medical_type,c.maps_website_url "
                "FROM public.clinic_cloudrun_readonly c WHERE " + predicate +
                " ORDER BY c.is_new DESC,c.id LIMIT %s", args,
            )
            return [
                {"id": row[0], "clinic_name": row[1], "address": row[2],
                 "medical_type": row[3], "maps_website_url": row[4]}
                for row in cur.fetchall()
            ]

    @_retry_closed_read_connection
    def maps_hp_status_candidates(self, prefecture="", medical_types=None, statuses=None, limit=50):
        """Reader-only HP candidates grouped by ledger status; reserved clinics stay excluded."""
        allowed = {"UNRESEARCHED", "ERROR", "REVIEW", "SUCCESS"}
        statuses = list(dict.fromkeys(statuses or []))
        if not statuses or any(value not in allowed for value in statuses):
            raise ValueError("Unsupported HP research status")
        predicate, args = _maps_hp_target_predicate(
            prefecture, medical_types, True,
            clinic_relation="public.clinic_cloudrun_readonly",
            include_verified=True,
        )
        status_expr = ("CASE WHEN c.hp_status='VERIFIED' OR h.fetch_status='OK' THEN 'SUCCESS' "
                       "WHEN h.clinic_id IS NULL THEN 'UNRESEARCHED' "
                       "WHEN h.fetch_status='REVIEW' THEN 'REVIEW' "
                       "ELSE 'ERROR' END")
        predicate += f" AND ({status_expr})=ANY(%s::text[])"
        args.append(statuses)
        args.append(min(100, max(1, int(limit))))
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT c.id,c.clinic_name,c.address,c.medical_type,c.maps_website_url," + status_expr + ",h.fetch_status" +
                " FROM public.clinic_cloudrun_readonly c LEFT JOIN hp_research.clinic_hp_research h "
                "ON h.clinic_id=c.id WHERE " + predicate + " ORDER BY c.is_new DESC,c.id LIMIT %s", args,
            )
            return [{"id": row[0], "clinic_name": row[1], "address": row[2],
                     "medical_type": row[3], "maps_website_url": row[4], "hp_status": row[5],
                     "hp_status_detail": row[6]}
                    for row in cur.fetchall()]

    @_retry_closed_read_connection
    def maps_hp_status_count(self, prefecture="", medical_types=None, statuses=None):
        allowed = {"UNRESEARCHED", "ERROR", "REVIEW", "SUCCESS"}
        statuses = list(dict.fromkeys(statuses or []))
        if not statuses or any(value not in allowed for value in statuses):
            raise ValueError("Unsupported HP research status")
        predicate, args = _maps_hp_target_predicate(
            prefecture, medical_types, True,
            clinic_relation="public.clinic_cloudrun_readonly",
            include_verified=True,
        )
        status_expr = ("CASE WHEN c.hp_status='VERIFIED' OR h.fetch_status='OK' THEN 'SUCCESS' "
                       "WHEN h.clinic_id IS NULL THEN 'UNRESEARCHED' "
                       "WHEN h.fetch_status='REVIEW' THEN 'REVIEW' "
                       "ELSE 'ERROR' END")
        predicate += f" AND ({status_expr})=ANY(%s::text[])"
        args.append(statuses)
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM public.clinic_cloudrun_readonly c LEFT JOIN hp_research.clinic_hp_research h "
                "ON h.clinic_id=c.id WHERE " + predicate, args,
            )
            return cur.fetchone()[0]

    @_retry_closed_read_connection
    def hp_recent_jobs(self):
        """Read HP job headers through the verified Reader connection."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM research.research_jobs WHERE status<>'RESET' "
                "ORDER BY created_at DESC,id DESC LIMIT 20"
            )
            names = [item.name for item in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]

    @_retry_closed_read_connection
    def hp_normal_recent_jobs(self):
        """Minimal HP-only job list for the Mac normal Reader UI."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id,kind,status,created_at FROM research.research_jobs "
                "WHERE kind='hp' AND status<>'RESET' "
                "ORDER BY created_at DESC,id DESC LIMIT 20"
            )
            names = [item.name for item in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]

    @_retry_closed_read_connection
    def hp_job_status(self, job_id):
        with self._conn.cursor() as cur:
            # The Mac/Cloud Reader has column-level grants. Never use SELECT * here:
            # JobRunner polls only these fields, and its UI derives progress from items.
            cur.execute(
                "SELECT id,kind,status,created_at FROM research.research_jobs WHERE id=%s",
                (job_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("調査履歴が見つかりません。")
            result = dict(zip([item.name for item in cur.description], row))
            cur.execute(
                "SELECT state,count(*) FROM research.research_job_items "
                "WHERE job_id=%s GROUP BY state", (job_id,),
            )
            result["counts"] = dict(cur.fetchall())
            cur.execute(
                "SELECT result,count(*) FROM research.research_job_items "
                "WHERE job_id=%s AND state='DONE' GROUP BY result", (job_id,),
            )
            result["results"] = dict(cur.fetchall())
            result["total"] = sum(result["counts"].values())
            return result

    @_retry_closed_read_connection
    def hp_normal_job_status(self, job_id):
        """Progress summary using only the columns shown in the HP-only UI."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id,kind,status,created_at FROM research.research_jobs "
                "WHERE id=%s AND kind='hp'", (job_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("HP調査履歴が見つかりません。")
            result = dict(zip([item.name for item in cur.description], row))
            cur.execute(
                "SELECT state,count(*) FROM research.research_job_items "
                "WHERE job_id=%s GROUP BY state", (job_id,),
            )
            result["counts"] = dict(cur.fetchall())
            cur.execute(
                "SELECT result,count(*) FROM research.research_job_items "
                "WHERE job_id=%s AND state='DONE' GROUP BY result", (job_id,),
            )
            result["results"] = dict(cur.fetchall())
            result["total"] = sum(result["counts"].values())
            return result

    @_retry_closed_read_connection
    def hp_normal_job_results(self, job_id):
        """Read per-clinic job outcomes and published HP fields via the dedicated Reader."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT i.clinic_id,c.clinic_name,c.address,i.state,i.result,h.fetch_status,"
                "h.hp_url,h.final_url,h.machine_hp_rank,c.hp_status,c.hp_rank "
                "FROM research.research_job_items i "
                "JOIN public.clinic_cloudrun_readonly c ON c.id=i.clinic_id "
                "LEFT JOIN hp_research.clinic_hp_research h ON h.clinic_id=i.clinic_id "
                "WHERE i.job_id=%s ORDER BY i.clinic_id", (job_id,),
            )
            return [{
                "clinic_id": row[0], "clinic_name": row[1], "address": row[2],
                "item_state": row[3], "item_result": row[4], "fetch_status": row[5],
                "hp_url": row[6], "final_url": row[7], "machine_hp_rank": row[8],
                "published_hp_status": row[9], "published_hp_rank": row[10],
            } for row in cur.fetchall()]

    @_retry_closed_read_connection
    def hp_auto_run_state(self):
        with self._conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config.settings WHERE key=%s", ("hp_auto_run_state",))
            row = cur.fetchone()
        if not row:
            return {"status": "IDLE"}
        value = row[0]
        return value if isinstance(value, dict) else json.loads(value)

    @_retry_closed_read_connection
    def hp_running_job_count(self):
        """Read the standalone HP RUNNING-job blocker without expanding Reader scope."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM research.research_jobs WHERE kind='hp' AND status='RUNNING'"
            )
            return int(cur.fetchone()[0])

    @_retry_closed_read_connection
    def hp_auto_run_cumulative_results(self, run_id):
        with self._conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config.settings WHERE key=%s", ("hp_auto_run_state",))
            row = cur.fetchone()
        if not row:
            return {}
        value = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        if value.get("run_id") != run_id:
            return {}
        results = value.get("cumulative_results") or {}
        return {str(key): int(count) for key, count in results.items()}

    @_retry_closed_read_connection
    def hp_auto_run_remaining_count(self, state):
        predicate, args = _maps_hp_target_predicate(
            state.get("prefecture", ""), state.get("medical_types"), state.get("force", False),
            clinic_relation="public.clinic_cloudrun_readonly",
        )
        predicate += (
            " AND NOT EXISTS (SELECT 1 FROM research.research_job_items success_i "
            "JOIN research.research_jobs success_j ON success_j.id=success_i.job_id "
            "WHERE success_i.clinic_id=c.id AND success_i.state='DONE' "
            "AND success_i.result::text ILIKE '%%SUCCESS%%' AND success_j.kind='hp')"
        )
        with self._conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.clinic_cloudrun_readonly c WHERE " + predicate, args)
            return int(cur.fetchone()[0])

    @_retry_closed_read_connection
    def hp_auto_run_eligible_count(self, prefecture="", medical_types=None):
        """Count fresh Auto Run targets, excluding historical successful HP Jobs."""
        predicate, args = _maps_hp_target_predicate(
            prefecture, medical_types, False,
            clinic_relation="public.clinic_cloudrun_readonly",
        )
        predicate += (
            " AND NOT EXISTS (SELECT 1 FROM research.research_job_items prior_i "
            "JOIN research.research_jobs prior_j ON prior_j.id=prior_i.job_id "
            "WHERE prior_i.clinic_id=c.id AND prior_i.state='DONE' "
            "AND prior_i.result::text ILIKE '%%SUCCESS%%' AND prior_j.kind='hp')"
        )
        with self._conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.clinic_cloudrun_readonly c WHERE " + predicate, args)
            return int(cur.fetchone()[0])

    def mhlw_join_status(self):
        # The optional Navi sidecar never existed and was explicitly excluded from migration.
        return None

    @_retry_closed_read_connection
    def revision(self):
        with self._conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(id),0) FROM provenance.change_history")
            return cur.fetchone()[0]

    @_retry_closed_read_connection
    def google_maps_queue_csv(self):
        from src.master.google_maps import queue_csv
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT clinic_id FROM provenance.source_records "
                "WHERE source='厚生局' AND clinic_id IS NOT NULL ORDER BY clinic_id"
            )
            ids = [r[0] for r in cur.fetchall()]
        records, seen = [], set()
        for record in self.repositories.clinics._batch_get(ids):
            if record["id"] not in seen:
                seen.add(record["id"]); records.append(record)
        return queue_csv(records)

    def _export_rows(self, filters, as_of=None):
        records = self.query(filters, limit=100000, as_of=as_of)
        return self._export_records(records)

    def _export_records(self, records, *, read_only=False):
        ids = [r["id"] for r in records]
        originals, templates = {}, {}
        if ids:
            with self._conn.cursor() as cur:
                source = ("provenance.clinic_cloudrun_comdesk_rows" if read_only
                          else "provenance.comdesk_original_rows")
                projection_column = ",projection_valid" if read_only else ""
                cur.execute(
                    f"SELECT clinic_id,template_id,row_json,uuid{projection_column} FROM {source} "
                    "WHERE clinic_id=ANY(%s) ORDER BY clinic_id,(uuid<>'') DESC,id", (ids,),
                )
                for row in cur.fetchall():
                    if read_only and row[4] is not True:
                        raise ValueError("Comdesk元行の投影列を確認できないためREAD ONLY出力を中止しました。")
                    cid, template_id, row_json, uuid = row[:4]
                    originals.setdefault(cid, []).append((template_id, row_json, uuid))
                cur.execute("SELECT id,headers_json,mapping_json FROM provenance.templates")
                for tid, headers, mapping in cur.fetchall():
                    templates[tid] = (_json(headers), _json(mapping))
        output = []
        for record in records:
            choices = originals.get(record["id"], [])
            if not choices:
                output.append(fixed_row(record)); continue
            chosen = next((r for r in choices if not record.get("uuid") or r[2] == record["uuid"]), None)
            if chosen is None:
                raise ValueError("既存UUIDに対応する元行を確認できません。元データを確認してください。")
            headers, mapping = templates[chosen[0]]
            output.append(fixed_row(record, headers, mapping, _json(chosen[1])))
        return output

    def _attach_export_metadata(self, records, rows, *, include_unverified_comdesk_email=True):
        """Append formal outbound-only HP rank and official-verified email columns.

        The stored/imported Comdesk row remains the legacy 28-column source row.
        These two values are derived from current system-of-record data at export time.
        """
        emails = self._verified_emails_for_ids([record["id"] for record in records])
        ids = [int(record["id"]) for record in records]
        if include_unverified_comdesk_email and ids and self._conn.__class__.__module__.startswith("psycopg"):
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT id,base_json->>'comdesk_unverified_email' FROM public.clinics WHERE id=ANY(%s::bigint[])",
                    (ids,),
                )
                for clinic_id, email in cur.fetchall():
                    if email and int(clinic_id) not in emails:
                        emails[int(clinic_id)] = str(email).strip()
        output = []
        for record, row in zip(records, rows):
            manual_fields = record.get("manual_fields") or ()
            if "hp_rank" in manual_fields:
                rank_value = record.get("hp_rank")
            else:
                rank_value = record.get("comdesk_sync_hp_rank") or record.get("effective_hp_rank")
            rank = str(rank_value or "").strip().upper()
            if rank not in {"A", "B", "C", "D"}:
                rank = ""
            output.append([*row, rank, emails.get(int(record["id"]), "")])
        return output

    def _verified_emails_for_ids(self, clinic_ids):
        """Read already-enriched, exportable emails; database errors must remain visible."""
        ids = [int(clinic_id) for clinic_id in clinic_ids]
        if not ids:
            return {}
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT clinic_id,email,status,verified_on_official "
                "FROM public.clinic_email_enrichment "
                "WHERE clinic_id=ANY(%s) "
                "AND status='VERIFIED_EMAIL' "
                "AND verified_on_official=true "
                "ORDER BY clinic_id,lower(btrim(email)),btrim(email)",
                (ids,),
            )
            rows = cur.fetchall()
        # Intentionally no exception fallback: permission/SQL/connection failures must not be
        # misreported as clinics with no email.
        return _verified_email_map(rows)

    @_retry_closed_read_connection
    def latest_completed_hp_job(self):
        """Return the latest fully completed HP job; never fall back to historical clinics."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT j.id,j.created_at,count(i.clinic_id) AS target_count, "
                "count(*) FILTER (WHERE i.state='DONE') AS done_count "
                "FROM research.research_jobs j "
                "JOIN research.research_job_items i ON i.job_id=j.id "
                "WHERE j.kind='hp' AND j.status='COMPLETED' "
                "GROUP BY j.id,j.created_at "
                "HAVING count(i.clinic_id)>0 "
                "AND count(*) FILTER (WHERE i.state<>'DONE')=0 "
                "ORDER BY j.created_at DESC,j.id DESC LIMIT 1"
            )
            row = cur.fetchone()
        if not row:
            return None
        return {"id": row[0], "created_at": row[1], "updated_at": None,
                "target_count": row[2], "done_count": row[3]}

    @_retry_closed_read_connection
    def hp_job_export_summary(self, job_id):
        """Current-job metrics plus cumulative UUID-empty HPs, including Human Review positives."""
        exclusion = "(c.exclude_reason IN ('hospital','center') " \
            "OR COALESCE(substring(c.effective_json from %s),'')='病院' " \
            "OR c.clinic_name LIKE '%%病院%%' OR c.clinic_name LIKE '%%センター%%')"
        auto_reverified = (
            "COALESCE(replace(rr.result_json, chr(92) || 'u0000', '')::jsonb->>'hp_identity_source','') "
            "IN ('AUTO_NAME_ONLY_MISMATCH','AUTO_PHONE_ADDRESS_MATCH')"
        )
        eligible = (
            "(i.result='SUCCESS' OR (i.result='REVIEW' AND ("
            "hr.human_decision IN ('OFFICIAL','ORGANIZATION_PAGE','ACCESS_RESTRICTED') OR "
            + auto_reverified + ")))"
        )
        with self._conn.cursor() as cur:
            # Keep one latest Human Review label per clinic/job. Automatic REVIEW history is
            # preserved; a positive human decision upgrades export eligibility without rewriting
            # the original job-item result.
            cur.execute(
                "WITH human_latest AS ("
                " SELECT DISTINCT ON (clinic_id,research_job_id) "
                " clinic_id,research_job_id,human_decision "
                " FROM provenance.hp_human_reviews "
                " ORDER BY clinic_id,research_job_id,reviewed_at DESC,id DESC"
                ") "
                "SELECT count(*), "
                "count(*) FILTER (WHERE i.state='DONE'), "
                "count(*) FILTER (WHERE i.state='DONE' AND " + eligible + " "
                "AND h.fetch_status='OK' AND COALESCE(BTRIM(h.final_url),'')<>''), "
                "count(*) FILTER (WHERE i.state='DONE' AND " + eligible + " "
                "AND h.fetch_status='OK' AND COALESCE(BTRIM(h.final_url),'')<>'' "
                "AND COALESCE(BTRIM(c.uuid),'')<>''), "
                "COALESCE(array_agg(i.clinic_id ORDER BY i.clinic_id) FILTER (WHERE "
                "i.state='DONE' AND " + eligible + " AND h.fetch_status='OK' "
                "AND COALESCE(BTRIM(h.final_url),'')<>'' AND COALESCE(BTRIM(c.uuid),'')='' "
                "AND c.merged_into IS NULL AND c.merge_hold=false AND NOT " + exclusion + "),'{}') "
                "FROM research.research_job_items i "
                "JOIN research.research_jobs j ON j.id=i.job_id AND j.kind='hp' AND j.status='COMPLETED' "
                "LEFT JOIN public.clinics c ON c.id=i.clinic_id "
                "LEFT JOIN hp_research.clinic_hp_research h ON h.clinic_id=i.clinic_id "
                "LEFT JOIN research.research_results rr ON rr.clinic_id=i.clinic_id "
                "LEFT JOIN human_latest hr ON hr.clinic_id=i.clinic_id AND hr.research_job_id=i.job_id "
                "WHERE i.job_id=%s", ('"facility_type":"([^"]*)"', job_id),
            )
            target, done, success, uuid_existing, current_ids = cur.fetchone()

            # Cumulative waiting list includes:
            #   1) automatic SUCCESS; and
            #   2) automatic REVIEW later confirmed by Human Review.
            # In both cases the HP ledger must be OK after content reanalysis, so a human label
            # alone never enters Comdesk before the selected URL was actually fetched/analyzed.
            cur.execute(
                "WITH human_latest AS ("
                " SELECT DISTINCT ON (clinic_id,research_job_id) "
                " clinic_id,research_job_id,human_decision "
                " FROM provenance.hp_human_reviews "
                " ORDER BY clinic_id,research_job_id,reviewed_at DESC,id DESC"
                ") "
                "SELECT COALESCE(array_agg(DISTINCT c.id ORDER BY c.id),'{}') "
                "FROM public.clinics c "
                "JOIN research.research_job_items i ON i.clinic_id=c.id "
                "JOIN research.research_jobs j ON j.id=i.job_id AND j.kind='hp' "
                "JOIN hp_research.clinic_hp_research h ON h.clinic_id=c.id "
                "LEFT JOIN research.research_results rr ON rr.clinic_id=c.id "
                "LEFT JOIN human_latest hr ON hr.clinic_id=c.id AND hr.research_job_id=i.job_id "
                "WHERE i.state='DONE' AND " + eligible + " "
                "AND h.fetch_status='OK' "
                "AND COALESCE(BTRIM(h.final_url),'')<>'' "
                "AND COALESCE(BTRIM(c.uuid),'')='' "
                "AND c.merged_into IS NULL AND c.merge_hold=false AND NOT " + exclusion,
                ('"facility_type":"([^"]*)"',),
            )
            waiting_ids = list(cur.fetchone()[0] or [])

        current_set = set(current_ids or [])
        carryover_count = sum(1 for clinic_id in waiting_ids if clinic_id not in current_set)
        return {"target_count": target, "done_count": done, "success_count": success,
                "uuid_existing_count": uuid_existing, "export_ids": waiting_ids,
                "carryover_count": carryover_count}

    @_retry_closed_read_connection
    def export_hp_job(self, job_id):
        """Render the cumulative successful UUID-empty Comdesk waiting list."""
        summary = self.hp_job_export_summary(job_id)
        ids = summary["export_ids"]
        records = self.repositories.clinics._batch_get(ids) if ids else []
        rows = self._attach_export_metadata(records, self._export_records(records))
        import pandas as pd
        frame = pd.DataFrame(rows, columns=COMDESK_EXPORT_HEADERS)
        return {
            "final_comdesk_import.xlsx": xlsx_bytes({"営業対象": frame}),
            "final_comdesk_import.csv": csv_bytes(COMDESK_EXPORT_HEADERS, rows),
        }

    @_retry_closed_read_connection
    def export(self, filters, template_id=None, as_of=None):
        records = self.query(filters, limit=100000, as_of=as_of)
        rows = self._attach_export_metadata(records, self._export_records(records))
        import pandas as pd
        frame = pd.DataFrame(rows, columns=COMDESK_EXPORT_HEADERS)
        return {
            "final_comdesk_import.xlsx": xlsx_bytes({"営業対象": frame}),
            "final_comdesk_import.csv": csv_bytes(COMDESK_EXPORT_HEADERS, rows),
        }

    @_retry_closed_read_connection
    def export_read_only(self, filters, template_id=None, as_of=None):
        """Render the standard sales export using only the READ ONLY allowlist.

        Keep source Comdesk values/mappings and verified-email metadata identical to the
        ordinary exporter. The caller deliberately avoids ``revision()`` because that cache
        token reads provenance.change_history, which is outside the Cloud Run reader scope.
        This method performs no writes and does not inspect job, audit, or delivery state.
        """
        count_before = self.count_read_only_ui(filters, as_of=as_of)
        if count_before > 100000:
            raise ValueError(
                f"READ ONLY出力は100,000件までです（現在 {count_before:,}件）。条件を絞ってください。"
            )
        records = self.query_read_only_ui(filters, limit=100000, as_of=as_of)
        count_after = self.count_read_only_ui(filters, as_of=as_of)
        if count_after != len(records):
            raise ValueError(
                "出力中に対象件数が変わったため、欠落の可能性を避けてCSV/Excel出力を中止しました。"
            )
        rows = self._attach_export_metadata(
            records, self._export_records(records, read_only=True), include_unverified_comdesk_email=False,
        )
        for row in rows:
            for header in READ_ONLY_PENDING_EXPORT_HEADERS:
                row[COMDESK_EXPORT_HEADERS.index(header)] = ""
        import pandas as pd
        frame = pd.DataFrame(rows, columns=COMDESK_EXPORT_HEADERS)
        return {
            "final_comdesk_import.xlsx": xlsx_bytes({"営業対象": frame}),
            "final_comdesk_import.csv": csv_bytes(COMDESK_EXPORT_HEADERS, rows),
        }

    @_retry_closed_read_connection
    def export_management_csv(self):
        rows = self.query(Filters(active_only=False, hp_only=False), limit=100000)
        keys = sorted({k for row in rows for k in row})
        values = [[row.get(k, "") if not isinstance(row.get(k), (list, dict)) else
                   json.dumps(row.get(k), ensure_ascii=False) for k in keys] for row in rows]
        return csv_bytes(keys, values)


def _json(value):
    return json.loads(value) if isinstance(value, str) else value
