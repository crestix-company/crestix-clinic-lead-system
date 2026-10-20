-- Cloud Run READ ONLY clinic projection.
-- PROPOSAL / integration migration only: do not apply to production without B/security review.
-- The Reader receives the view, never SELECT on public.clinics. The NOLOGIN view owner reads
-- only the source columns needed by this view, is not a table owner, and has NOBYPASSRLS.
BEGIN;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'clinic_cloudrun_projection_owner') THEN
    RAISE EXCEPTION 'Projection owner already exists; inspect its grants before applying';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='clinic_cloudrun_reader'
                 AND NOT rolsuper AND NOT rolbypassrls) THEN
    RAISE EXCEPTION 'Reader role is absent or can bypass access controls';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relname = 'clinics' AND c.relrowsecurity
  ) THEN
    RAISE EXCEPTION 'public.clinics must already have RLS enabled';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_policy WHERE polrelid='public.clinics'::regclass
             AND 0=ANY(polroles)) THEN
    RAISE EXCEPTION 'PUBLIC clinic policy requires manual RLS composition review';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid='provenance.comdesk_original_rows'::regclass
                 AND relrowsecurity)
     OR NOT EXISTS (SELECT 1 FROM pg_class WHERE oid='provenance.templates'::regclass
                    AND relrowsecurity) THEN
    RAISE EXCEPTION 'Comdesk source tables must already have RLS enabled';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_policy WHERE polrelid IN
             ('provenance.comdesk_original_rows'::regclass,'provenance.templates'::regclass)
             AND 0=ANY(polroles)) THEN
    RAISE EXCEPTION 'PUBLIC Comdesk policy requires manual RLS composition review';
  END IF;
  IF to_regprocedure('public.clinic_cloudrun_effective_projection(text,bigint)') IS NOT NULL
     OR to_regclass('public.clinic_cloudrun_readonly') IS NOT NULL
     OR to_regclass('provenance.clinic_cloudrun_comdesk_rows') IS NOT NULL
     OR EXISTS (SELECT 1 FROM pg_policy WHERE polrelid='public.clinics'::regclass
                AND polname='clinic_cloudrun_projection_owner_select')
     OR EXISTS (SELECT 1 FROM pg_policy WHERE polrelid IN
                ('provenance.comdesk_original_rows'::regclass,'provenance.templates'::regclass)
                AND polname='clinic_cloudrun_projection_owner_select') THEN
    RAISE EXCEPTION 'Cloud Run projection object already exists; refuse replacement';
  END IF;

  IF EXISTS (
    SELECT 1 FROM pg_auth_members m
    JOIN pg_roles granted ON granted.oid = m.roleid
    JOIN pg_roles member ON member.oid = m.member
    WHERE granted.rolname = 'clinic_cloudrun_projection_owner'
       OR member.rolname = 'clinic_cloudrun_projection_owner'
  ) THEN
    RAISE EXCEPTION 'clinic_cloudrun_projection_owner must not inherit or grant other roles';
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_roles r ON r.oid = c.relowner
    WHERE n.nspname = 'public' AND c.relname = 'clinics'
      AND r.rolname = 'clinic_cloudrun_projection_owner'
  ) THEN
    RAISE EXCEPTION 'clinic_cloudrun_projection_owner must not own public.clinics';
  END IF;
END
$$;

CREATE ROLE clinic_cloudrun_projection_owner
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;

-- Deliberately no table-wide privilege and no access to evidence JSON. The role can read only
-- the input columns needed to construct the public projection. RLS is evaluated for this role.
-- maps_match_method/maps_checked_at and the function's treatment_status key are not read by
-- the READ ONLY adapter anywhere (src/repository/supabase_adapter.py); omitted here rather than
-- granted-but-unused, per the data-minimization review of this view's column set.
GRANT SELECT (
  id, uuid, clinic_name, phone, phone_norm, address, prefecture, medical_type, active,
  designation_date, recent_until, registration_reason, owner_equal,
  hp_status, hp_url, hp_rank, signal_count, hot_status, departments_json,
  treatments_json, signals_json, first_seen_at, last_seen_at, source_as_of_date,
  is_new, merged_into, merge_hold, maps_presence_status,
  maps_profile_url, maps_website_url,
  exclude_reason, effective_json
) ON TABLE public.clinics TO clinic_cloudrun_projection_owner;

