"""Supabase-only facade used by the production application runtime.

This deliberately has no SQLite store, path, fallback, comparator, or sidecar attachment.
SQLite repositories remain available to explicit migration/admin/test callers through the
repository factories, but production UI and workers receive this object.
"""
from __future__ import annotations

import json
from functools import wraps

from src.io.output_writer import csv_bytes, xlsx_bytes
from src.master.comdesk import COMDESK_HEADERS, COMDESK_EXPORT_HEADERS
from src.master.fixed_export import fixed_row
from src.master.filters import Filters


from src.repository.hp_targets import maps_hp_target_predicate as _maps_hp_target_predicate



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
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:
            if not self._is_retryable_connection_error(exc):
                raise
            self._reconnect_read()
            return method(self, *args, **kwargs)
    return wrapped


class SupabaseRuntimeStore:
    is_supabase_runtime = True
    runtime_identity = "supabase"

    def __init__(self, repositories=None):
        self._owns_runtime_connection = repositories is None
        if repositories is None:
            from src.repository.backend import build_repositories
            repositories = build_repositories("supabase")
        self.repositories = repositories
        self._conn = repositories.clinics._conn

    def _reconnect_read(self):
        if not self._owns_runtime_connection:
            return
        old_conn = self._conn
        from src.repository.backend import build_repositories
        repositories = build_repositories("supabase")
        self.repositories = repositories
        self._conn = repositories.clinics._conn
        try:
            if old_conn is not None and not getattr(old_conn, "closed", False):
                old_conn.close()
        except Exception:
            pass

    def _ensure_read_connection(self):
        if not self._owns_runtime_connection:
            return
        if getattr(self._conn, "closed", False) or getattr(self._conn, "broken", False):
            self._reconnect_read()

    def _is_retryable_connection_error(self, exc):
        if not self._owns_runtime_connection:
            return False
        try:
            import psycopg
        except ImportError:
            return False
        return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))

    @property
    def path(self):
        """Compatibility identity only; never represents or opens a filesystem path."""
        return self.runtime_identity

    @_retry_closed_read_connection
    def get(self, clinic_id): return self.repositories.clinics.get(clinic_id)
    @_retry_closed_read_connection
    def get_by_uuid(self, value): return self.repositories.clinics.get_by_uuid(value)
    @_retry_closed_read_connection
    def get_by_medical_key(self, value): return self.repositories.clinics.get_by_medical_key(value)
    @_retry_closed_read_connection
    def query(self, filters=None, limit=100, offset=0, as_of=None):
        return self.repositories.clinics.query(filters, limit, offset, as_of)
    @_retry_closed_read_connection
    def count(self, filters=None, as_of=None): return self.repositories.clinics.count(filters, as_of)
    @_retry_closed_read_connection
    def funnel(self, filters, as_of=None): return self.repositories.clinics.funnel(filters, as_of)
    @_retry_closed_read_connection
    def metrics(self): return self.repositories.clinics.metrics()
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
    def web_research_metrics(self): return self.repositories.hp_research.batch_metrics()
    @_retry_closed_read_connection
    def history(self, clinic_id, limit=30):
        return self.repositories.provenance.history_for_clinic(clinic_id, limit)
    @_retry_closed_read_connection
    def templates(self): return self.repositories.provenance.templates()
    @_retry_closed_read_connection
    def reviews(self, limit=100): return self.repositories.provenance.reviews(limit)
    @_retry_closed_read_connection
    def setting(self, key, default=None): return self.repositories.settings.get(key, default)

    @_retry_closed_read_connection
    def prefectures(self):
        with self._conn.cursor() as cur:
            cur.execute("SELECT DISTINCT prefecture FROM public.clinics WHERE prefecture<>'' ORDER BY prefecture")
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
    def ad_count_max(self):
        from src.scoring.research_scoring import AD_SIGNAL_NAMES
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX((SELECT count(DISTINCT v) FROM "
                "jsonb_array_elements_text(signals_json) v WHERE v=ANY(%s))),0) "
                "FROM public.clinics", (list(AD_SIGNAL_NAMES),),
            )
            return cur.fetchone()[0]

    @_retry_closed_read_connection
    def has_maps(self):
        with self._conn.cursor() as cur:
            cur.execute("SELECT EXISTS(SELECT 1 FROM public.clinics WHERE maps_presence_status<>'')")
            return bool(cur.fetchone()[0])

    @_retry_closed_read_connection
    def maps_hp_candidate_ids(self, prefecture="", medical_types=None, force=False, limit=500):
        predicate, args = _maps_hp_target_predicate(prefecture, medical_types, force)
        args.append(min(500, max(1, int(limit))))
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT c.id FROM public.clinics c WHERE " + predicate +
                " ORDER BY c.is_new DESC,c.id LIMIT %s", args,
            )
            return [r[0] for r in cur.fetchall()]

    @_retry_closed_read_connection
    def maps_hp_available_count(self, prefecture="", medical_types=None, force=False):
        predicate, args = _maps_hp_target_predicate(prefecture, medical_types, force)
        with self._conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.clinics c WHERE " + predicate, args)
            return cur.fetchone()[0]

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

    def _export_records(self, records):
        ids = [r["id"] for r in records]
        originals, templates = {}, {}
        if ids:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT clinic_id,template_id,row_json,uuid FROM provenance.comdesk_original_rows "
                    "WHERE clinic_id=ANY(%s) ORDER BY clinic_id,(uuid<>'') DESC,id", (ids,),
                )
                for cid, template_id, row_json, uuid in cur.fetchall():
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

    def _attach_export_metadata(self, records, rows):
        """Append formal outbound-only HP rank and official-verified email columns.

        The stored/imported Comdesk row remains the legacy 28-column source row.
        These two values are derived from current system-of-record data at export time.
        """
        emails = self._verified_emails_for_ids([record["id"] for record in records])
        output = []
        for record, row in zip(records, rows):
            rank = str(record.get("effective_hp_rank") or "").strip().upper()
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
                "SELECT j.id,j.created_at,j.updated_at,count(i.clinic_id) AS target_count, "
                "count(*) FILTER (WHERE i.state='DONE') AS done_count "
                "FROM research.research_jobs j "
                "JOIN research.research_job_items i ON i.job_id=j.id "
                "WHERE j.kind='hp' AND j.status='COMPLETED' "
                "GROUP BY j.id,j.created_at,j.updated_at "
                "HAVING count(i.clinic_id)>0 "
                "AND count(*) FILTER (WHERE i.state<>'DONE')=0 "
                "ORDER BY j.updated_at DESC NULLS LAST,j.created_at DESC,j.id DESC LIMIT 1"
            )
            row = cur.fetchone()
        if not row:
            return None
        return {"id": row[0], "created_at": row[1], "updated_at": row[2],
                "target_count": row[3], "done_count": row[4]}

    @_retry_closed_read_connection
    def hp_job_export_summary(self, job_id):
        """Current-job metrics plus cumulative UUID-empty HPs, including Human Review positives."""
        exclusion = "(c.exclude_reason IN ('hospital','center') " \
            "OR COALESCE(substring(c.effective_json from %s),'')='病院' " \
            "OR c.clinic_name LIKE '%%病院%%' OR c.clinic_name LIKE '%%センター%%')"
        auto_reverified = (
            "COALESCE(replace(rr.result_json, chr(92) || 'u0000', '')::jsonb->>'hp_identity_source','')="
            "'AUTO_NAME_ONLY_MISMATCH'"
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
    def export_management_csv(self):
        rows = self.query(Filters(active_only=False, hp_only=False), limit=100000)
        keys = sorted({k for row in rows for k in row})
        values = [[row.get(k, "") if not isinstance(row.get(k), (list, dict)) else
                   json.dumps(row.get(k), ensure_ascii=False) for k in keys] for row in rows]
        return csv_bytes(keys, values)


def _json(value):
    return json.loads(value) if isinstance(value, str) else value
