"""Supabase-only facade used by the production application runtime.

This deliberately has no SQLite store, path, fallback, comparator, or sidecar attachment.
SQLite repositories remain available to explicit migration/admin/test callers through the
repository factories, but production UI and workers receive this object.
"""
from __future__ import annotations

import json

from src.io.output_writer import csv_bytes, xlsx_bytes
from src.master.comdesk import COMDESK_HEADERS
from src.master.fixed_export import fixed_row
from src.master.filters import Filters


def _maps_hp_target_predicate(prefecture="", medical_types=None, force=False):
    """Canonical Step4 target predicate shared by displayed count and job candidates."""
    conditions = [
        "c.merged_into IS NULL",
        "c.merge_hold=false",
        "c.active=true",
        "c.maps_presence_status='MAPS_MATCHED_WEBSITE'",
        "COALESCE(BTRIM(c.maps_website_url),'')<>''",
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


class SupabaseRuntimeStore:
    is_supabase_runtime = True
    runtime_identity = "supabase"

    def __init__(self, repositories=None):
        if repositories is None:
            from src.repository.backend import build_repositories
            repositories = build_repositories("supabase")
        self.repositories = repositories
        self._conn = repositories.clinics._conn

    @property
    def path(self):
        """Compatibility identity only; never represents or opens a filesystem path."""
        return self.runtime_identity

    def get(self, clinic_id): return self.repositories.clinics.get(clinic_id)
    def get_by_uuid(self, value): return self.repositories.clinics.get_by_uuid(value)
    def get_by_medical_key(self, value): return self.repositories.clinics.get_by_medical_key(value)
    def query(self, filters=None, limit=100, offset=0, as_of=None):
        return self.repositories.clinics.query(filters, limit, offset, as_of)
    def count(self, filters=None, as_of=None): return self.repositories.clinics.count(filters, as_of)
    def funnel(self, filters, as_of=None): return self.repositories.clinics.funnel(filters, as_of)
    def metrics(self): return self.repositories.clinics.metrics()
    def treatment_status_for_ids(self, ids): return self.repositories.treatment.status_for_ids(ids)
    def treatment_category_options(self):
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT treatment_category_name FROM treatment.clinic_treatment_research "
                "WHERE treatment_category_name<>'' ORDER BY treatment_category_name"
            )
            return [r[0] for r in cur.fetchall()]
    def web_research_metrics(self): return self.repositories.hp_research.batch_metrics()
    def history(self, clinic_id, limit=30):
        return self.repositories.provenance.history_for_clinic(clinic_id, limit)
    def templates(self): return self.repositories.provenance.templates()
    def reviews(self, limit=100): return self.repositories.provenance.reviews(limit)
    def setting(self, key, default=None): return self.repositories.settings.get(key, default)

    def prefectures(self):
        with self._conn.cursor() as cur:
            cur.execute("SELECT DISTINCT prefecture FROM public.clinics WHERE prefecture<>'' ORDER BY prefecture")
            return [r[0] for r in cur.fetchall()]

    def municipalities(self):
        # Keep the canonical Python extraction used by the Supabase filter post-pass.
        from src.normalizer.address import extract_municipality
        with self._conn.cursor() as cur:
            cur.execute("SELECT DISTINCT address FROM public.clinics WHERE address<>''")
            values = {extract_municipality(r[0]) for r in cur.fetchall()}
        return sorted(v for v in values if v)

    def ad_count_max(self):
        from src.scoring.research_scoring import AD_SIGNAL_NAMES
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX((SELECT count(DISTINCT v) FROM "
                "jsonb_array_elements_text(signals_json) v WHERE v=ANY(%s))),0) "
                "FROM public.clinics", (list(AD_SIGNAL_NAMES),),
            )
            return cur.fetchone()[0]

    def has_maps(self):
        with self._conn.cursor() as cur:
            cur.execute("SELECT EXISTS(SELECT 1 FROM public.clinics WHERE maps_presence_status<>'')")
            return bool(cur.fetchone()[0])

    def maps_hp_candidate_ids(self, prefecture="", medical_types=None, force=False, limit=500):
        predicate, args = _maps_hp_target_predicate(prefecture, medical_types, force)
        args.append(min(500, max(1, int(limit))))
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT c.id FROM public.clinics c WHERE " + predicate +
                " ORDER BY c.is_new DESC,c.id LIMIT %s", args,
            )
            return [r[0] for r in cur.fetchall()]

    def maps_hp_available_count(self, prefecture="", medical_types=None, force=False):
        predicate, args = _maps_hp_target_predicate(prefecture, medical_types, force)
        with self._conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.clinics c WHERE " + predicate, args)
            return cur.fetchone()[0]

    def mhlw_join_status(self):
        # The optional Navi sidecar never existed and was explicitly excluded from migration.
        return None

    def revision(self):
        with self._conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(id),0) FROM provenance.change_history")
            return cur.fetchone()[0]

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

    def hp_job_export_summary(self, job_id):
        """Counts and export IDs for one completed HP job, all from the canonical HP ledger."""
        exclusion = "(c.exclude_reason IN ('hospital','center') " \
            "OR COALESCE(substring(c.effective_json from %s),'')='病院' " \
            "OR c.clinic_name LIKE '%病院%' OR c.clinic_name LIKE '%センター%')"
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT count(*), "
                "count(*) FILTER (WHERE i.state='DONE'), "
                "count(*) FILTER (WHERE i.state='DONE' AND i.result='SUCCESS' "
                "AND h.fetch_status='OK' AND COALESCE(BTRIM(h.final_url),'')<>''), "
                "count(*) FILTER (WHERE i.state='DONE' AND i.result='SUCCESS' "
                "AND h.fetch_status='OK' AND COALESCE(BTRIM(h.final_url),'')<>'' "
                "AND COALESCE(BTRIM(c.uuid),'')<>''), "
                "COALESCE(array_agg(i.clinic_id ORDER BY i.clinic_id) FILTER (WHERE "
                "i.state='DONE' AND i.result='SUCCESS' AND h.fetch_status='OK' "
                "AND COALESCE(BTRIM(h.final_url),'')<>'' AND COALESCE(BTRIM(c.uuid),'')='' "
                "AND c.merged_into IS NULL AND c.merge_hold=false AND NOT " + exclusion + "),'{}') "
                "FROM research.research_job_items i "
                "JOIN research.research_jobs j ON j.id=i.job_id AND j.kind='hp' AND j.status='COMPLETED' "
                "LEFT JOIN public.clinics c ON c.id=i.clinic_id "
                "LEFT JOIN hp_research.clinic_hp_research h ON h.clinic_id=i.clinic_id "
                "WHERE i.job_id=%s", ('"facility_type":"([^"]*)"', job_id),
            )
            target, done, success, uuid_existing, ids = cur.fetchone()
        return {"target_count": target, "done_count": done, "success_count": success,
                "uuid_existing_count": uuid_existing, "export_ids": list(ids or [])}

    def export_hp_job(self, job_id):
        """Render only successful, UUID-empty clinics from the explicitly selected HP job."""
        summary = self.hp_job_export_summary(job_id)
        ids = summary["export_ids"]
        records = self.repositories.clinics._batch_get(ids) if ids else []
        rows = self._export_records(records)
        import pandas as pd
        frame = pd.DataFrame(rows, columns=COMDESK_HEADERS)
        return {
            "final_comdesk_import.xlsx": xlsx_bytes({"営業対象": frame}),
            "final_comdesk_import.csv": csv_bytes(COMDESK_HEADERS, rows),
        }

    def export(self, filters, template_id=None, as_of=None):
        rows = self._export_rows(filters, as_of)
        import pandas as pd
        frame = pd.DataFrame(rows, columns=COMDESK_HEADERS)
        return {
            "final_comdesk_import.xlsx": xlsx_bytes({"営業対象": frame}),
            "final_comdesk_import.csv": csv_bytes(COMDESK_HEADERS, rows),
        }

    def export_management_csv(self):
        rows = self.query(Filters(active_only=False, hp_only=False), limit=100000)
        keys = sorted({k for row in rows for k in row})
        values = [[row.get(k, "") if not isinstance(row.get(k), (list, dict)) else
                   json.dumps(row.get(k), ensure_ascii=False) for k in keys] for row in rows]
        return csv_bytes(keys, values)


def _json(value):
    return json.loads(value) if isinstance(value, str) else value
