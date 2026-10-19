-- Cloud Run READ ONLY clinic projection.
-- PROPOSAL / integration migration only: do not apply to production without B/security review.
-- The Reader receives the view, never SELECT on public.clinics. The NOLOGIN view owner reads
-- only the source columns needed by this view, is not a table owner, and has NOBYPASSRLS.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'clinic_cloudrun_projection_owner') THEN
    CREATE ROLE clinic_cloudrun_projection_owner
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  ELSIF EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname = 'clinic_cloudrun_projection_owner'
      AND (rolcanlogin OR rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole OR rolinherit)
  ) THEN
    RAISE EXCEPTION 'clinic_cloudrun_projection_owner exists with unsafe role attributes';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relname = 'clinics' AND c.relrowsecurity
  ) THEN
    RAISE EXCEPTION 'public.clinics must already have RLS enabled';
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

-- Deliberately no table-wide privilege and no access to evidence JSON. The role can read only
-- the input columns needed to construct the public projection. RLS is evaluated for this role.
GRANT SELECT (
  id, uuid, clinic_name, phone, address, prefecture, medical_type, active,
  designation_date, recent_until, registration_reason, owner_equal, age_probability,
  hp_status, hp_url, hp_rank, signal_count, hot_status, departments_json,
  treatments_json, signals_json, first_seen_at, last_seen_at, source_as_of_date,
  is_new, merged_into, tel_match_key, merge_hold, maps_presence_status,
  maps_profile_url, maps_website_url, maps_match_method, maps_checked_at,
  exclude_reason, effective_json
) ON TABLE public.clinics TO clinic_cloudrun_projection_owner;

CREATE POLICY clinic_cloudrun_projection_owner_select ON public.clinics
  FOR SELECT TO clinic_cloudrun_projection_owner USING (true);

-- jsonb rejects JSON text containing \u0000 and PostgreSQL's JSON operators also fail while
-- converting that value to database text. Replace only unescaped NUL escape tokens with a
-- row-specific marker before parsing. Literal backslash-u0000 text remains untouched. The
-- function returns only an allowlisted projection; it never returns the parsed source object.
CREATE OR REPLACE FUNCTION public.clinic_cloudrun_effective_projection(source_json text, clinic_id bigint)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
DECLARE
  marker text := '__CLINIC_NUL_' || clinic_id::text || '_' || md5(COALESCE(source_json, '')) || '__';
  sanitized text;
  parsed jsonb;
  companies jsonb := '[]'::jsonb;
  facility jsonb := '""'::jsonb;
  epark_url jsonb;
  epark_contract jsonb;
  treatment_status jsonb;
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

  IF jsonb_typeof(parsed->'facility_type') = 'string' THEN
    facility := parsed->'facility_type';
  END IF;
  IF jsonb_typeof(parsed->'hp_production_companies') = 'array' THEN
    SELECT COALESCE(jsonb_agg(value), '[]'::jsonb)
      INTO companies
      FROM jsonb_array_elements(parsed->'hp_production_companies') AS item(value)
      WHERE jsonb_typeof(value) = 'string';
  END IF;
  IF jsonb_typeof(parsed->'epark_url') = 'string' THEN epark_url := parsed->'epark_url'; END IF;
  IF jsonb_typeof(parsed->'epark_contract') = 'string' THEN epark_contract := parsed->'epark_contract'; END IF;
  IF jsonb_typeof(parsed->'treatment_status') = 'string' THEN treatment_status := parsed->'treatment_status'; END IF;

  RETURN jsonb_build_object(
    '_valid', true,
    '_marker', marker,
    'values', jsonb_build_object(
      'facility_type', facility,
      'hp_production_companies', companies,
      'epark_url', epark_url,
      'epark_contract', epark_contract,
      'treatment_status', treatment_status
    )
  );
EXCEPTION WHEN others THEN
  -- Malformed JSON, unpaired surrogates, out-of-range numbers and other jsonb parser failures
  -- must not crash the READ ONLY UI. The adapter receives an empty projection for these rows.
  RETURN jsonb_build_object('_valid', false, '_marker', marker, 'values', '{}'::jsonb);
END
$function$;

-- The function only returns the documented allowlist, so execution does not expose raw JSON.
GRANT EXECUTE ON FUNCTION public.clinic_cloudrun_effective_projection(text, bigint)
  TO clinic_cloudrun_reader, clinic_cloudrun_projection_owner;

GRANT CREATE ON SCHEMA public TO clinic_cloudrun_projection_owner;

CREATE VIEW public.clinic_cloudrun_readonly WITH (security_barrier = true) AS
WITH projected AS (
  SELECT c.id, c.uuid, c.merged_into, c.tel_match_key, c.first_seen_at, c.last_seen_at,
         c.source_as_of_date, c.is_new, c.departments_json, c.hp_status, c.hp_url,
         c.hp_rank, c.maps_website_url, c.clinic_name, c.phone, c.address, c.prefecture,
         c.medical_type, c.active, c.designation_date, c.recent_until,
         c.registration_reason, c.owner_equal, c.age_probability, c.signal_count,
         c.hot_status, c.treatments_json, c.signals_json, c.merge_hold,
         c.maps_presence_status, c.maps_profile_url, c.maps_match_method,
         c.maps_checked_at, c.exclude_reason,
         substring(c.effective_json FROM '"facility_type"[[:space:]]*:[[:space:]]*"([^"]*)"')
           AS effective_facility_type,
         public.clinic_cloudrun_effective_projection(c.effective_json, c.id) AS _effective
  FROM public.clinics c
)
SELECT
  id, uuid, merged_into, tel_match_key, first_seen_at, last_seen_at, source_as_of_date,
  is_new, departments_json, hp_status, hp_url, hp_rank, maps_website_url,
  clinic_name, phone, address, prefecture, medical_type, active, designation_date,
  recent_until, registration_reason, owner_equal, age_probability, signal_count,
  hot_status, treatments_json, signals_json, merge_hold, maps_presence_status,
  maps_profile_url, maps_match_method, maps_checked_at, exclude_reason,
  (_effective->'values') AS effective_projection,
  (_effective->>'_marker') AS projection_marker,
  ((_effective->>'_valid')::boolean) AS projection_valid,
  effective_facility_type
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
