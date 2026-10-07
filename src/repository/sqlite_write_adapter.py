"""SQLite WRITE adapter for Stage4-D Gate2-A.

Two kinds of methods here:

  (a) Thin delegation to existing, unmodified ClinicStore/jobs.py/search_provider.py public
      methods -- zero behavior change, identical to how src/repository/sqlite_adapter.py (READ)
      delegates to ClinicStore today.
  (b) New target-contract methods (insert_new_clinic / update_matched_clinic_base /
      refresh_projection / resolve_review_to_*) that implement the Owner Decision 1 medical_key
      contract via src.master.identity_contract. These are ADDITIVE: nothing in src/master/
      store.py is imported-from-here-back or modified, and nothing in app_v2.py/jobs.py calls
      this module yet, so today's live SQLite runtime (ClinicStore._project()/._upsert(), still
      unconditionally rewriting medical_key) is completely unchanged. Wiring the live runtime to
      call this module instead is the deferred "direct SQLite WRITE closure" step, done at the
      live cutover session, not in this offline-prep pass.
"""
import json

from src.master.store import now, dumps
from src.master.matching import medical_key
from src.master.identity_contract import (
    generate_medical_key_for_new_clinic, resolve_medical_key_transition,
    refresh_clinic_projection_preserving_identity,
)


class SqliteClinicWriteRepository:
    def __init__(self, store):
        self._store = store

    def insert_new_clinic(self, record, *, is_new=False, is_authoritative_official_source=False):
        key = generate_medical_key_for_new_clinic(record, is_authoritative_official_source=is_authoritative_official_source)
        record_for_insert = {**record}
        if key:
            record_for_insert.setdefault("clinic_id", record.get("clinic_id", ""))
        ts = now()
        with self._store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            cid = c.execute(
                "INSERT INTO clinics(uuid,base_json,first_seen_at,last_seen_at,source_as_of_date,is_new) "
                "VALUES(?,?,?,?,?,?)",
                (record.get("uuid", ""), dumps(record), ts, ts, record.get("as_of", ""), int(is_new)),
            ).lastrowid
            c.execute("INSERT INTO change_history(clinic_id,action,before_json,after_json,note,created_at) "
                      "VALUES(?,?,?,?,?,?)", (cid, "医院追加", dumps({}), dumps(record), "", ts))
            if key:
                c.execute("UPDATE clinics SET medical_key=? WHERE id=?", (key, cid))
        return cid

    def update_matched_clinic_base(self, clinic_id, record, *, source, as_of):
        with self._store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM clinics WHERE id=?", (clinic_id,)).fetchone()
            base = json.loads(row["base_json"])
            before = dict(base)
            if source == "厚生局" and as_of >= row["source_as_of_date"]:
                base.update(record)
            else:
                base.update({k: v for k, v in record.items() if v and not base.get(k)})
            uid = row["uuid"] or record.get("uuid", "")
            ts = now()
            c.execute(
                "UPDATE clinics SET base_json=?,uuid=?,last_seen_at=?,"
                "source_as_of_date=CASE WHEN ?<source_as_of_date THEN source_as_of_date ELSE ? END WHERE id=?",
                (dumps(base), uid, ts, as_of, as_of, clinic_id),
            )
            if before != base:
                c.execute("INSERT INTO change_history(clinic_id,action,before_json,after_json,note,created_at) "
                          "VALUES(?,?,?,?,?,?)", (clinic_id, "マスター更新", dumps(before), dumps(base), "", ts))

    def refresh_projection(self, clinic_id, *, is_authoritative_official_source=False):
        with self._store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM clinics WHERE id=?", (clinic_id,)).fetchone()
            base = json.loads(row["base_json"])
            research = c.execute("SELECT result_json FROM research_results WHERE clinic_id=?", (clinic_id,)).fetchone()
            data = {**base, **(json.loads(research[0]) if research else {})}
            manual = {r["field"]: json.loads(r["value_json"]) for r in
                       c.execute("SELECT * FROM manual_overrides WHERE clinic_id=?", (clinic_id,))}
            data.update(manual)
            data["uuid"] = row["uuid"]
            data["manual_fields"] = list(manual)
            fields = refresh_clinic_projection_preserving_identity(data)
            candidate_key = medical_key({**data, **fields})
            resolved_key = resolve_medical_key_transition(
                row["medical_key"], candidate_key, authoritative=is_authoritative_official_source
            )
            fields["effective_json"] = dumps(fields["effective_json"])
            fields["departments_json"] = dumps(fields["departments_json"])
            fields["treatments_json"] = dumps(fields["treatments_json"])
            fields["signals_json"] = dumps(fields["signals_json"])
            if resolved_key != row["medical_key"]:
                fields["medical_key"] = resolved_key
            c.execute("UPDATE clinics SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?",
                      (*fields.values(), clinic_id))

    def resolve_review_to_new_clinic(self, record, *, is_authoritative_official_source=False):
        return self.insert_new_clinic(record, is_new=True, is_authoritative_official_source=is_authoritative_official_source)

    def resolve_review_to_existing_clinic(self, clinic_id, record, *, source):
        with self._store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM clinics WHERE id=?", (clinic_id,)).fetchone()
            base = json.loads(row["base_json"])
            if source == "厚生局":
                base.update(record)
            else:
                base.update({k: v for k, v in record.items() if v and not base.get(k)})
            c.execute("UPDATE clinics SET uuid=?,base_json=?,last_seen_at=? WHERE id=?",
                      (row["uuid"] or record.get("uuid", ""), dumps(base), now(), clinic_id))


