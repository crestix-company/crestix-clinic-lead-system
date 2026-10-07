"""SQLite adapter: thin delegation to the existing, unmodified ClinicStore and sidecar
functions. No SQL or business logic is reimplemented here -- every method is a passthrough,
so behavior is identical to today's app by construction.
"""
import json

from src.master.store import ClinicStore
from src.master.data_paths import production_db_path
from src.master.hp_effective_rank import hp_batch_path, load_machine_ranks
from src.master.hp_site_type import load_batch_urls, load_batch_treatments
from src.master.hp_batch_metrics import web_research_metrics
from src.master.research_sidecar import (
    RESEARCH_SIDECAR_QUALIFIED_TABLE, research_sidecar_available,
)


class SqliteClinicRepository:
    def __init__(self, store: ClinicStore):
        self._store = store

    def get(self, clinic_id):
        return self._store.get(clinic_id)

    def get_by_uuid(self, uuid):
        with self._store.connect() as c:
            row = c.execute(
                "SELECT id FROM clinics WHERE uuid=? AND merged_into IS NULL", (uuid,)
            ).fetchone()
        return self._store.get(row["id"]) if row else None

    def get_by_medical_key(self, medical_key):
        with self._store.connect() as c:
            row = c.execute(
                "SELECT id FROM clinics WHERE medical_key=? AND merged_into IS NULL", (medical_key,)
            ).fetchone()
        return self._store.get(row["id"]) if row else None

    def query(self, filters=None, limit=100, offset=0, as_of=None):
        return self._store.query(filters, limit, offset, as_of)

    def count(self, filters=None, as_of=None):
        return self._store.count(filters, as_of)

    def funnel(self, filters, as_of=None):
        return self._store.funnel(filters, as_of)

    def metrics(self):
        return self._store.metrics()


class SqliteHpResearchRepository:
    def __init__(self, batch_path=None, production_path=None):
        self._batch_path = batch_path or hp_batch_path()
        self._production_path = production_path or production_db_path()

    def machine_rank(self, clinic_id):
        return load_machine_ranks(self._batch_path).get(int(clinic_id), "UNKNOWN")

    def batch_urls(self, clinic_id):
        return load_batch_urls(self._batch_path).get(int(clinic_id), ("", ""))

    def website_treatment_categories(self, clinic_id):
        return load_batch_treatments(self._batch_path).get(int(clinic_id), [])

    def batch_metrics(self):
        return web_research_metrics(self._production_path, self._batch_path)


class SqliteTreatmentRepository:
    def __init__(self, store: ClinicStore):
        self._store = store

    def confirmed_categories(self, clinic_id):
        if not research_sidecar_available():
            return []
        with self._store.connect() as c:
            rows = c.execute(
                f"SELECT treatment_category_name FROM {RESEARCH_SIDECAR_QUALIFIED_TABLE} "
                "WHERE clinic_id=? AND research_status='CONFIRMED' ORDER BY treatment_category_name",
                (clinic_id,),
            ).fetchall()
        return [r[0] for r in rows]

    def status_for_ids(self, ids):
        return self._store.treatment_status_for_ids(ids)

    def status_counts(self, filters=None, as_of=None):
        return self._store.treatment_status_counts(filters, as_of)


class SqliteResearchRepository:
    def __init__(self, store: ClinicStore):
        self._store = store

    def research_result(self, clinic_id):
        with self._store.connect() as c:
            row = c.execute(
                "SELECT result_json FROM research_results WHERE clinic_id=?", (clinic_id,)
            ).fetchone()
        return json.loads(row[0]) if row else {}

    def hp_pages(self, clinic_id):
        with self._store.connect() as c:
            rows = c.execute(
                "SELECT url, page_json, checked_at FROM hp_pages WHERE clinic_id=? ORDER BY url",
                (clinic_id,),
            ).fetchall()
        return [{**json.loads(r[1]), "url": r[0], "checked_at": r[2]} for r in rows]


class SqliteProvenanceRepository:
    def __init__(self, store: ClinicStore):
        self._store = store

    def history_for_clinic(self, clinic_id, limit=30):
        return self._store.history(clinic_id, limit)

    def templates(self):
        return self._store.templates()

    def reviews(self, limit=100):
        return self._store.reviews(limit)


class SqliteSettingsRepository:
    def __init__(self, store: ClinicStore):
        self._store = store

    def get(self, key, default=None):
        return self._store.setting(key, default)

    def set(self, key, value):
        self._store.set_setting(key, value)
