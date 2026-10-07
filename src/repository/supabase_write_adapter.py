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
import re

from psycopg.types.json import Jsonb

from src.master.store import now, dumps
from src.master.matching import medical_key
from src.master.identity_contract import (
    generate_medical_key_for_new_clinic, resolve_medical_key_transition,
    refresh_clinic_projection_preserving_identity,
)
from src.repository.high_range_id import assert_high_range


class _DictRows:
    def __init__(self, rows, names):
        self._rows = [dict(zip(names, row)) for row in rows]

    def __iter__(self):
        return iter(self._rows)


class _MatchingConnection:
    """Small read-only compatibility surface so the canonical match_record algorithm is
    shared by SQLite and PostgreSQL; only placeholders and schema-qualified table names differ.
    """
    _TABLES = {
        "clinics": "public.clinics",
        "comdesk_original_rows": "provenance.comdesk_original_rows",
    }

    def __init__(self, cur):
        self._cur = cur

    def execute(self, statement, args=()):
        sql = statement.replace("?", "%s")
        for table, qualified in self._TABLES.items():
            sql = re.sub(rf"(?<![.\w]){table}\b", qualified, sql)
        self._cur.execute(sql, args)
        names = [item.name for item in self._cur.description]
        return _DictRows(self._cur.fetchall(), names)


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
                previous = (row[0] if isinstance(row[0], dict) else json.loads(row[0])) if row else {}
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
                SupabaseClinicWriteRepository._refresh_projection_tx(
                    cur, clinic_id, is_authoritative_official_source=False
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
                SupabaseClinicWriteRepository._refresh_projection_tx(
                    cur, clinic_id, is_authoritative_official_source=False
                )
            self._conn.commit()
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)

    def get_saved_research(self, clinic_id):
        with self._conn.cursor() as cur:
            cur.execute("SELECT result_json FROM research.research_results WHERE clinic_id=%s", (clinic_id,))
            row = cur.fetchone()
        if not row:
            return {}
        return row[0] if isinstance(row[0], dict) else json.loads(row[0])


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
        tie-break rule (>=30 point gap, else AMBIGUOUS). Identity matching uses canonical
        base_json JSONB so losslessly preserved legacy effective_json TEXT (including escaped
        NUL values that PostgreSQL JSONB cannot represent) is never parsed by this path.
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
                "base_json->>'medical_institution_number'=%s", (med,)
            )
            hits = cur.fetchall()
            if len(hits) == 1:
                return int(hits[0][0]), "medical_institution_number", 100
            cur.execute(
                "SELECT id FROM public.clinics WHERE merged_into IS NULL AND "
                "base_json->>'clinic_id'=%s", (med,)
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
        research = ((research_row[0] if isinstance(research_row[0], dict) else json.loads(research_row[0]))
                    if research_row else {})
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
        bool_columns = {"active", "owner_equal"}
        values = [
            Jsonb(fields[c])
            if c in ("departments_json", "treatments_json", "signals_json")
            else (None if fields[c] is None else bool(fields[c]))
            if c in bool_columns
            else fields[c]
            for c in columns
        ]
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

    @staticmethod
    def _upsert_tx(cur, record, match, source, source_hash, row_number, *,
                   original=None, template_id=None, new_flag=False, existing_source_id=None):
        from src.normalizer.clinic_name import normalize_person
        timestamp = now()
        if match.status == "MATCHED":
            cid = match.candidates[0]
            cur.execute(
                "SELECT base_json,uuid,source_as_of_date FROM public.clinics WHERE id=%s FOR UPDATE", (cid,)
            )
            base, uuid_value, source_as_of_date = cur.fetchone()
            before = dict(base)
            if source == "厚生局" and record.get("as_of", "") >= source_as_of_date:
                base.update(record)
                if record.get("manager_name") and normalize_person(before.get("manager_name")) != normalize_person(record["manager_name"]):
                    cur.execute("SELECT result_json FROM research.research_results WHERE clinic_id=%s FOR UPDATE", (cid,))
                    old_research = cur.fetchone()
                    if old_research:
                        research = json.loads(old_research[0])
                        for key in list(research):
                            if key.startswith(("age_", "license_", "graduation_")) or key == "doctor_name":
                                research.pop(key, None)
                        research.update(
                            age_probability_under_59=None,
                            age_estimation_confidence="REVIEW",
                            age_estimation_reason="管理者が変更されています。現在の院長の経歴を再確認してください。",
                        )
                        cur.execute(
                            "UPDATE research.research_results SET result_json=%s,updated_at=%s WHERE clinic_id=%s",
                            (dumps(research), timestamp, cid),
                        )
            else:
                base.update({key: value for key, value in record.items() if value and not base.get(key)})
            cur.execute(
                "UPDATE public.clinics SET base_json=%s,uuid=%s,last_seen_at=%s,"
                "source_as_of_date=CASE WHEN %s<source_as_of_date THEN source_as_of_date ELSE %s END WHERE id=%s",
                (Jsonb(base), uuid_value or record.get("uuid", ""), timestamp,
                 record.get("as_of", ""), record.get("as_of", ""), cid),
            )
            if before != base:
                cur.execute(
                    "INSERT INTO provenance.change_history(clinic_id,action,before_json,after_json,note,created_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s)",
                    (cid, "マスター更新", dumps(before), dumps(base), match.reason, timestamp),
                )
        elif match.status == "NEW":
            cur.execute(
                "INSERT INTO public.clinics(uuid,base_json,first_seen_at,last_seen_at,source_as_of_date,is_new) "
                "VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
                (record.get("uuid", ""), Jsonb(record), timestamp, timestamp,
                 record.get("as_of", ""), bool(new_flag)),
            )
            cid = cur.fetchone()[0]
            assert_high_range(cid, table="public.clinics")
            cur.execute(
                "INSERT INTO provenance.change_history(clinic_id,action,before_json,after_json,note,created_at) "
                "VALUES(%s,%s,%s,%s,%s,%s)",
                (cid, "医院追加", dumps({}), dumps(record), match.reason, timestamp),
            )
        else:
            cid = None
        if existing_source_id is None:
            cur.execute(
                "INSERT INTO provenance.source_records(clinic_id,source,record_json,source_hash,row_number,"
                "match_status,match_reason,match_score,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (cid, source, Jsonb(record), source_hash, row_number, match.status,
                 match.reason, match.score, timestamp),
            )
            source_id = cur.fetchone()[0]
            assert_high_range(source_id, table="provenance.source_records")
        else:
            source_id = existing_source_id
            cur.execute(
                "UPDATE provenance.source_records SET clinic_id=%s,match_status=%s,match_reason=%s,match_score=%s WHERE id=%s",
                (cid, match.status, match.reason, match.score, source_id),
            )
        if original is not None:
            cur.execute(
                "INSERT INTO provenance.comdesk_original_rows(clinic_id,template_id,row_json,uuid,source_hash,row_number,created_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (cid, template_id, Jsonb(original), record.get("uuid", ""), source_hash, row_number, timestamp),
            )
        if cid is None:
            cur.execute(
                "SELECT id FROM provenance.match_reviews WHERE source_record_id=%s AND status='PENDING' FOR UPDATE",
                (source_id,),
            )
            pending = cur.fetchone()
            if pending:
                cur.execute(
                    "UPDATE provenance.match_reviews SET candidates_json=%s,updated_at=%s WHERE id=%s",
                    (Jsonb(match.candidates), timestamp, pending[0]),
                )
            else:
                cur.execute(
                    "INSERT INTO provenance.match_reviews(source_record_id,candidates_json,updated_at) VALUES(%s,%s,%s)",
                    (source_id, Jsonb(match.candidates), timestamp),
                )
        else:
            if existing_source_id is not None:
                cur.execute(
                    "UPDATE provenance.match_reviews SET status='RESOLVED',resolved_clinic_id=%s,note=%s,updated_at=%s "
                    "WHERE source_record_id=%s AND status='PENDING'",
                    (cid, "新しい電話番号キーで再照合", timestamp, source_id),
                )
            SupabaseClinicWriteRepository._refresh_projection_tx(
                cur, cid, is_authoritative_official_source=(source == "厚生局")
            )
        return cid

    def import_comdesk(self, table, mapping=None):
        from src.master.comdesk import infer_comdesk_columns, record_from_row
        from src.master.matching import match_record
        from src.master.store import digest
        mapping = dict(mapping or infer_comdesk_columns(table))
        if "uuid" not in mapping:
            hits = [i for i, header in enumerate(table.headers)
                    if header.strip().casefold() in {"uuid", "案件id", "管理id", "リードid", "lead_id", "id"}]
            mapping["uuid"] = hits[0] if len(hits) == 1 else None
        if mapping.get("clinic_name") is None:
            raise ValueError("医院名の列を指定してください。")
        if not all(value is None or isinstance(value, int) and 0 <= value < len(table.headers)
                   for value in mapping.values()):
            raise ValueError("対応する列番号を確認してください。")
        rows = table.data.values.tolist()
        batch = digest(["comdesk", table.headers, mapping, rows])
        template = digest([table.headers, mapping])
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT result_json FROM provenance.import_batches WHERE id=%s", (batch,))
                old = cur.fetchone()
                if old:
                    self._conn.commit()
                    return {**old[0], "already_imported": True}
                cur.execute(
                    "INSERT INTO provenance.templates(id,headers_json,mapping_json,created_at) VALUES(%s,%s,%s,%s) "
                    "ON CONFLICT(id) DO NOTHING",
                    (template, Jsonb(table.headers), Jsonb(mapping), now()),
                )
                counts = {"MATCHED": 0, "NEW": 0, "AMBIGUOUS": 0, "template_id": template}
                matcher = _MatchingConnection(cur)
                for index, raw in enumerate(rows):
                    record = record_from_row(raw, mapping)
                    if not record.get("clinic_name", "").strip():
                        raise ValueError(f"{index + 2}行目の医院名が空白です。取込は取り消しました。")
                    match = match_record(matcher, record)
                    self._upsert_tx(cur, record, match, "コムデスク", batch, index,
                                    original=raw, template_id=template)
                    counts[match.status] += 1
                cur.execute(
                    "INSERT INTO provenance.import_batches(id,source,result_json,created_at) VALUES(%s,%s,%s,%s)",
                    (batch, "コムデスク", Jsonb(counts), now()),
                )
            self._conn.commit()
            return counts
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)

    def import_master(self, frame):
        import pandas as pd
        from src.master.matching import match_record
        from src.master.store import digest
        from src.utils.date_utils import parse_date
        records = frame.fillna("").astype(str).to_dict("records") if isinstance(frame, pd.DataFrame) else frame
        if not records:
            raise ValueError("取り込む厚生局データがありません。")
        for index, record in enumerate(records):
            if (not record.get("clinic_name") or not record.get("prefecture")
                    or record.get("medical_type") not in {"医科", "歯科"} or not parse_date(record.get("as_of"))):
                raise ValueError(f"厚生局データ{index + 2}行目：医院名・都道府県・医科/歯科・基準日を確認してください。")
        batch = digest(["master", records])
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT result_json FROM provenance.import_batches WHERE id=%s", (batch,))
                old = cur.fetchone()
                if old:
                    self._conn.commit()
                    return {**old[0], "already_imported": True}
                for prefecture, medical_type, as_of in {(r["prefecture"], r["medical_type"], r["as_of"]) for r in records}:
                    cur.execute("SELECT max(source_as_of_date) FROM public.clinics WHERE prefecture=%s AND medical_type=%s", (prefecture, medical_type))
                    latest = cur.fetchone()[0]
                    if latest and as_of < latest:
                        raise ValueError("DBより古い厚生局データです。更新日を確認してください。取込は取り消しました。")
                    if not latest or as_of > latest:
                        cur.execute("UPDATE public.clinics SET is_new=false WHERE prefecture=%s AND medical_type=%s", (prefecture, medical_type))
                counts = {"MATCHED": 0, "NEW": 0, "AMBIGUOUS": 0}
                matcher = _MatchingConnection(cur)
                for index, record in enumerate(records):
                    match = match_record(matcher, record)
                    self._upsert_tx(cur, record, match, "厚生局", batch, index, new_flag=True)
                    counts[match.status] += 1
                cur.execute(
                    "INSERT INTO provenance.import_batches(id,source,result_json,created_at) VALUES(%s,%s,%s,%s)",
                    (batch, "厚生局", Jsonb(counts), now()),
                )
            self._conn.commit()
            return counts
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)

    def resolve_review(self, review_id, target_id=None, note=""):
        from src.normalizer.clinic_name import normalize_person

        def fetch_dict(cur):
            row = cur.fetchone()
            return dict(zip([item.name for item in cur.description], row)) if row else None

        def root_id(cur, clinic_id):
            seen = set()
            while True:
                if clinic_id in seen:
                    raise ValueError("医院の統合先が循環しています。")
                seen.add(clinic_id)
                cur.execute("SELECT merged_into FROM public.clinics WHERE id=%s", (clinic_id,))
                row = cur.fetchone()
                if not row:
                    raise ValueError("医院が見つかりません。")
                if row[0] is None:
                    return clinic_id
                clinic_id = row[0]

        def merge(cur, source_id, destination_id, reason):
            source_id, destination_id = root_id(cur, source_id), root_id(cur, destination_id)
            if source_id == destination_id:
                return destination_id
            cur.execute("SELECT * FROM public.clinics WHERE id=%s FOR UPDATE", (source_id,))
            source = fetch_dict(cur)
            cur.execute("SELECT * FROM public.clinics WHERE id=%s FOR UPDATE", (destination_id,))
            target = fetch_dict(cur)
            if source["uuid"] and target["uuid"] and source["uuid"] != target["uuid"]:
                raise ValueError("異なる既存UUID同士は統合できません。")
            cur.execute("SELECT field,value_json FROM provenance.manual_overrides WHERE clinic_id=%s", (source_id,))
            source_manual = dict(cur.fetchall())
            cur.execute("SELECT field,value_json FROM provenance.manual_overrides WHERE clinic_id=%s", (destination_id,))
            for field, value in cur.fetchall():
                if field in source_manual and source_manual[field] != value:
                    raise ValueError("手動修正の値が異なります。両医院の修正内容を確認してください。")
            if source["uuid"] and not target["uuid"]:
                source_id, destination_id = destination_id, source_id
                source, target = target, source
            source_base, target_base = dict(source["base_json"]), dict(target["base_json"])
            base = ({**target_base, **source_base} if source["source_as_of_date"] > target["source_as_of_date"]
                    else {**source_base, **target_base})
            history_before = {"source": source, "target": target, "job_items": []}
            cur.execute(
                "SELECT clinic_id,result_json,updated_at FROM research.research_results "
                "WHERE clinic_id IN (%s,%s) ORDER BY updated_at,clinic_id", (source_id, destination_id)
            )
            research_rows = [dict(zip([item.name for item in cur.description], row)) for row in cur.fetchall()]
            history_before["research_results"] = research_rows
            if research_rows:
                research = {}
                for row in research_rows:
                    research.update(json.loads(row["result_json"]))
                previous_doctor = research.get("doctor_name")
                if previous_doctor and base.get("manager_name") and normalize_person(previous_doctor) != normalize_person(base["manager_name"]):
                    for field in list(research):
                        if field.startswith(("age_", "license_", "graduation_")) or field == "doctor_name":
                            research.pop(field)
                    research.update(age_probability_under_59=None, age_estimation_confidence="REVIEW",
                                    age_estimation_reason="再統合後の院長の経歴を確認してください。")
                cur.execute(
                    "INSERT INTO research.research_results(clinic_id,result_json,updated_at) VALUES(%s,%s,%s) "
                    "ON CONFLICT(clinic_id) DO UPDATE SET result_json=EXCLUDED.result_json,updated_at=EXCLUDED.updated_at",
                    (destination_id, dumps(research), max(row["updated_at"] for row in research_rows)),
                )
            cur.execute(
                "INSERT INTO provenance.manual_overrides(clinic_id,field,value_json,source,note,updated_at) "
                "SELECT %s,field,value_json,source,note,updated_at FROM provenance.manual_overrides WHERE clinic_id=%s "
                "ON CONFLICT(clinic_id,field) DO NOTHING", (destination_id, source_id)
            )
            cur.execute(
                "INSERT INTO research.hp_pages(clinic_id,url,page_json,checked_at) "
                "SELECT %s,url,page_json,checked_at FROM research.hp_pages WHERE clinic_id=%s "
                "ON CONFLICT(clinic_id,url) DO NOTHING", (destination_id, source_id)
            )
            cur.execute("SELECT job_id,clinic_id,state,result,note,lease_until FROM research.research_job_items WHERE clinic_id=%s", (source_id,))
            items = cur.fetchall()
            for item in items:
                history_before["job_items"].append(dict(zip([d.name for d in cur.description], item)))
                cur.execute("SELECT state,result,note,lease_until FROM research.research_job_items WHERE job_id=%s AND clinic_id=%s", (item[0], destination_id))
                existing = cur.fetchone()
                if existing:
                    if item[2] == "DONE" and existing[0] != "DONE":
                        cur.execute("UPDATE research.research_job_items SET state='DONE',result=%s,note=%s,lease_until='' WHERE job_id=%s AND clinic_id=%s", (item[3], item[4], item[0], destination_id))
                    cur.execute("DELETE FROM research.research_job_items WHERE job_id=%s AND clinic_id=%s", (item[0], source_id))
                else:
                    cur.execute("UPDATE research.research_job_items SET clinic_id=%s WHERE job_id=%s AND clinic_id=%s", (destination_id, item[0], source_id))
            cur.execute("UPDATE public.clinics SET merged_into=%s,merge_hold=false WHERE id=%s", (destination_id, source_id))
            cur.execute(
                "UPDATE public.clinics SET uuid=%s,base_json=%s,first_seen_at=%s,last_seen_at=%s,source_as_of_date=%s,merge_hold=false WHERE id=%s",
                (target["uuid"] or source["uuid"], Jsonb(base), min(target["first_seen_at"], source["first_seen_at"]),
                 max(target["last_seen_at"], source["last_seen_at"]),
                 max(target["source_as_of_date"], source["source_as_of_date"]), destination_id),
            )
            cur.execute("UPDATE provenance.source_records SET clinic_id=%s,match_status='MATCHED',match_reason=%s WHERE clinic_id=%s", (destination_id, reason, source_id))
            cur.execute("UPDATE provenance.comdesk_original_rows SET clinic_id=%s WHERE clinic_id=%s", (destination_id, source_id))
            cur.execute("UPDATE provenance.match_reviews SET resolved_clinic_id=%s WHERE resolved_clinic_id=%s", (destination_id, source_id))
            cur.execute("SELECT id,candidates_json FROM provenance.match_reviews WHERE status='PENDING'")
            for pending_id, candidates in cur.fetchall():
                mapped = sorted({destination_id if cid == source_id else cid for cid in candidates})
                if mapped != candidates:
                    cur.execute("UPDATE provenance.match_reviews SET candidates_json=%s WHERE id=%s", (Jsonb(mapped), pending_id))
            self._refresh_projection_tx(cur, destination_id, is_authoritative_official_source=True)
            cur.execute(
                "INSERT INTO provenance.change_history(clinic_id,action,before_json,after_json,note,created_at) VALUES(%s,%s,%s,%s,%s,%s)",
                (destination_id, "電話番号キーで再統合", dumps(history_before),
                 dumps({"source_id": source_id, "target_id": destination_id}), reason, now()),
            )
            return destination_id

        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT m.*,s.record_json,s.source,s.source_hash,s.row_number,s.clinic_id AS existing_clinic_id "
                    "FROM provenance.match_reviews m JOIN provenance.source_records s ON m.source_record_id=s.id "
                    "WHERE m.id=%s AND m.status='PENDING' FOR UPDATE", (review_id,)
                )
                review = fetch_dict(cur)
                if not review:
                    raise ValueError("この確認事項は処理済みです。")
                record = dict(review["record_json"])
                if review["existing_clinic_id"] is not None:
                    clinic_id = root_id(cur, review["existing_clinic_id"])
                    target_id = clinic_id if target_id is None else merge(cur, clinic_id, target_id, "手動確認: " + note)
                    cur.execute("UPDATE public.clinics SET merge_hold=false WHERE id=%s", (target_id,))
                elif target_id is None:
                    uid = record.get("uuid", "")
                    if uid:
                        cur.execute("SELECT 1 FROM public.clinics WHERE uuid=%s AND merged_into IS NULL", (uid,))
                        if cur.fetchone():
                            raise ValueError("同じUUIDが既にあります。別医院として追加できません。")
                    cur.execute(
                        "INSERT INTO public.clinics(uuid,base_json,first_seen_at,last_seen_at,source_as_of_date,is_new) "
                        "VALUES(%s,%s,%s,%s,%s,true) RETURNING id",
                        (uid, Jsonb(record), now(), now(), record.get("as_of", "")),
                    )
                    target_id = cur.fetchone()[0]
                    assert_high_range(target_id, table="public.clinics")
                else:
                    target_id = root_id(cur, target_id)
                    cur.execute("SELECT uuid,base_json FROM public.clinics WHERE id=%s FOR UPDATE", (target_id,))
                    uuid_value, base = cur.fetchone()
                    if uuid_value and record.get("uuid") and uuid_value != record["uuid"]:
                        raise ValueError("異なる既存UUID同士は統合できません。元のコムデスクで確認してください。")
                    base = dict(base)
                    base.update(record if review["source"] == "厚生局" else
                                {key: value for key, value in record.items() if value and not base.get(key)})
                    cur.execute("UPDATE public.clinics SET uuid=%s,base_json=%s,last_seen_at=%s WHERE id=%s", (uuid_value or record.get("uuid", ""), Jsonb(base), now(), target_id))
                cur.execute("UPDATE provenance.source_records SET clinic_id=%s,match_status='MATCHED',match_reason=%s WHERE id=%s", (target_id, "手動確認: " + note, review["source_record_id"]))
                cur.execute("UPDATE provenance.comdesk_original_rows SET clinic_id=%s WHERE source_hash=%s AND row_number=%s", (target_id, review["source_hash"], review["row_number"]))
                cur.execute("UPDATE provenance.match_reviews SET status='RESOLVED',resolved_clinic_id=%s,note=%s,updated_at=%s WHERE id=%s", (target_id, note, now(), review_id))
                cur.execute("INSERT INTO provenance.change_history(clinic_id,action,before_json,after_json,note,created_at) VALUES(%s,%s,%s,%s,%s,%s)", (target_id, "重複確認", dumps({"review_id": review_id}), dumps({"target_id": target_id}), note, now()))
                self._refresh_projection_tx(cur, target_id, is_authoritative_official_source=(review["source"] == "厚生局"))
            self._conn.commit()
            return target_id
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)

    def refresh_age_model(self):
        from src.enrichment.profiles import estimate_profile_age
        from src.master.store import digest
        from src.utils.config import ROOT
        from src.utils.date_utils import today_japan
        signature = digest([today_japan().year, (ROOT / "config/age_model.yml").read_text(encoding="utf-8")])
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT value FROM app_config.settings WHERE key='age_basis'")
                row = cur.fetchone()
                if row and json.loads(row[0]) == signature:
                    self._conn.commit()
                    return
                cur.execute(
                    "SELECT clinic_id FROM research.research_results WHERE "
                    "result_json::jsonb->>'license_registration_year' IS NOT NULL OR "
                    "result_json::jsonb->>'graduation_year' IS NOT NULL"
                )
                ids = [item[0] for item in cur.fetchall()]
            self._conn.commit()
            research_repo = SupabaseResearchWriteRepository(self._conn)
            for clinic_id in ids:
                with self._conn.cursor() as cur:
                    cur.execute("SELECT effective_json FROM public.clinics WHERE id=%s", (clinic_id,))
                    effective = json.loads(cur.fetchone()[0])
                self._conn.commit()
                research_repo.save_research(clinic_id, estimate_profile_age({**effective, "id": clinic_id}))
            SupabaseSettingsWriteRepository(self._conn).set("age_basis", signature)
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

    @staticmethod
    def _row_dict(cur, row):
        return dict(zip([item.name for item in cur.description], row))

    def job_status(self, job_id):
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM research.research_jobs WHERE id=%s", (job_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError("調査履歴が見つかりません。")
            result = self._row_dict(cur, row)
            cur.execute("SELECT state,count(*) FROM research.research_job_items WHERE job_id=%s GROUP BY state", (job_id,))
            result["counts"] = dict(cur.fetchall())
            cur.execute("SELECT result,count(*) FROM research.research_job_items WHERE job_id=%s AND state='DONE' GROUP BY result", (job_id,))
            result["results"] = dict(cur.fetchall())
            result["total"] = sum(result["counts"].values())
            return result

    def recent_jobs(self):
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM research.research_jobs WHERE status<>'RESET' ORDER BY created_at DESC,id DESC LIMIT 20")
            rows = cur.fetchall()
            names = [item.name for item in cur.description]
        return [dict(zip(names, row)) for row in rows]

    def pending_site_candidates(self, job_id):
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT i.clinic_id,c.maps_presence_status,c.maps_website_url "
                "FROM research.research_job_items i JOIN public.clinics c ON c.id=i.clinic_id "
                "WHERE i.job_id=%s AND i.state='PENDING' ORDER BY i.clinic_id", (job_id,)
            )
            return cur.fetchall()

    def create_job_from_filters(self, filters, kind="hp", limit=100, max_searches=100, force=False, max_pages=20):
        import uuid
        from src.repository.supabase_filters import where
        if kind not in {"hp", "epark", "media"}:
            raise ValueError("調査種類を確認してください。")
        limit = min(500, max(1, int(limit)))
        max_searches = min(1000, max(0, int(max_searches)))
        sql, args = where(filters)
        if not force:
            sql += {
                "hp": " AND hp_status='UNRESEARCHED'",
                "epark": " AND (effective_json::jsonb)->>'epark_checked_at' IS NULL",
                "media": " AND (effective_json::jsonb)->>'media_checked_at' IS NULL",
            }[kind]
        jid = uuid.uuid4().hex
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM public.clinics WHERE " + sql
                    + " ORDER BY (uuid<>'') DESC,is_new DESC,id LIMIT %s",
                    (*args, limit),
                )
                ids = [row[0] for row in cur.fetchall()]
                if not ids:
                    raise ValueError("指定した条件の未調査医院がありません。条件を見直すか、強制再調査を選択してください。")
                ts = now()
                cur.execute(
                    "INSERT INTO research.research_jobs(id,kind,options_json,max_searches,created_at,updated_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s)",
                    (jid, kind, Jsonb({"force": bool(force), "max_pages": max_pages}), max_searches, ts, ts),
                )
                cur.executemany(
                    "INSERT INTO research.research_job_items(job_id,clinic_id) VALUES(%s,%s)",
                    [(jid, clinic_id) for clinic_id in ids],
                )
            self._conn.commit()
            return jid
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)

    def recover_job_for_run(self, job_id):
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT * FROM research.research_jobs WHERE id=%s FOR UPDATE", (job_id,))
                row = cur.fetchone()
                if not row:
                    raise ValueError("調査履歴が見つかりません。")
                columns = [item.name for item in cur.description]
                job = dict(zip(columns, row))
                if job["status"] in {"COMPLETED", "RESET"}:
                    self._conn.commit()
                    return None
                cur.execute("UPDATE research.research_job_items SET state='PENDING' WHERE job_id=%s AND state='RUNNING'", (job_id,))
                cur.execute("UPDATE research.research_jobs SET status='PAUSED' WHERE id=%s AND status='RUNNING'", (job_id,))
                cur.execute("UPDATE research.research_jobs SET status='RUNNING',updated_at=%s WHERE id=%s", (now(), job_id))
            self._conn.commit()
            return job
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)

    def complete_job_if_no_remaining_items(self, job_id):
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1 FROM research.research_job_items WHERE job_id=%s AND state IN ('PENDING','RUNNING') LIMIT 1", (job_id,))
                if cur.fetchone() is None:
                    cur.execute("UPDATE research.research_jobs SET status='COMPLETED',updated_at=%s WHERE id=%s AND status='RUNNING'", (now(), job_id))
            self._conn.commit()
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)

    def claim_specific_item(self, job_id, clinic_id):
        try:
            with self._conn.cursor() as cur:
                cur.execute("UPDATE research.research_job_items SET state='RUNNING' WHERE job_id=%s AND clinic_id=%s AND state='PENDING'", (job_id, clinic_id))
                claimed = cur.rowcount > 0
            self._conn.commit()
            return claimed
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)

    def requeue_item_for_budget_or_pause(self, job_id, clinic_id, note, job_status):
        try:
            with self._conn.cursor() as cur:
                cur.execute("UPDATE research.research_job_items SET state='PENDING',note=%s WHERE job_id=%s AND clinic_id=%s", (note, job_id, clinic_id))
                cur.execute("UPDATE research.research_jobs SET status=%s,updated_at=%s WHERE id=%s", (job_status, now(), job_id))
            self._conn.commit()
        except Exception as exc:
            _rollback_and_raise(self._conn, exc)

    def finish_item(self, job_id, clinic_id, status, note):
        try:
            with self._conn.cursor() as cur:
                cur.execute("UPDATE research.research_job_items SET state='DONE',result=%s,note=%s WHERE job_id=%s AND clinic_id=%s", (status, note, job_id, clinic_id))
                cur.execute("UPDATE research.research_jobs SET updated_at=%s WHERE id=%s", (now(), job_id))
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
