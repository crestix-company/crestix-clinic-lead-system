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

import_maps_results is implemented (SupabaseProvenanceWriteRepository._find_target /
.import_maps_results). Its classification/anti-downgrade business logic (classify_match_status,
build_maps_update) is imported directly from src.master.google_maps -- the SAME function
objects the SQLite path calls -- rather than reimplemented, so the two backends cannot drift on
that rule. Only the SQL/I/O (find_target's queries) is a Postgres-specific port. See
docs/supabase_migration/23_stage4d_gate2_offline_preparation.md and
tests/test_stage4d_write_repository.py for the parity tests this relies on.
"""
import json

from psycopg.types.json import Jsonb

from src.master.store import now, dumps
from src.master.matching import medical_key
from src.master.identity_contract import (
    generate_medical_key_for_new_clinic, resolve_medical_key_transition,
    refresh_clinic_projection_preserving_identity,
)
from src.repository.high_range_id import assert_high_range
from src.repository.errors import BackendNotSupportedError


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

    @staticmethod
    def _find_target(cur, row):
        """Postgres port of src.master.google_maps.find_target(), same branch order and same
        tie-break rule (>=30 point gap, else AMBIGUOUS). effective_json is TEXT in Postgres
        (preserves SQLite JSON text losslessly, see schema_target.sql); cast to jsonb inline for
        the two ->> lookups, matching SQLite's json_extract(effective_json, '$....') exactly.
        """
        from src.master.google_maps import _s, _name_score, _addr_score
        from src.normalizer.phone import tel_match_key
        from src.normalizer.address import normalize_address
        from src.normalizer.clinic_name import normalize_clinic_name

        internal = _s(row.get("internal_clinic_id"))
        if internal.isdigit():
            cur.execute("SELECT id FROM public.clinics WHERE id=%s AND merged_into IS NULL", (int(internal),))
            hit = cur.fetchone()
            if hit:
                return int(hit[0]), "internal_clinic_id", 100
        med = _s(row.get("medical_institution_number"))
        if med:
            cur.execute(
                "SELECT id FROM public.clinics WHERE merged_into IS NULL AND "
                "(effective_json::jsonb)->>'medical_institution_number'=%s", (med,)
            )
            hits = cur.fetchall()
            if len(hits) == 1:
                return int(hits[0][0]), "medical_institution_number", 100
            cur.execute(
                "SELECT id FROM public.clinics WHERE merged_into IS NULL AND "
                "(effective_json::jsonb)->>'clinic_id'=%s", (med,)
            )
            hits = cur.fetchall()
            if len(hits) == 1:
                return int(hits[0][0]), "medical_institution_number", 100
        tk = tel_match_key(_s(row.get("source_phone"))) or tel_match_key(_s(row.get("maps_phone")))
        if tk:
            cur.execute(
                "SELECT id,name_norm,address_norm FROM public.clinics WHERE merged_into IS NULL AND tel_match_key=%s",
                (tk,),
            )
            hits = cur.fetchall()
            if len(hits) == 1:
                return int(hits[0][0]), "tel_match_key", 100
            if len(hits) > 1:
                scored = []
                for h in hits:
                    cur.execute("SELECT effective_json FROM public.clinics WHERE id=%s", (h[0],))
                    raw = cur.fetchone()[0]
                    d = raw if isinstance(raw, dict) else json.loads(raw)
                    scored.append((
                        _name_score(row.get("source_clinic_name"), d.get("clinic_name"))
                        + _addr_score(row.get("source_address"), d.get("address")),
                        int(h[0]),
                    ))
                scored.sort(reverse=True)
                if scored and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 30):
                    return scored[0][1], "tel_match_key", scored[0][0] / 2
                return None, "AMBIGUOUS", 0
        name = normalize_clinic_name(_s(row.get("source_clinic_name")))
        addr = normalize_address(_s(row.get("source_address")))
        if name and addr:
            cur.execute(
                "SELECT id FROM public.clinics WHERE merged_into IS NULL AND name_norm=%s AND address_norm=%s",
                (name, addr),
            )
            hits = cur.fetchall()
            if len(hits) == 1:
                return int(hits[0][0]), "name_address", 100
        return None, "NOT_FOUND", 0

    def import_maps_results(self, frame):
        """Postgres port of src.master.google_maps.import_maps_results(). The counts
        classification and the website-preservation decision are NOT reimplemented here --
        they call the exact same pure functions (classify_match_status / build_maps_update)
        that the SQLite path calls, so the anti-downgrade business rule cannot drift between
        backends. Only I/O (table names, placeholder style, jsonb vs text-json) differs.
        The whole batch is one transaction, matching the SQLite BEGIN IMMEDIATE scope exactly
        (either the entire batch commits, or none of it does).
        """
        from src.master.google_maps import validate_maps_frame, classify_match_status, build_maps_update, _s

        frame = validate_maps_frame(frame)
        records = frame.to_dict("records")
        import hashlib
        batch = hashlib.sha256(json.dumps(records, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        counts = {"TOTAL": len(records), "MATCHED": 0, "WEBSITE": 0, "NO_WEBSITE": 0, "NOT_FOUND": 0,
                  "AMBIGUOUS": 0, "EXCLUDED": 0, "ERROR": 0, "UNLINKED": 0, "PRESERVED_WEBSITE": 0}
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT result_json FROM provenance.import_batches WHERE id=%s", (batch,))
                old = cur.fetchone()
                if old:
                    self._conn.commit()
                    already = old[0] if isinstance(old[0], dict) else json.loads(old[0])
                    return {**already, "already_imported": True}
                for i, row in enumerate(records, start=2):
                    cid, method, score = self._find_target(cur, row)
                    maps_status = _s(row.get("maps_match_status"))
                    bucket = classify_match_status(maps_status)
                    if bucket in ("WEBSITE", "NO_WEBSITE"):
                        counts["MATCHED"] += 1
                        counts[bucket] += 1
                    elif bucket is not None:
                        counts[bucket] += 1
                    if cid is None:
                        counts["UNLINKED"] += 1
                    cur.execute(
                        "INSERT INTO provenance.google_maps_results"
                        "(clinic_id,batch_id,row_number,result_json,maps_match_status,maps_match_method,"
                        "maps_profile_url,maps_website_url,scraped_at,created_at) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (cid, batch, i, Jsonb(row), maps_status, _s(row.get("maps_match_method")) or method,
                         _s(row.get("maps_profile_url")), _s(row.get("maps_website_url")), _s(row.get("scraped_at")), now()),
                    )
                    if cid is not None:
                        cur.execute("SELECT base_json FROM public.clinics WHERE id=%s FOR UPDATE", (cid,))
                        raw_base = cur.fetchone()[0]
                        base = raw_base if isinstance(raw_base, dict) else json.loads(raw_base)
                        before = dict(base)
                        maps_update, preserved = build_maps_update(row, base, maps_status, method)
                        if preserved:
                            counts["PRESERVED_WEBSITE"] += 1
                        base.update(maps_update)
                        cur.execute("UPDATE public.clinics SET base_json=%s WHERE id=%s", (Jsonb(base), cid))
                        if before != base:
                            cur.execute(
                                "INSERT INTO provenance.change_history(clinic_id,action,before_json,after_json,note,created_at) "
                                "VALUES(%s,%s,%s,%s,%s,%s)",
                                (cid, "Google Maps取込", dumps(before), dumps(base),
                                 _s(row.get("maps_match_method")) or method, now()),
                            )
                        SupabaseClinicWriteRepository._refresh_projection_tx(cur, cid, is_authoritative_official_source=False)
                cur.execute(
                    "INSERT INTO provenance.import_batches(id,source,result_json,created_at) VALUES(%s,%s,%s,%s)",
                    (batch, "Google Maps", Jsonb(counts), now()),
                )
            self._conn.commit()
            return counts
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)


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

    @staticmethod
    def _refresh_projection_tx(cur, clinic_id, *, is_authoritative_official_source=False):
        """Cursor-level core of refresh_projection(), without its own commit/rollback -- so a
        caller that already holds an open transaction for a larger operation (e.g.
        SupabaseProvenanceWriteRepository.import_maps_results, which must commit/rollback the
        whole Maps batch atomically) can call this without prematurely committing.
        """
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
        # columns are always drawn from refresh_clinic_projection_preserving_identity()'s fixed
        # whitelist (plus the literal "medical_key") -- never user input -- so plain string
        # joining is safe here, matching ClinicStore._project()'s own convention in store.py.
        cur.execute(
            "UPDATE public.clinics SET " + ",".join(f"{c}=%s" for c in columns) + " WHERE id=%s",
            (*values, clinic_id),
        )

    def refresh_projection(self, clinic_id, *, is_authoritative_official_source=False):
        try:
            with self._conn.cursor() as cur:
                self._refresh_projection_tx(cur, clinic_id, is_authoritative_official_source=is_authoritative_official_source)
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

    def import_comdesk(self, table, mapping=None):
        raise BackendNotSupportedError(
            "Supabase adapterはimport_comdesk()に未対応です(DataFrame一括import/digest重複判定/"
            "match_record照合のPostgres移植とparity testがGate2本実装側の残課題です)。"
            "SQLite backendを使用してください。"
        )

    def import_master(self, frame):
        raise BackendNotSupportedError(
            "Supabase adapterはimport_master()に未対応です(import_comdeskと同じ理由)。"
            "SQLite backendを使用してください。"
        )

    def resolve_review(self, review_id, target_id=None, note=""):
        raise BackendNotSupportedError(
            "Supabase adapterはmerge_clinicsを伴うresolve_review()の統合経路に未対応です"
            "(reintegration.pyのmerge semantics移植が必要)。SQLite backendを使用してください。"
        )

    def refresh_age_model(self):
        raise BackendNotSupportedError(
            "Supabase adapterはrefresh_age_model()に未対応です。SQLite backendを使用してください。"
        )


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

    def create_job_from_filters(self, filters, kind="hp", limit=100, max_searches=100, force=False, max_pages=20):
        raise BackendNotSupportedError(
            "Supabase adapterはcreate_job_from_filters()に未対応です"
            "(src.master.filters.whereのPostgres翻訳はREAD側のsupabase_filters.pyにあるが、"
            "kind別のhp/epark/media WHERE節のGate2移植とparity testが先に必要です)。"
            "SQLite backendを使用してください。"
        )

    def recover_job_for_run(self, job_id):
        raise BackendNotSupportedError(
            "Supabase adapterはrecover_job_for_run()に未対応です(src.master.jobsのthread/worker"
            "orchestration移植がGate2本実装側の残課題です)。SQLite backendを使用してください。"
        )

    def complete_job_if_no_remaining_items(self, job_id):
        raise BackendNotSupportedError(
            "Supabase adapterはcomplete_job_if_no_remaining_items()に未対応です。"
            "SQLite backendを使用してください。"
        )

    def claim_specific_item(self, job_id, clinic_id):
        raise BackendNotSupportedError(
            "Supabase adapterはclaim_specific_item()に未対応です。SQLite backendを使用してください。"
        )

    def requeue_item_for_budget_or_pause(self, job_id, clinic_id, note, job_status):
        raise BackendNotSupportedError(
            "Supabase adapterはrequeue_item_for_budget_or_pause()に未対応です。"
            "SQLite backendを使用してください。"
        )

    def finish_item(self, job_id, clinic_id, status, note):
        raise BackendNotSupportedError(
            "Supabase adapterはfinish_item()に未対応です。SQLite backendを使用してください。"
        )

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

    def mark_item_state(self, job_id, clinic_id, state, *, result="", note=""):
        # result defaults to "" (not None): research.research_job_items.result is NOT NULL DEFAULT ''.
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

    def check_cache_or_reserve(self, query_key, job_id, month, monthly_limit, max_searches, used_in_session, force):
        """Postgres port of SqliteSearchWriteRepository.check_cache_or_reserve() -- same atomic
        budget-check-then-reserve transaction, same SELECT/UPDATE/INSERT sequence. app_config
        settings.value is TEXT (json dumps), not jsonb -- json.loads is required here, unlike
        the jsonb columns elsewhere in this file that psycopg parses automatically.
        """
        from src.enrichment.search_provider import BudgetReached
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT result_json FROM research.search_cache WHERE query_key=%s", (query_key,))
                cached = cur.fetchone()
                same_job = None
                if job_id:
                    cur.execute(
                        "SELECT 1 FROM research.search_usage u JOIN research.search_cache s USING(query_key) "
                        "WHERE u.job_id=%s AND u.query_key=%s AND s.searched_at>=u.attempted_at LIMIT 1",
                        (job_id, query_key),
                    )
                    same_job = cur.fetchone()
                if cached and (not force or same_job):
                    self._conn.commit()
                    return "cached", cached[0]
                cur.execute("SELECT count(*) FROM research.search_usage WHERE month=%s", (month,))
                used = cur.fetchone()[0]
                cur.execute("SELECT value FROM app_config.settings WHERE key='external_usage_reserve'")
                row = cur.fetchone()
                reserve = json.loads(row[0]) if row else 0
                cur.execute("SELECT value FROM app_config.settings WHERE key='monthly_limit'")
                row = cur.fetchone()
                monthly = min(monthly_limit, int(json.loads(row[0])) if row else monthly_limit)
                if used + int(reserve) >= monthly:
                    raise BudgetReached("月間検索上限です。残りの医院を保存して停止しました。")
                if job_id:
                    cur.execute(
                        "SELECT search_count,max_searches FROM research.research_jobs WHERE id=%s FOR UPDATE",
                        (job_id,),
                    )
                    job = cur.fetchone()
                    if not job or job[0] >= job[1]:
                        raise BudgetReached("今回の検索上限です。上限を変更すると続きから再開できます。")
                    cur.execute("UPDATE research.research_jobs SET search_count=search_count+1 WHERE id=%s", (job_id,))
                elif used_in_session >= max_searches:
                    raise BudgetReached("今回の検索上限です。")
                cur.execute(
                    "INSERT INTO research.search_usage(month,job_id,query_key,attempted_at) VALUES(%s,%s,%s,%s)",
                    (month, job_id, query_key, now()),
                )
            self._conn.commit()
            return "reserved", None
        except BudgetReached:
            self._conn.rollback()
            raise
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)

    def monthly_usage_count(self, month):
        with self._conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM research.search_usage WHERE month=%s", (month,))
            return cur.fetchone()[0]
