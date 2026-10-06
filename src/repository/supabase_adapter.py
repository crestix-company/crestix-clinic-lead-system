"""Supabase (PostgreSQL) adapter -- READ ONLY in Stage4-A.

No write method is implemented: Stage4-A keeps WRITE on SQLite exclusively (see the Stage4-A
handoff, section 3/16). Every method here only ever runs SELECT statements.

Business logic (effective_hp_rank, effective_rank_reason, classify_site, extract_municipality,
medical_key-shape decisions) is never reimplemented against Postgres: these stay the same pure
functions imported from src.master.*, called with data fetched from the migrated schemas. Only
I/O (which table a column comes from) differs between this adapter and the SQLite one.
"""
import json

from src.master.filters import Filters
from src.master.hp_effective_rank import effective_hp_rank, effective_rank_reason
from src.master.hp_site_type import classify_site
from src.master.hp_batch_metrics import BatchMetricInvariantError
from src.normalizer.address import extract_municipality
from src.repository.errors import BackendNotSupportedError
from src.repository import supabase_filters as sf


def connect(url, *, connect_timeout=20, autocommit=False):
    """Opens a Postgres connection with the Stage3 root-caused float-readback fix applied
    (extra_float_digits=0 is this project's session default; raised to 3 here so float8 text
    output is shortest-round-trip-safe again -- see docs/supabase_migration root cause writeup).
    This changes output FORMATTING only, never stored bytes, and keeps every comparison exact.

    autocommit must be set here, before the first statement -- psycopg refuses to change it once
    a connection is already mid-transaction (ProgrammingError: "can't change 'autocommit' now"),
    which the SET below would otherwise immediately cause. Default stays False (unchanged
    behavior for existing callers); pass True for a connection that will be reused across many
    independent statements over a long lifetime (see src.repository.shadow), where one
    cancelled/failed statement must never leave the connection stuck for every statement after.
    """
    import psycopg
    conn = psycopg.connect(url, connect_timeout=connect_timeout)
    if autocommit:
        conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SET extra_float_digits = 3")
    return conn


class SupabaseHpResearchRepository:
    def __init__(self, conn):
        self._conn = conn

    def machine_rank(self, clinic_id):
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT machine_hp_rank FROM hp_research.clinic_hp_research "
                "WHERE clinic_id=%s AND fetch_status='OK' AND machine_hp_rank<>''",
                (clinic_id,),
            )
            row = cur.fetchone()
        return row[0] if row else "UNKNOWN"

    def batch_urls(self, clinic_id):
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT hp_url, final_url FROM hp_research.clinic_hp_research WHERE clinic_id=%s",
                (clinic_id,),
            )
            row = cur.fetchone()
        return (row[0] or "", row[1] or "") if row else ("", "")

    def website_treatment_categories(self, clinic_id):
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT treatment_categories FROM hp_research.clinic_hp_research "
                "WHERE clinic_id=%s AND fetch_status='OK'",
                (clinic_id,),
            )
            row = cur.fetchone()
        if not row or not isinstance(row[0], list):
            return []
        return row[0]

    def batch_metrics(self):
        with self._conn.cursor() as cur:
            cur.execute(
                """
                WITH url_clinics AS (
                  SELECT id FROM public.clinics WHERE hp_url<>'' OR maps_website_url<>''
                ), joined AS (
                  SELECT u.id, r.fetch_status, r.treatment_categories
                  FROM url_clinics u LEFT JOIN hp_research.clinic_hp_research r ON r.clinic_id=u.id
                )
                SELECT
                  count(*),
                  sum(CASE WHEN fetch_status='OK' THEN 1 ELSE 0 END),
                  sum(CASE WHEN fetch_status IS NOT NULL AND fetch_status<>'OK' THEN 1 ELSE 0 END),
                  sum(CASE WHEN fetch_status IS NULL THEN 1 ELSE 0 END),
                  sum(CASE WHEN fetch_status='OK' AND jsonb_array_length(treatment_categories)>0 THEN 1 ELSE 0 END),
                  sum(CASE WHEN fetch_status='OK' AND jsonb_array_length(treatment_categories)=0 THEN 1 ELSE 0 END)
                FROM joined
                """
            )
            row = cur.fetchone()
        keys = ("url_acquired", "researched", "failed", "not_researched",
                "treatment_detected", "treatment_not_detected")
        result = {k: int(v or 0) for k, v in zip(keys, row)}
        if result["url_acquired"] != result["researched"] + result["failed"] + result["not_researched"]:
            raise BatchMetricInvariantError("WebサイトURL件数と調査状態の合計が一致しません。")
        if result["researched"] != result["treatment_detected"] + result["treatment_not_detected"]:
            raise BatchMetricInvariantError("Webサイト調査完了件数と治療カテゴリ判定の合計が一致しません。")
        return result


