\set ON_ERROR_STOP on

DO $$
BEGIN
  IF inet_server_addr() IS DISTINCT FROM '127.0.0.1'::inet THEN
    RAISE EXCEPTION 'This destructive fixture is restricted to a local PostgreSQL server';
  END IF;
END
$$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='clinic_cloudrun_reader') THEN
    CREATE ROLE clinic_cloudrun_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='clinic_cloudrun_projection_owner') THEN
    CREATE ROLE clinic_cloudrun_projection_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  END IF;
END
$$;

CREATE TABLE public.clinics (
  id bigint PRIMARY KEY,
  uuid text NOT NULL DEFAULT '',
  clinic_name text NOT NULL DEFAULT '',
  phone text NOT NULL DEFAULT '',
  address text NOT NULL DEFAULT '',
  prefecture text NOT NULL DEFAULT '',
  medical_type text NOT NULL DEFAULT '',
  active boolean NOT NULL DEFAULT true,
  designation_date text NOT NULL DEFAULT '',
  recent_until text NOT NULL DEFAULT '',
  registration_reason text NOT NULL DEFAULT '',
  owner_equal boolean,
  age_probability double precision,
  hp_status text NOT NULL DEFAULT 'VERIFIED',
  hp_url text NOT NULL DEFAULT 'https://clinic.example.jp',
  hp_rank text NOT NULL DEFAULT 'D',
  signal_count integer NOT NULL DEFAULT 0,
  hot_status text NOT NULL DEFAULT '通常',
  departments_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  treatments_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  signals_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  first_seen_at text NOT NULL DEFAULT '2026-01-01',
  last_seen_at text NOT NULL DEFAULT '2026-01-01',
  source_as_of_date text NOT NULL DEFAULT '2026-01-01',
  is_new boolean NOT NULL DEFAULT false,
  merged_into bigint,
  tel_match_key text NOT NULL DEFAULT '',
  merge_hold boolean NOT NULL DEFAULT false,
  maps_presence_status text NOT NULL DEFAULT '',
  maps_profile_url text NOT NULL DEFAULT '',
  maps_website_url text NOT NULL DEFAULT '',
  maps_match_method text NOT NULL DEFAULT '',
  maps_checked_at text NOT NULL DEFAULT '',
  exclude_reason text NOT NULL DEFAULT '',
  effective_json text
);

ALTER TABLE public.clinics ENABLE ROW LEVEL SECURITY;
CREATE POLICY fixture_projection_rls_limit ON public.clinics
  AS RESTRICTIVE FOR SELECT TO clinic_cloudrun_projection_owner USING (id <> 999);

INSERT INTO public.clinics (id, clinic_name, phone, address, prefecture, medical_type,
                            departments_json, treatments_json, signals_json, effective_json)
VALUES
 (1, 'Projection Clinic', '03-0000-0001', '東京都千代田区1-1', '東京都', '医科',
  '["内科"]', '["胃カメラ"]', '["Web予約"]',
  $json${"facility_type":"診療所","hp_production_companies":["Alpha",1,{"x":1},null],"epark_url":"A\u0000B","owner_name":"PRIVATE_OWNER"}$json$),
 (2, 'Second Clinic', '06-0000-0002', '大阪府大阪市1-1', '大阪府', '歯科',
  '["歯科"]', '["矯正歯科"]', '[]',
  $json${"facility_type":"診療所","hp_production_companies":["Beta"],"epark_url":"A\\u0000B","owner_name":"PRIVATE_OWNER"}$json$),
 (3, 'Null JSON Clinic', '', '', '北海道', '医科', '[]', '[]', '[]', NULL),
 (4, 'Malformed JSON Clinic', '', '', '沖縄県', '医科', '[]', '[]', '[]', '{"facility_type":'),
 (5, 'Missing Key Clinic', '', '', '福岡県', '医科', '[]', '[]', '[]', '{}'),
 (6, 'Wrong Type Clinic', '', '', '愛知県', '医科', '[]', '[]', '[]',
  '{"facility_type":42,"hp_production_companies":"Alpha"}'),
 (7, 'Array Root Clinic', '', '', '宮城県', '医科', '[]', '[]', '[]', '[]'),
 (8, 'Unpaired Surrogate Clinic', '', '', '神奈川県', '医科', '[]', '[]', '[]',
  $json${"other":"\uD800","facility_type":"診療所"}$json$),
 (999, 'RLS Hidden Clinic', '', '', '東京都', '医科', '[]', '[]', '[]',
  '{"facility_type":"診療所"}'),
 (1000, 'Facility Only Hospital', '', '', '東京都', '医科', '[]', '[]', '[]',
  '{"facility_type":"病院"}'),
 (1001, 'Out Of Range Number Clinic', '', '', '東京都', '医科', '[]', '[]', '[]',
  '{"facility_type":"診療所","large":1e1000000}');