CREATE POLICY clinic_cloudrun_projection_owner_select ON public.clinics
  FOR SELECT TO clinic_cloudrun_projection_owner USING (true);

-- jsonb rejects JSON text containing \u0000 and PostgreSQL's JSON operators also fail while
-- converting that value to database text. Replace only unescaped NUL escape tokens with a
-- row-specific marker before parsing. Literal backslash-u0000 text remains untouched. The
-- function returns only an allowlisted projection; it never returns the parsed source object.
-- Collision guard: CREATE OR REPLACE would silently redefine (or hide a naming collision
-- with) any pre-existing same-signature function. Abort instead, mirroring the role-
-- existence check above; a human must resolve a real collision, not have this migration
-- paper over it.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public' AND p.proname = 'clinic_cloudrun_effective_projection'
  ) THEN
    RAISE EXCEPTION 'public.clinic_cloudrun_effective_projection already exists; stop for review';
  END IF;
END
$$;

CREATE FUNCTION public.clinic_cloudrun_effective_projection(source_json text, clinic_id bigint)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
DECLARE
  -- Derived only from the already-visible clinic id, never from source_json.
  marker text := '__CLINIC_NUL_' || clinic_id::text || '__';
  sanitized text;
  parsed jsonb;
  companies jsonb := '[]'::jsonb;
  epark_url jsonb;
  epark_contract jsonb;
BEGIN
  IF source_json IS NULL THEN
    RETURN jsonb_build_object('_valid', false, '_marker', marker, 'values', '{}'::jsonb);
  END IF;
  -- A marker collision fails closed rather than changing source values silently.
  IF strpos(source_json, marker) > 0 THEN
    RETURN jsonb_build_object('_valid', false, '_marker', marker, 'values', '{}'::jsonb);
  END IF;

  sanitized := regexp_replace(
    source_json,
    $pattern$([^\\])((\\\\)*)\\u0000$pattern$,
    $replacement$\1\2$replacement$ || marker,
    'g'
  );
  parsed := sanitized::jsonb;
  IF jsonb_typeof(parsed) <> 'object' THEN
    RETURN jsonb_build_object('_valid', false, '_marker', marker, 'values', '{}'::jsonb);
  END IF;

  IF jsonb_typeof(parsed->'hp_production_companies') = 'array' THEN
    SELECT COALESCE(jsonb_agg(value), '[]'::jsonb)
      INTO companies
      FROM jsonb_array_elements(parsed->'hp_production_companies') AS item(value)
      WHERE jsonb_typeof(value) = 'string';
  END IF;
  IF jsonb_typeof(parsed->'epark_url') = 'string' THEN epark_url := parsed->'epark_url'; END IF;
  IF jsonb_typeof(parsed->'epark_contract') = 'string' THEN epark_contract := parsed->'epark_contract'; END IF;

  RETURN jsonb_build_object(
    '_valid', true,
    '_marker', marker,
    'values', jsonb_build_object(
      'hp_production_companies', companies,
      'epark_url', epark_url,
      'epark_contract', epark_contract
    )
  );
EXCEPTION WHEN others THEN
  -- Malformed JSON, unpaired surrogates, out-of-range numbers and other jsonb parser failures
  -- must not crash the READ ONLY UI. The adapter receives an empty projection for these rows.
  RETURN jsonb_build_object('_valid', false, '_marker', marker, 'values', '{}'::jsonb);
