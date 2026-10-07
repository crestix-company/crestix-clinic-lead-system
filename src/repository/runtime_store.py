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
        conditions = [
            "merged_into IS NULL", "merge_hold=false", "active=true",
            "maps_presence_status='MAPS_MATCHED_WEBSITE'", "maps_website_url<>''",
        ]
        args = []
        if prefecture:
            conditions.append("prefecture=%s"); args.append(prefecture)
        if medical_types:
            conditions.append("medical_type=ANY(%s)"); args.append(list(medical_types))
        if not force:
            conditions.append("hp_status='UNRESEARCHED'")
        args.append(min(500, max(1, int(limit))))
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM public.clinics WHERE " + " AND ".join(conditions) +
                " ORDER BY (uuid<>'') DESC,is_new DESC,id LIMIT %s", args,
            )
            return [r[0] for r in cur.fetchall()]

    def maps_hp_available_count(self, prefecture="", medical_types=None, force=False):
        conditions = [
            "merged_into IS NULL", "merge_hold=false", "active=true",
            "maps_presence_status='MAPS_MATCHED_WEBSITE'", "maps_website_url<>''",
        ]
        args = []
        if prefecture:
            conditions.append("prefecture=%s"); args.append(prefecture)
        if medical_types:
            conditions.append("medical_type=ANY(%s)"); args.append(list(medical_types))
        if not force:
            conditions.append("hp_status='UNRESEARCHED'")
        with self._conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.clinics WHERE " + " AND ".join(conditions), args)
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