class SqliteResearchWriteRepository:
    def __init__(self, store):
        self._store = store

    def save_research(self, clinic_id, result, pages=None):
        self._store.save_research(clinic_id, result, pages)

    def override(self, clinic_id, field, value, *, note="", source="手動確認"):
        self._store.override(clinic_id, field, value, note=note, source=source)


class SqliteProvenanceWriteRepository:
    def __init__(self, store):
        self._store = store

    def insert_source_record(self, clinic_id, source, record, source_hash, row_number, match_status, match_reason, match_score):
        with self._store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            return c.execute(
                "INSERT INTO source_records(clinic_id,source,record_json,source_hash,row_number,match_status,"
                "match_reason,match_score,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (clinic_id, source, dumps(record), source_hash, row_number, match_status, match_reason, match_score, now()),
            ).lastrowid

    def history(self, clinic_id, action, before, after, note=""):
        with self._store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            self._store._history(c, clinic_id, action, before, after, note)

    def import_maps_results(self, clinic_id, result, batch_hash):
        from src.master.google_maps import import_maps_results
        import_maps_results(self._store, [result], batch_hash)


class SqliteSettingsWriteRepository:
    def __init__(self, store):
        self._store = store

    def set(self, key, value):
        self._store.set_setting(key, value)


class SqliteJobsWriteRepository:
    def __init__(self, store):
        self._store = store

    def create_job(self, clinic_ids, kind, options, max_searches):
        import uuid
        jid = uuid.uuid4().hex
        with self._store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("INSERT INTO research_jobs(id,kind,options_json,max_searches,created_at,updated_at) "
                      "VALUES(?,?,?,?,?,?)", (jid, kind, dumps(options), max_searches, now(), now()))
            c.executemany("INSERT INTO research_job_items(job_id,clinic_id) VALUES(?,?)",
                          [(jid, i) for i in clinic_ids])
        return jid

    def pause_job(self, job_id):
        from src.master.jobs import pause_job
        pause_job(self._store, job_id)

    def reset_job(self, job_id):
        from src.master.jobs import reset_job
        reset_job(self._store, job_id)

    def job_limit(self, job_id, limit):
        from src.master.jobs import job_limit
        job_limit(self._store, job_id, limit)

    def claim_next_pending_item(self, job_id):
        with self._store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT clinic_id FROM research_job_items WHERE job_id=? AND state='PENDING' "
                "ORDER BY clinic_id LIMIT 1", (job_id,),
            ).fetchone()
            if row is None:
                return None
            c.execute("UPDATE research_job_items SET state='RUNNING' WHERE job_id=? AND clinic_id=?",
                      (job_id, row[0]))
            return row[0]

    def mark_item_state(self, job_id, clinic_id, state, *, result=None, note=""):
        with self._store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE research_job_items SET state=?,result=?,note=? WHERE job_id=? AND clinic_id=?",
                      (state, result, note, job_id, clinic_id))

    def mark_job_status(self, job_id, status):
        with self._store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE research_jobs SET status=?,updated_at=? WHERE id=?", (status, now(), job_id))


class SqliteSearchWriteRepository:
    def __init__(self, store):
        self._store = store

    def reserve_attempt(self, job_id, query_key, month):
        with self._store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if job_id:
                c.execute("UPDATE research_jobs SET search_count=search_count+1 WHERE id=?", (job_id,))
            c.execute("INSERT INTO search_usage(month,job_id,query_key,attempted_at) VALUES(?,?,?,?)",
                      (month, job_id, query_key, now()))

    def store_cache_result(self, query_key, query, result):
        with self._store.connect() as c:
            c.execute("INSERT OR REPLACE INTO search_cache VALUES(?,?,?,?)", (query_key, query, dumps(result), now()))

    def get_cached(self, query_key):
        with self._store.connect() as c:
            row = c.execute("SELECT result_json FROM search_cache WHERE query_key=?", (query_key,)).fetchone()
        return json.loads(row[0]) if row else None

    def monthly_usage_count(self, month):
        with self._store.connect() as c:
            return c.execute("SELECT count(*) FROM search_usage WHERE month=?", (month,)).fetchone()[0]