END
$function$;
-- The function only returns the documented allowlist, so execution does not expose raw JSON.
-- PostgreSQL grants EXECUTE to PUBLIC by default on function creation; revoke that before
-- granting only to the two roles that actually need it.
REVOKE EXECUTE ON FUNCTION public.clinic_cloudrun_effective_projection(text, bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.clinic_cloudrun_effective_projection(text, bigint)
  TO clinic_cloudrun_reader, clinic_cloudrun_projection_owner;

GRANT CREATE ON SCHEMA public TO clinic_cloudrun_projection_owner;

CREATE VIEW public.clinic_cloudrun_readonly WITH (security_barrier = true) AS
WITH projected AS (
  SELECT c.id, c.uuid, c.merged_into, c.first_seen_at, c.last_seen_at,
         c.source_as_of_date, c.is_new, c.departments_json, c.hp_status, c.hp_url,
         c.hp_rank, c.maps_website_url, c.clinic_name, c.phone, c.phone_norm, c.address, c.prefecture,
         c.medical_type, c.active, c.designation_date, c.recent_until,
         c.registration_reason, c.owner_equal, c.signal_count,
         c.hot_status, c.treatments_json, c.signals_json, c.merge_hold,
         c.maps_presence_status, c.maps_profile_url, c.exclude_reason,
         -- Expose only a boolean for the hospital exclusion, never the raw facility type.
         (c.effective_json LIKE '%"facility_type":"病院"%')
           AS is_hospital_or_center,
         public.clinic_cloudrun_effective_projection(c.effective_json, c.id) AS _effective
  FROM public.clinics c
)
SELECT
  id, uuid, merged_into, first_seen_at, last_seen_at, source_as_of_date,
  is_new, departments_json, hp_status, hp_url, hp_rank, maps_website_url,
  clinic_name, phone, phone_norm, address, prefecture, medical_type, active, designation_date,
  recent_until, registration_reason, owner_equal, signal_count,
  hot_status, treatments_json, signals_json, merge_hold, maps_presence_status,
  maps_profile_url, exclude_reason,
  (_effective->'values') AS effective_projection,
  (_effective->>'_marker') AS projection_marker,
  ((_effective->>'_valid')::boolean) AS projection_valid,
  is_hospital_or_center
FROM projected;

ALTER VIEW public.clinic_cloudrun_readonly OWNER TO clinic_cloudrun_projection_owner;
REVOKE CREATE ON SCHEMA public FROM clinic_cloudrun_projection_owner;

GRANT SELECT ON TABLE public.clinic_cloudrun_readonly TO clinic_cloudrun_reader;
REVOKE SELECT ON TABLE public.clinics FROM clinic_cloudrun_reader;
DO $$
DECLARE
  column_name text;
BEGIN
  FOR column_name IN
    SELECT a.attname
    FROM pg_attribute a
    WHERE a.attrelid = 'public.clinics'::regclass
      AND a.attnum > 0 AND NOT a.attisdropped
  LOOP
    EXECUTE format(
      'REVOKE SELECT (%I) ON TABLE public.clinics FROM clinic_cloudrun_reader', column_name
    );
  END LOOP;
  IF has_table_privilege('clinic_cloudrun_reader', 'public.clinics', 'SELECT')
     OR has_any_column_privilege('clinic_cloudrun_reader', 'public.clinics', 'SELECT') THEN
    RAISE EXCEPTION 'clinic_cloudrun_reader still has effective direct SELECT on public.clinics';
  END IF;
END
$$;

COMMENT ON VIEW public.clinic_cloudrun_readonly IS
  'IAP Cloud Run READ ONLY projection; excludes raw effective_json and evidence JSON. Owner role is NOLOGIN/NOBYPASSRLS and is not public.clinics owner.';

-- The Reader sees only approved Comdesk values at their original positions. Unknown headers,
-- doctor/schedule fields and free-text history remain empty; template mapping still works.
GRANT USAGE ON SCHEMA provenance TO clinic_cloudrun_projection_owner;
GRANT SELECT (id, clinic_id, template_id, uuid, row_json)
  ON provenance.comdesk_original_rows TO clinic_cloudrun_projection_owner;
GRANT SELECT (id, headers_json) ON provenance.templates TO clinic_cloudrun_projection_owner;
CREATE POLICY clinic_cloudrun_projection_owner_select ON provenance.comdesk_original_rows
  FOR SELECT TO clinic_cloudrun_projection_owner USING (true);
CREATE POLICY clinic_cloudrun_projection_owner_select ON provenance.templates
  FOR SELECT TO clinic_cloudrun_projection_owner USING (true);
GRANT CREATE ON SCHEMA provenance TO clinic_cloudrun_projection_owner;
CREATE VIEW provenance.clinic_cloudrun_comdesk_rows WITH (security_barrier=true) AS
SELECT o.id, o.clinic_id, o.template_id, o.uuid,
  CASE WHEN t.id IS NOT NULL AND jsonb_typeof(o.row_json)='array'
            AND jsonb_typeof(t.headers_json)='array'
       THEN jsonb_array_length(o.row_json)=jsonb_array_length(t.headers_json)
       ELSE false END AS projection_valid,
  CASE WHEN t.id IS NOT NULL AND jsonb_typeof(o.row_json)='array'
              AND jsonb_typeof(t.headers_json)='array'
              AND jsonb_array_length(o.row_json)=jsonb_array_length(t.headers_json)
       THEN COALESCE((SELECT jsonb_agg(
         CASE WHEN t.headers_json->>i IN
           ('UUID','種別','名前','カナ','郵便番号','都道府県','住所１','住所２','住所カナ',
            'Tel1','Tel2','Tel3','Tel4','FAX','URL','旧社名','リードソース','開業日')
           AND jsonb_typeof(o.row_json->i)='string' THEN o.row_json->i
           ELSE to_jsonb(''::text) END ORDER BY i)
         FROM generate_series(0,jsonb_array_length(o.row_json)-1) AS s(i)), '[]'::jsonb)
       ELSE '[]'::jsonb END AS row_json
FROM provenance.comdesk_original_rows o
LEFT JOIN provenance.templates t ON t.id=o.template_id;
ALTER VIEW provenance.clinic_cloudrun_comdesk_rows OWNER TO clinic_cloudrun_projection_owner;
REVOKE CREATE ON SCHEMA provenance FROM clinic_cloudrun_projection_owner;
GRANT SELECT ON provenance.clinic_cloudrun_comdesk_rows TO clinic_cloudrun_reader;
REVOKE SELECT ON provenance.comdesk_original_rows FROM clinic_cloudrun_reader;
REVOKE SELECT (id,clinic_id,template_id,row_json,uuid)
  ON provenance.comdesk_original_rows FROM clinic_cloudrun_reader;
DO $$
DECLARE column_name text;
BEGIN
  FOR column_name IN
    SELECT a.attname FROM pg_attribute a
    WHERE a.attrelid='provenance.comdesk_original_rows'::regclass
      AND a.attnum>0 AND NOT a.attisdropped
  LOOP
    EXECUTE format('REVOKE SELECT (%I) ON provenance.comdesk_original_rows FROM clinic_cloudrun_reader',
                   column_name);
  END LOOP;
END $$;

DO $$ BEGIN
  IF has_schema_privilege('clinic_cloudrun_projection_owner','public','CREATE')
     OR has_schema_privilege('clinic_cloudrun_projection_owner','provenance','CREATE')
     OR has_any_column_privilege('clinic_cloudrun_reader','provenance.comdesk_original_rows','SELECT')
     OR has_table_privilege('clinic_cloudrun_reader','provenance.comdesk_original_rows','SELECT') THEN
    RAISE EXCEPTION 'Projection role or Reader retains an unsafe effective privilege';
  END IF;
  IF EXISTS (
    SELECT 1 FROM (VALUES ('public.clinics'),('public.clinic_cloudrun_readonly'),
                 ('provenance.comdesk_original_rows'),('provenance.clinic_cloudrun_comdesk_rows')) AS x(relation)
    WHERE has_table_privilege('clinic_cloudrun_reader',x.relation,'INSERT')
       OR has_table_privilege('clinic_cloudrun_reader',x.relation,'UPDATE')
       OR has_table_privilege('clinic_cloudrun_reader',x.relation,'DELETE')
       OR has_table_privilege('clinic_cloudrun_reader',x.relation,'TRUNCATE')
  ) THEN
    RAISE EXCEPTION 'Reader retains write privileges on a projection or source relation';
  END IF;
END $$;
COMMIT;
