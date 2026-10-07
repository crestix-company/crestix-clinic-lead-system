"""SQLite WRITE adapter for Stage4-D Gate2-A.

Two kinds of methods here:

  (a) Thin delegation to existing, unmodified ClinicStore/jobs.py/search_provider.py public
      methods -- zero behavior change, identical to how src/repository/sqlite_adapter.py (READ)
      delegates to ClinicStore today.
  (b) New target-contract methods (insert_new_clinic / update_matched_clinic_base /
      refresh_projection / resolve_review_to_*) that implement the Owner Decision 1 medical_key
      contract via src.master.identity_contract. These are NOT wired into app_v2.py/jobs.py --
      ClinicStore._project()/._upsert() (store.py, unmodified) remain the live code path for
      import/save_research/override/resolve_review/Maps-import; these two methods exist as the
      proven-equivalent target for Gate 2's eventual _project() replacement (see
      test_refresh_projection_parity_with_existing_project), not as something currently called.

app_v2.py/jobs.py/search_provider.py/google_maps.py DO call the (a)-class passthrough methods
below (import_comdesk/import_master/resolve_review/refresh_age_model/save_research/override/
set/job lifecycle/CachedSearch two-phase/import_maps_results) -- see
docs/supabase_migration/24_stage4d_live_cutover_readiness.md for exactly which call sites were
rewired. Every one of those delegates to the exact same ClinicStore/jobs.py/search_provider.py/
google_maps.py function, with the same arguments, so CLINIC_WRITE_BACKEND=sqlite (the default)
produces byte-identical behavior to before the rewiring -- only one more layer of indirection.
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

    def import_comdesk(self, table, mapping=None):
        return self._store.import_comdesk(table, mapping)

    def import_master(self, frame):
        return self._store.import_master(frame)

    def resolve_review(self, review_id, target_id=None, note=""):
        return self._store.resolve_review(review_id, target_id, note)

    def refresh_age_model(self):
        self._store.refresh_age_model()


class SqliteResearchWriteRepository:
    def __init__(self, store):
        self._store = store

    def save_research(self, clinic_id, result, pages=None):
        self._store.save_research(clinic_id, result, pages)

    def override(self, clinic_id, field, value, *, note="", source="手動確認"):
        self._store.override(clinic_id, field, value, note=note, source=source)

    def get_saved_research(self, clinic_id):
        with self._store.connect() as c:
            row = c.execute("SELECT result_json FROM research_results WHERE clinic_id=?", (clinic_id,)).fetchone()
        return json.loads(row[0]) if row else {}


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

    def import_maps_results(self, frame):
        from src.master.google_maps import import_maps_results
        return import_maps_results(self._store, frame)


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
        # Moved verbatim from src.master.jobs.pause_job -- that function now delegates here
        # (see jobs.py) so this is the single place the SQL exists, not a second copy of it.
        with self._store.connect() as c:
            c.execute("UPDATE research_jobs SET status='PAUSED',updated_at=? WHERE id=? AND status<>'COMPLETED'",
                      (now(), job_id))

    def reset_job(self, job_id):
        # Moved verbatim from src.master.jobs.reset_job.
        with self._store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT status FROM research_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("調査履歴が見つかりません。")
            if row[0] == "RUNNING":
                raise ValueError("実行中の調査はリセットできません。先に一時停止し、停止完了を待ってください。")
            c.execute("UPDATE research_job_items SET state='CANCELLED' WHERE job_id=? AND state='PENDING'", (job_id,))
            c.execute("UPDATE research_jobs SET status='RESET',updated_at=? WHERE id=?", (now(), job_id))

    def job_limit(self, job_id, limit):
        # Moved verbatim from src.master.jobs.job_limit.
        with self._store.connect() as c:
            c.execute("UPDATE research_jobs SET max_searches=?,updated_at=? WHERE id=?",
                      (max(0, int(limit)), now(), job_id))

    def job_status(self, job_id):
        with self._store.connect() as c:
            row = c.execute("SELECT * FROM research_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("調査履歴が見つかりません。")
            result = dict(row)
            result["counts"] = {item[0]: item[1] for item in c.execute(
                "SELECT state,count(*) FROM research_job_items WHERE job_id=? GROUP BY state", (job_id,))}
            result["results"] = {item[0]: item[1] for item in c.execute(
                "SELECT result,count(*) FROM research_job_items WHERE job_id=? AND state='DONE' GROUP BY result", (job_id,))}
            result["total"] = sum(result["counts"].values())
            return result

    def recent_jobs(self):
        with self._store.connect() as c:
            return [dict(row) for row in c.execute(
                "SELECT * FROM research_jobs WHERE status<>'RESET' ORDER BY created_at DESC,id DESC LIMIT 20")]

    def pending_site_candidates(self, job_id):
        with self._store.connect() as c:
            return [tuple(row) for row in c.execute(
                "SELECT i.clinic_id,c.maps_presence_status,c.maps_website_url "
                "FROM research_job_items i JOIN clinics c ON c.id=i.clinic_id "
                "WHERE i.job_id=? AND i.state='PENDING' ORDER BY i.clinic_id", (job_id,))]

    def create_job_from_filters(self, filters, kind="hp", limit=100, max_searches=100, force=False, max_pages=20):
        # Moved verbatim from src.master.jobs.create_job (the candidate SELECT and the job+item
        # INSERT stay in the same BEGIN IMMEDIATE transaction, matching current atomicity).
        import uuid
        from src.master.filters import where
        if kind not in {"hp", "epark", "media"}:
            raise ValueError("調査種類を確認してください。")
        limit = min(500, max(1, int(limit)))
        max_searches = min(1000, max(0, int(max_searches)))
        sql, args = where(filters)
        if not force:
            sql += {
                "hp": " AND hp_status='UNRESEARCHED'",
                "epark": " AND json_extract(effective_json,'$.epark_checked_at') IS NULL",
                "media": " AND json_extract(effective_json,'$.media_checked_at') IS NULL",
            }[kind]
        jid = uuid.uuid4().hex
        with self._store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            ids = [r[0] for r in c.execute(
                "SELECT id FROM clinics WHERE " + sql + " ORDER BY (uuid<>'') DESC,is_new DESC,id LIMIT ?",
                (*args, limit),
            )]
            if not ids:
                raise ValueError("指定した条件の未調査医院がありません。条件を見直すか、強制再調査を選択してください。")
            c.execute(
                "INSERT INTO research_jobs(id,kind,options_json,max_searches,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (jid, kind, dumps({"force": bool(force), "max_pages": max_pages}), max_searches, now(), now()),
            )
            c.executemany("INSERT INTO research_job_items(job_id,clinic_id) VALUES(?,?)", [(jid, i) for i in ids])
        return jid

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

    def mark_item_state(self, job_id, clinic_id, state, *, result="", note=""):
        # result defaults to "" (not None): research_job_items.result is NOT NULL DEFAULT ''.
        with self._store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE research_job_items SET state=?,result=?,note=? WHERE job_id=? AND clinic_id=?",
                      (state, result, note, job_id, clinic_id))

    def mark_job_status(self, job_id, status):
        with self._store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE research_jobs SET status=?,updated_at=? WHERE id=?", (status, now(), job_id))

    # -- The five methods below are moved verbatim from src.master.jobs._run_locked/_research_one
    # (see jobs.py, which now calls these instead of issuing the SQL inline). Each preserves the
    # exact original transaction boundary -- single-statement UPDATEs stay single-statement,
    # multi-statement atomic blocks stay atomic -- so CLINIC_WRITE_BACKEND=sqlite behavior is
    # byte-identical to before this rewiring.

    def recover_job_for_run(self, job_id):
        """Startup recovery for run_job(): any item left RUNNING by a crashed/killed previous
        process goes back to PENDING, then the job transitions to RUNNING. Returns the job row
        (as a dict) read before any update, or None if the job is already COMPLETED/RESET
        (in which case nothing is updated and the caller should do nothing further)."""
        with self._store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM research_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("調査履歴が見つかりません。")
            job = dict(row)
            if job["status"] in {"COMPLETED", "RESET"}:
                return None
            c.execute("UPDATE research_job_items SET state='PENDING' WHERE job_id=? AND state='RUNNING'", (job_id,))
            c.execute("UPDATE research_jobs SET status='PAUSED' WHERE id=? AND status='RUNNING'", (job_id,))
            c.execute("UPDATE research_jobs SET status='RUNNING',updated_at=? WHERE id=?", (now(), job_id))
        return job

    def complete_job_if_no_remaining_items(self, job_id):
        with self._store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute(
                "SELECT 1 FROM research_job_items WHERE job_id=? AND state IN ('PENDING','RUNNING') LIMIT 1",
                (job_id,),
            ).fetchone() is None:
                c.execute("UPDATE research_jobs SET status='COMPLETED',updated_at=? WHERE id=? AND status='RUNNING'",
                          (now(), job_id))

    def claim_specific_item(self, job_id, clinic_id):
        """Claim one named clinic_id (used by the parallel per-site-lane worker, which already
        knows which clinic_id it's assigned) -- distinct from claim_next_pending_item(), which
        picks whichever PENDING row sorts first. Returns True iff this call claimed it."""
        with self._store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            return c.execute(
                "UPDATE research_job_items SET state='RUNNING' WHERE job_id=? AND clinic_id=? AND state='PENDING'",
                (job_id, clinic_id),
            ).rowcount > 0

    def requeue_item_for_budget_or_pause(self, job_id, clinic_id, note, job_status):
        """BudgetReached/Stopped path: the item goes back to PENDING (not DONE -- it was never
        actually attempted) with a note, and the job status reflects why the run stopped."""
        with self._store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE research_job_items SET state='PENDING',note=? WHERE job_id=? AND clinic_id=?",
                      (note, job_id, clinic_id))
            c.execute("UPDATE research_jobs SET status=?,updated_at=? WHERE id=?", (job_status, now(), job_id))

    def finish_item(self, job_id, clinic_id, status, note):
        """Terminal state for one item (success or error) -- job status itself is untouched,
        only its updated_at timestamp, matching the original 2-statement atomic block."""
        with self._store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE research_job_items SET state='DONE',result=?,note=? WHERE job_id=? AND clinic_id=?",
                      (status, note, job_id, clinic_id))
            c.execute("UPDATE research_jobs SET updated_at=? WHERE id=?", (now(), job_id))


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

    def check_cache_or_reserve(self, query_key, job_id, month, monthly_limit, max_searches, used_in_session, force):
        """Moved verbatim from src.enrichment.search_provider.CachedSearch.search()'s
        pre-network-call transaction: cache lookup, same_job replay check, monthly/job/session
        budget checks, and the reservation INSERT are all one BEGIN IMMEDIATE, exactly as
        before -- splitting the budget check from the reservation into two transactions would
        let two concurrent calls both pass the check and jointly overrun the monthly quota.
        Returns ("cached", result) or ("reserved", None). Raises BudgetReached over budget.
        """
        from src.enrichment.search_provider import BudgetReached
        with self._store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            cached = c.execute("SELECT result_json FROM search_cache WHERE query_key=?", (query_key,)).fetchone()
            same_job = job_id and c.execute(
                "SELECT 1 FROM search_usage u JOIN search_cache s USING(query_key) "
                "WHERE u.job_id=? AND u.query_key=? AND s.searched_at>=u.attempted_at LIMIT 1",
                (job_id, query_key),
            ).fetchone()
            if cached and (not force or same_job):
                return "cached", json.loads(cached[0])
            used = c.execute("SELECT count(*) FROM search_usage WHERE month=?", (month,)).fetchone()[0]
            reserve = self._store.setting("external_usage_reserve", 0)
            monthly = min(monthly_limit, int(self._store.setting("monthly_limit", 900)))
            if used + int(reserve) >= monthly:
                raise BudgetReached("月間検索上限です。残りの医院を保存して停止しました。")
            if job_id:
                job = c.execute("SELECT * FROM research_jobs WHERE id=?", (job_id,)).fetchone()
                if not job or job["search_count"] >= job["max_searches"]:
                    raise BudgetReached("今回の検索上限です。上限を変更すると続きから再開できます。")
                c.execute("UPDATE research_jobs SET search_count=search_count+1 WHERE id=?", (job_id,))
            elif used_in_session >= max_searches:
                raise BudgetReached("今回の検索上限です。")
            c.execute("INSERT INTO search_usage(month,job_id,query_key,attempted_at) VALUES(?,?,?,?)",
                      (month, job_id, query_key, now()))
        return "reserved", None