\ir ../../supabase/migrations/20261019195000_cloudrun_readonly_clinic_view.sql

CREATE SCHEMA hp_research;
CREATE TABLE hp_research.clinic_hp_research (
  clinic_id bigint PRIMARY KEY, machine_hp_rank text, fetch_status text,
  hp_url text, final_url text, treatment_categories jsonb
);
INSERT INTO hp_research.clinic_hp_research VALUES
  (1, 'A', 'OK', 'https://clinic.example.jp', 'https://clinic.example.jp', '["胃カメラ"]'),
  (2, 'B', 'OK', 'https://second.example.jp', 'https://second.example.jp', '[]');
ALTER TABLE hp_research.clinic_hp_research ENABLE ROW LEVEL SECURITY;
CREATE POLICY fixture_reader_select ON hp_research.clinic_hp_research
  FOR SELECT TO clinic_cloudrun_reader USING (true);
GRANT USAGE ON SCHEMA hp_research TO clinic_cloudrun_reader;
GRANT SELECT (clinic_id, machine_hp_rank, fetch_status, hp_url, final_url, treatment_categories)
  ON hp_research.clinic_hp_research TO clinic_cloudrun_reader;

CREATE SCHEMA treatment;
CREATE TABLE treatment.clinic_treatment_research (
  clinic_id bigint, research_status text, treatment_category_name text
);
CREATE TABLE treatment.clinic_research_status (clinic_id bigint, research_status text);
INSERT INTO treatment.clinic_treatment_research VALUES (1, 'CONFIRMED', '胃カメラ');
ALTER TABLE treatment.clinic_treatment_research ENABLE ROW LEVEL SECURITY;
ALTER TABLE treatment.clinic_research_status ENABLE ROW LEVEL SECURITY;
CREATE POLICY fixture_reader_select ON treatment.clinic_treatment_research
  FOR SELECT TO clinic_cloudrun_reader USING (true);
CREATE POLICY fixture_reader_select ON treatment.clinic_research_status
  FOR SELECT TO clinic_cloudrun_reader USING (true);
GRANT USAGE ON SCHEMA treatment TO clinic_cloudrun_reader;
GRANT SELECT (clinic_id, research_status, treatment_category_name)
  ON treatment.clinic_treatment_research TO clinic_cloudrun_reader;
GRANT SELECT (clinic_id, research_status)
  ON treatment.clinic_research_status TO clinic_cloudrun_reader;

CREATE SCHEMA provenance;
CREATE TABLE provenance.comdesk_original_rows (
  id bigint PRIMARY KEY, clinic_id bigint, template_id bigint, row_json jsonb, uuid text
);
CREATE TABLE provenance.templates (id bigint PRIMARY KEY, headers_json jsonb, mapping_json jsonb);
INSERT INTO provenance.templates VALUES
  (7, '["UUID","種別","名前","カナ","郵便番号","都道府県","住所１","住所２","住所カナ","Tel1","Tel2","Tel3","Tel4","FAX","URL","備考","旧社名","リードソース","履歴","記事名","休診日","診療日","午前始","午前終","午後始","午後終","院長名","開業日"]', '{}');
INSERT INTO provenance.comdesk_original_rows VALUES
  (1, 1, 7, '["uuid-1","","Original Name","","100-0001","東京都","千代田区1-1","","","03-1234-5678","","","","","https://original.example.jp","","","","","","休診","月火水木金","09:00","12:00","13:00","17:00","Original Doctor",""]', 'uuid-1');
ALTER TABLE provenance.comdesk_original_rows ENABLE ROW LEVEL SECURITY;
ALTER TABLE provenance.templates ENABLE ROW LEVEL SECURITY;
CREATE POLICY fixture_reader_select ON provenance.comdesk_original_rows
  FOR SELECT TO clinic_cloudrun_reader USING (true);
CREATE POLICY fixture_reader_select ON provenance.templates
  FOR SELECT TO clinic_cloudrun_reader USING (true);
GRANT USAGE ON SCHEMA provenance TO clinic_cloudrun_reader;
GRANT SELECT (id, clinic_id, template_id, row_json, uuid)
  ON provenance.comdesk_original_rows TO clinic_cloudrun_reader;
GRANT SELECT (id, headers_json, mapping_json) ON provenance.templates TO clinic_cloudrun_reader;

CREATE TABLE public.clinic_email_enrichment (
  clinic_id bigint, email text, status text, verified_on_official boolean
);
INSERT INTO public.clinic_email_enrichment VALUES
  (1, 'official@example.jp', 'VERIFIED_EMAIL', true),
  (1, 'candidate@example.jp', 'SEARCH_CANDIDATE', true),
  (2, 'unverified@example.jp', 'VERIFIED_EMAIL', false);