class SupabaseClinicRepository:
    _COLUMNS = (
        "id", "uuid", "merged_into", "effective_json", "tel_match_key", "first_seen_at",
        "last_seen_at", "source_as_of_date", "is_new", "departments_json",
        "hp_status", "hp_url", "hp_rank", "maps_website_url",
    )

    def __init__(self, conn, hp_repository: SupabaseHpResearchRepository):
        self._conn = conn
        self._hp = hp_repository
        self._excluded_ids_cache = None  # Stage4-B.6 P0-1, see _excluded_clinic_ids()

    def _excluded_clinic_ids(self):
        """Stage4-B.6 P0-1: computes the exact clinic_id set the hospital/center exclusion
        predicate produces, ONCE per repository instance, then caches it. See the long comment
        in supabase_filters.clauses() for why this predicate can't be simplified and why caching
        it is safe (Supabase is never written to during Stage4-B/C shadow-read usage).
        """
        if self._excluded_ids_cache is None:
            # This is the ORIGINAL predicate verbatim (see supabase_filters.clauses()) -- the one
            # and only place it is still evaluated row-by-row. Its result is cached and reused by
            # every subsequent query via the `NOT EXISTS (SELECT 1 FROM unnest(...))` anti-join
            # in supabase_filters.clauses(excluded_ids=...), which EXPLAIN confirmed is a 4.7x
            # faster equivalent (4690ms -> 993ms) because it lets Postgres use a Merge Anti Join
            # against clinics_pkey instead of evaluating an unindexable regex+LIKE per row.
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM public.clinics WHERE "
                    "exclude_reason IN ('hospital','center') "
                    "OR COALESCE(substring(effective_json from '\"facility_type\":\"([^\"]*)\"'),'')='病院' "
                    "OR clinic_name LIKE '%病院%' OR clinic_name LIKE '%センター%'"
                )
                self._excluded_ids_cache = [r[0] for r in cur.fetchall()]
        return self._excluded_ids_cache

    def _where(self, filters, as_of):
        return sf.where(filters, as_of, excluded_ids=self._excluded_clinic_ids())

    @staticmethod
    def _from_sql(filters):
        """Return the FROM fragment required by SQL-pushed filters."""
        sql = "public.clinics"
        if filters.effective_ranks and not set(filters.effective_ranks) <= {"A", "B"}:
            # Collision-free aliases keep existing unqualified clinic predicates valid.
            sql += (
                " LEFT JOIN (SELECT clinic_id, machine_hp_rank AS _machine_hp_rank, "
                "fetch_status AS _fetch_status FROM hp_research.clinic_hp_research) _effective_hp "
                "ON _effective_hp.clinic_id=clinics.id"
            )
        return sql

    def _clauses(self, filters, as_of):
        return sf.clauses(filters, as_of, excluded_ids=self._excluded_clinic_ids())

    def _fetch_row(self, where_sql, param):
        cols = ",".join(self._COLUMNS)
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT {cols} FROM public.clinics WHERE {where_sql}", (param,))
            row = cur.fetchone()
        return dict(zip(self._COLUMNS, row)) if row else None

    def _build_record(self, d, machine_hp_rank, fetch_status, hp_batch_url, final_url, treatment_categories):
        """Shared by get() and the batch path used by query() (P0-2) -- exactly one place builds
        the record shape, so there is no risk of the two paths drifting apart.
        """
        data = {
            **json.loads(d["effective_json"]),
            "id": d["id"], "uuid": d["uuid"], "tel_match_key": d["tel_match_key"],
            "first_seen_at": d["first_seen_at"], "last_seen_at": d["last_seen_at"],
            "source_as_of_date": d["source_as_of_date"],
            "is_new_since_last_update": bool(d["is_new"]),
            "normalized_departments": d["departments_json"],
        }
        machine = machine_hp_rank if (fetch_status == "OK" and machine_hp_rank) else "UNKNOWN"
        data.update(
            machine_rank=machine, old_hp_rank_db=d["hp_rank"],
            effective_hp_rank=effective_hp_rank(machine, d["hp_rank"]),
            effective_rank_reason=effective_rank_reason(machine, d["hp_rank"]),
        )
        source_url = hp_batch_url or ""
        site_type, portal_name = classify_site(d["hp_status"], d["hp_url"], source_url or d["maps_website_url"], final_url or "")
        data.update(
            site_type=site_type, portal_name=portal_name,
            site_source_url=source_url, site_final_url=final_url or "",
            website_treatment_categories=treatment_categories if (fetch_status == "OK" and isinstance(treatment_categories, list)) else [],
        )
        return data

    def get(self, clinic_id):
        d = self._fetch_row("id=%s", int(clinic_id))
        if d is None:
            raise ValueError("医院が見つかりません。")
        if d["merged_into"]:
            return self.get(d["merged_into"])
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT machine_hp_rank, fetch_status, hp_url, final_url, treatment_categories "
                "FROM hp_research.clinic_hp_research WHERE clinic_id=%s",
                (d["id"],),
            )
            hp_row = cur.fetchone()
        machine_hp_rank, fetch_status, hp_batch_url, final_url, treatment_categories = hp_row or ("", "", "", "", [])
        return self._build_record(d, machine_hp_rank, fetch_status, hp_batch_url, final_url, treatment_categories)

    def get_by_uuid(self, uuid):
        d = self._fetch_row("uuid=%s AND merged_into IS NULL", uuid)
        return self.get(d["id"]) if d else None

    def get_by_medical_key(self, medical_key):
        with self._conn.cursor() as cur:
            cur.execute("SELECT id FROM public.clinics WHERE medical_key=%s AND merged_into IS NULL", (medical_key,))
            row = cur.fetchone()
        return self.get(row[0]) if row else None

    def _batch_get(self, ids):
        """P0-2: fetches `ids` (already-resolved, merged_into-IS-NULL candidates, in caller's
        desired order) in a small, fixed number of round trips instead of 4 per row. Returns
        records in the SAME order as `ids`.
        """
        if not ids:
            return []
        cols = ",".join(self._COLUMNS)
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT {cols} FROM public.clinics WHERE id = ANY(%s)", (list(ids),))
            rows_by_id = {row[0]: dict(zip(self._COLUMNS, row)) for row in cur.fetchall()}
            cur.execute(
                "SELECT clinic_id, machine_hp_rank, fetch_status, hp_url, final_url, treatment_categories "
                "FROM hp_research.clinic_hp_research WHERE clinic_id = ANY(%s)",
                (list(ids),),
            )
            hp_by_id = {r[0]: r[1:] for r in cur.fetchall()}
        records = []
        for cid in ids:
            d = rows_by_id.get(cid)
            if d is None:
                continue
            if d["merged_into"]:
                # Candidates come from a merged_into IS NULL predicate, so this should not
                # normally happen; fall back to the single-row path for correctness if it does.
                records.append(self.get(d["merged_into"]))
                continue
            machine_hp_rank, fetch_status, hp_batch_url, final_url, treatment_categories = hp_by_id.get(cid, ("", "", "", "", []))
            records.append(self._build_record(d, machine_hp_rank, fetch_status, hp_batch_url, final_url, treatment_categories))
        return records

    def _candidate_ids(self, filters, as_of):
        sql, args = self._where(filters, as_of)
        if sf.needs_post_filter(filters):
            need_hp = bool(filters.site_types)
            cols = ["id"]
            if need_hp:
                cols += ["hp_rank", "hp_status", "hp_url", "maps_website_url"]
            if filters.municipalities:
                cols += ["address"]
            if filters.production_companies:
                cols += ["effective_json"]
            with self._conn.cursor() as cur:
                cur.execute(
                    f"SELECT {','.join(cols)} FROM {self._from_sql(filters)} WHERE {sql} ORDER BY signal_count DESC, id",
                    args,
                )
                rows = cur.fetchall()
            # Batch-fetch hp_research rows for every candidate in one round trip (never N+1 --
            # the naive per-row self._hp.machine_rank()/batch_urls() calls below would otherwise
            # issue one query per candidate, which is correct but unusably slow at ~150k rows).
            hp_by_id = {}
            if need_hp and rows:
                ids_so_far = [dict(zip(cols, row))["id"] for row in rows]
                with self._conn.cursor() as cur:
                    cur.execute(
                        "SELECT clinic_id, machine_hp_rank, fetch_status, hp_url, final_url "
                        "FROM hp_research.clinic_hp_research WHERE clinic_id = ANY(%s)",
                        (ids_so_far,),
                    )
                    for cid, machine_hp_rank, fetch_status, hp_url, final_url in cur.fetchall():
                        hp_by_id[cid] = (machine_hp_rank, fetch_status, hp_url, final_url)
            ids = []
            for row in rows:
                d = dict(zip(cols, row))
                machine_hp_rank, fetch_status, hp_batch_url, final_url = hp_by_id.get(d["id"], ("", "", "", ""))
                machine = machine_hp_rank if (fetch_status == "OK" and machine_hp_rank) else "UNKNOWN"
                if filters.site_types:
                    site_type, _ = classify_site(d["hp_status"], d["hp_url"], (hp_batch_url or "") or d["maps_website_url"], final_url or "")
                    if site_type not in filters.site_types:
                        continue
                if filters.municipalities:
                    if extract_municipality(d["address"]) not in filters.municipalities:
                        continue
                if filters.production_companies:
                    companies = json.loads(d["effective_json"]).get("hp_production_companies", []) or []
                    if not any(c in filters.production_companies for c in companies):
                        continue
                ids.append(d["id"])
            return ids
        with self._conn.cursor() as cur:
            if filters.effective_ranks:
                # The effective A/B result is highly selective (~1k rows). Sending the cached
                # 9k excluded-id array to Postgres makes the otherwise fast query noisier than
                # returning those ~1k ids and applying the exact same cached set locally. This
                # remains one DB round trip and preserves ordering byte-for-byte.
                sql, args = sf.where(filters, as_of, excluded_ids=[])
                cur.execute(
                    f"SELECT id FROM {self._from_sql(filters)} WHERE {sql} ORDER BY signal_count DESC, id",
                    args,
                )
                excluded = set(self._excluded_clinic_ids())
                return [r[0] for r in cur.fetchall() if r[0] not in excluded]
            cur.execute(f"SELECT id FROM {self._from_sql(filters)} WHERE {sql} ORDER BY signal_count DESC, id", args)
            return [r[0] for r in cur.fetchall()]

    def count(self, filters=None, as_of=None):
        filters = filters or Filters(active_only=False, hp_only=False)
        sf.raise_if_unsupported(filters)
        if sf.needs_post_filter(filters) or filters.effective_ranks:
            return len(self._candidate_ids(filters, as_of))
        sql, args = self._where(filters, as_of)
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {self._from_sql(filters)} WHERE {sql}", args)
            return cur.fetchone()[0]

    def query(self, filters=None, limit=100, offset=0, as_of=None):
        filters = filters or Filters(active_only=False, hp_only=False)
        sf.raise_if_unsupported(filters)
        limit = min(100000, max(0, int(limit)))
        offset = max(0, int(offset))
        ids = self._candidate_ids(filters, as_of)
        page_ids = ids[offset:offset + limit]
        return self._batch_get(page_ids)  # P0-2: batched, preserves page_ids order

    def funnel(self, filters, as_of=None):
        sf.raise_if_unsupported(filters)
        conditions = ["merged_into IS NULL", "merge_hold=false"]
        args = []
        with self._conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.clinics WHERE merged_into IS NULL")
            output = [("全マスター", cur.fetchone()[0])]
            cur.execute("SELECT count(*) FROM public.clinics WHERE merged_into IS NULL AND merge_hold=false")
            output.append(("重複確認待ちを除く", cur.fetchone()[0]))
            for label, clause_sql, params in self._clauses(filters, as_of):
                conditions.append(clause_sql)
                args.extend(params)
                cur.execute(
                    f"SELECT count(*) FROM {self._from_sql(filters)} WHERE {' AND '.join(conditions)}",
                    args,
                )
                output.append((label, cur.fetchone()[0]))
        output.append(("最終営業対象", output[-1][1]))
        return output

    def metrics(self):
        # P0-3: 15 independent round trips -> 1. The 10 per-clinics-item counts become FILTER
        # aggregates over a single scan; the 2 provenance-schema counts (different tables, can't
        # join into the same FROM) become scalar subqueries in the same SELECT; the 3 Google Maps
        # counts join the same FILTER-aggregate pattern. Same labels, same predicates, same
        # result dict shape as the original per-item queries -- just issued together.
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  count(*) FILTER (WHERE true),
                  count(*) FILTER (WHERE uuid<>''),
                  count(*) FILTER (WHERE uuid=''),
                  count(*) FILTER (WHERE active),
                  count(*) FILTER (WHERE hp_status='VERIFIED'),
                  count(*) FILTER (WHERE hp_status='NOT_FOUND'),
                  count(*) FILTER (WHERE age_probability IS NOT NULL),
                  count(*) FILTER (WHERE hp_status IN ('REVIEW','ERROR')),
                  count(*) FILTER (WHERE signal_count>=2),
                  count(*) FILTER (WHERE signal_count>=3),
                  count(*) FILTER (WHERE maps_presence_status IN ('MAPS_MATCHED_WEBSITE','MAPS_MATCHED_NO_WEBSITE')),
                  count(*) FILTER (WHERE maps_presence_status='MAPS_MATCHED_WEBSITE' AND maps_website_url<>''),
                  count(*) FILTER (WHERE maps_presence_status='MAPS_NOT_FOUND'),
                  (SELECT count(*) FROM provenance.match_reviews WHERE status='PENDING'),
                  (SELECT count(*) FROM provenance.source_records WHERE source='厚生局')
                FROM public.clinics WHERE merged_into IS NULL
                """
            )
            row = cur.fetchone()
        labels = (
            "全マスター", "既存UUIDあり", "新規医院（UUIDなし）", "現存クリニック", "HP確認済み", "HP未発見",
            "年齢推定済み", "HP要確認", "アツい（2個以上）", "かなりアツい",
            "Google Maps掲載確認", "Google Maps HP取得", "Google Maps未発見",
            "要確認重複（保留レコード）", "厚生局取込レコード",
        )
        return dict(zip(labels, row))


class SupabaseTreatmentRepository:
    def __init__(self, conn):
        self._conn = conn

    def confirmed_categories(self, clinic_id):
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT treatment_category_name FROM treatment.clinic_treatment_research "
                "WHERE clinic_id=%s AND research_status='CONFIRMED' ORDER BY treatment_category_name",
                (clinic_id,),
            )
            return [r[0] for r in cur.fetchall()]

    def status_for_ids(self, ids):
        ids = list(ids)
        if not ids:
            return {}
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT clinic_id FROM treatment.clinic_treatment_research "
                "WHERE research_status='CONFIRMED' AND clinic_id = ANY(%s)",
                (ids,),
            )
            fetched = {r[0] for r in cur.fetchall()}
            cur.execute(
                "SELECT clinic_id, research_status FROM treatment.clinic_research_status WHERE clinic_id = ANY(%s)",
                (ids,),
            )
            status_rows = dict(cur.fetchall())
        result = {}
        for cid in ids:
            if cid in fetched:
                result[cid] = "FETCHED"
            elif status_rows.get(cid) == "DONE":
                result[cid] = "DONE_NO_CATEGORY"
            elif status_rows.get(cid) == "FETCH_FAILED":
                result[cid] = "FETCH_FAILED"
            else:
                result[cid] = "NOT_RESEARCHED"
        return result

    def status_counts(self, filters=None, as_of=None):
        raise BackendNotSupportedError(
            "Supabase adapterはtreatment_status_counts()に未対応です(Stage4-Aの範囲外)。SQLite backendを使用してください。"
        )


class SupabaseResearchRepository:
    def __init__(self, conn):
        self._conn = conn

    def research_result(self, clinic_id):
        with self._conn.cursor() as cur:
            cur.execute("SELECT result_json FROM research.research_results WHERE clinic_id=%s", (clinic_id,))
            row = cur.fetchone()
        return json.loads(row[0]) if row else {}

    def hp_pages(self, clinic_id):
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT url, page_json, checked_at FROM research.hp_pages WHERE clinic_id=%s ORDER BY url",
                (clinic_id,),
            )
            rows = cur.fetchall()
        return [{**json.loads(r[1]), "url": r[0], "checked_at": r[2]} for r in rows]


class SupabaseProvenanceRepository:
    def __init__(self, conn):
        self._conn = conn

    def history_for_clinic(self, clinic_id, limit=30):
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, clinic_id, action, before_json, after_json, note, created_at "
                "FROM provenance.change_history WHERE clinic_id=%s ORDER BY id DESC LIMIT %s",
                (clinic_id, limit),
            )
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def templates(self):
        with self._conn.cursor() as cur:
            cur.execute("SELECT id, headers_json, mapping_json FROM provenance.templates ORDER BY created_at, id")
            rows = cur.fetchall()
        return [{"id": r[0], "headers": json.loads(r[1]), "mapping": json.loads(r[2])} for r in rows]

    def reviews(self, limit=100):
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT m.id, m.source_record_id, m.candidates_json, m.status, m.resolved_clinic_id, "
                "m.note, m.updated_at, s.record_json, s.source, s.match_reason, s.match_score "
                "FROM provenance.match_reviews m JOIN provenance.source_records s ON s.id=m.source_record_id "
                "WHERE m.status='PENDING' ORDER BY m.id LIMIT %s",
                (limit,),
            )
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


class SupabaseSettingsRepository:
    def __init__(self, conn):
        self._conn = conn

    def get(self, key, default=None):
        with self._conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config.settings WHERE key=%s", (key,))
            row = cur.fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        raise BackendNotSupportedError(
            "Supabase adapterはWRITEに未対応です(Stage4-AはREAD ONLY)。SQLite backendを使用してください。"
        )