ALTER TABLE public.clinic_email_enrichment ENABLE ROW LEVEL SECURITY;
CREATE POLICY fixture_reader_select ON public.clinic_email_enrichment
  FOR SELECT TO clinic_cloudrun_reader USING (
    status='VERIFIED_EMAIL' AND verified_on_official IS TRUE AND NULLIF(BTRIM(email),'') IS NOT NULL
  );
GRANT SELECT (clinic_id, email, status, verified_on_official)
  ON public.clinic_email_enrichment TO clinic_cloudrun_reader;

SET ROLE clinic_cloudrun_reader;

DO $$
DECLARE visible_rows integer;
DECLARE projection jsonb;
BEGIN
  IF has_table_privilege(current_user, 'public.clinics', 'SELECT') THEN
    RAISE EXCEPTION 'Reader retains direct table SELECT';
  END IF;
  IF has_column_privilege(current_user, 'public.clinics', 'effective_json', 'SELECT') THEN
    RAISE EXCEPTION 'Reader retains effective_json SELECT';
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema='public' AND table_name='clinic_cloudrun_readonly'
               AND column_name IN ('effective_json','base_json','treatment_evidence')) THEN
    RAISE EXCEPTION 'Raw or evidence JSON leaked into the view';
  END IF;
  SELECT count(*) INTO visible_rows FROM public.clinic_cloudrun_readonly;
  IF visible_rows <> 10 THEN
    RAISE EXCEPTION 'RLS is bypassed or fixture row count changed: %', visible_rows;
  END IF;
  IF EXISTS (SELECT 1 FROM public.clinic_cloudrun_readonly WHERE id=999) THEN
    RAISE EXCEPTION 'The view owner bypassed the restrictive RLS policy';
  END IF;
  SELECT effective_projection INTO projection FROM public.clinic_cloudrun_readonly WHERE id=1;
  IF projection->>'facility_type' <> '診療所'
     OR projection->'hp_production_companies' <> '["Alpha"]'::jsonb
     OR projection->>'epark_url' NOT LIKE '%__CLINIC_NUL_1_%' THEN
    RAISE EXCEPTION 'Allowlisted projection or mixed-array filtering failed: %', projection;
  END IF;
  SELECT effective_projection INTO projection FROM public.clinic_cloudrun_readonly WHERE id=2;
  IF projection->>'epark_url' <> ('A' || chr(92) || 'u0000B') THEN
    RAISE EXCEPTION 'Literal backslash-u0000 was changed: %', projection;
  END IF;
  IF (SELECT projection_valid FROM public.clinic_cloudrun_readonly WHERE id=3) IS DISTINCT FROM false
     OR (SELECT projection_valid FROM public.clinic_cloudrun_readonly WHERE id=4) IS DISTINCT FROM false
     OR (SELECT projection_valid FROM public.clinic_cloudrun_readonly WHERE id=7) IS DISTINCT FROM false
     OR (SELECT projection_valid FROM public.clinic_cloudrun_readonly WHERE id=8) IS DISTINCT FROM false
     OR (SELECT projection_valid FROM public.clinic_cloudrun_readonly WHERE id=1001) IS DISTINCT FROM false THEN
    RAISE EXCEPTION 'Invalid JSON was not caught and marked invalid';
  END IF;
  IF (SELECT effective_projection->>'facility_type' FROM public.clinic_cloudrun_readonly WHERE id=5) <> ''
     OR (SELECT effective_projection->'hp_production_companies' FROM public.clinic_cloudrun_readonly WHERE id=5) <> '[]'::jsonb
     OR (SELECT effective_projection->>'facility_type' FROM public.clinic_cloudrun_readonly WHERE id=6) <> ''
     OR (SELECT effective_projection->'hp_production_companies' FROM public.clinic_cloudrun_readonly WHERE id=6) <> '[]'::jsonb THEN
    RAISE EXCEPTION 'Missing/type-mismatched projection values were not defaulted safely';
  END IF;
  SELECT public.clinic_cloudrun_effective_projection(
    '{"facility_type":"診療所","owner_name":"DO_NOT_RETURN"}', 12345
  ) INTO projection;
  IF projection::text LIKE '%DO_NOT_RETURN%' OR projection::text LIKE '%owner_name%' THEN
    RAISE EXCEPTION 'Projection function exposed a non-allowlisted source key';
  END IF;
END
$$;

DO $$
BEGIN
  BEGIN
    PERFORM effective_json FROM public.clinics LIMIT 1;
    RAISE EXCEPTION 'Reader can directly select effective_json';
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;
  END;
END
$$;

RESET ROLE;
