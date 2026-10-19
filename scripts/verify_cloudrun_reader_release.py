"""Test the manual SQL package on a NEW disposable loopback PostgreSQL database only.

Uses synthetic fixtures from the reviewed PR #122 checkout. Never use production credentials.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

PACKAGE = Path(__file__).resolve().parents[1] / 'docs/cloudrun_reader_release'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--implementation', type=Path, required=True)
    args = parser.parse_args()
    dsn = os.environ['CLINIC_RELEASE_TEST_DSN']
    settings = conninfo_to_dict(dsn)
    assert settings.get('host') == '127.0.0.1', 'Only an explicit loopback host is allowed'
    assert not settings.get('hostaddr') or settings['hostaddr'] == '127.0.0.1'
    assert not settings.get('service'), 'Service-based routing is not allowed'
    original = (args.implementation / 'tests/fixtures/cloudrun_readonly_projection_pg.sql').read_text()
    setup, assertions = original.split('SET ROLE clinic_cloudrun_reader;', 1)
    setup = re.sub(r'^\\.*$', '', setup, flags=re.M)
    setup = re.sub(r'CREATE POLICY fixture_reader_select.*?;', '', setup, flags=re.S)
    setup = re.sub(r'CREATE POLICY fixture_projection_rls_limit.*?;', '', setup, flags=re.S)
    setup = re.sub(r'GRANT .*? TO clinic_cloudrun_reader;', '', setup, flags=re.S)
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute("SELECT to_regclass('public.clinics')").fetchone()[0] is None
        assert conn.execute("SELECT current_user").fetchone()[0] == 'postgres'
        conn.execute(setup)
        conn.execute('''
            CREATE ROLE anon NOLOGIN;
            CREATE ROLE authenticated NOLOGIN;
            CREATE ROLE service_role NOLOGIN;
            CREATE ROLE clinic_runtime NOLOGIN;
            ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon,authenticated,service_role;
            ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO anon,authenticated,service_role;
            GRANT SELECT ON public.clinics TO clinic_cloudrun_reader;
            GRANT USAGE ON SCHEMA public,provenance,hp_research,treatment TO clinic_runtime;
            GRANT ALL ON ALL TABLES IN SCHEMA public,provenance,hp_research,treatment TO clinic_runtime;
            CREATE POLICY fixture_runtime_rw ON public.clinics FOR ALL TO clinic_runtime USING(true) WITH CHECK(true);
        ''')
        baseline = conn.execute("SELECT policyname,qual,with_check FROM pg_policies WHERE policyname='fixture_runtime_rw'").fetchall()
        # Supabase's postgres role is not a PostgreSQL superuser. Exercise ownership transfer
        # with CREATEROLE and source ownership rather than relying on local superuser bypass.
        assert not conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user").fetchone()[0]
        conn.execute((PACKAGE / 'apply.sql').read_text())
        checks = conn.execute((PACKAGE / 'verify.sql').read_text())
        while True:
            columns = [column.name for column in checks.description]
            for row in checks.fetchall():
                for name, value in zip(columns, row):
                    if name.endswith('_ok'):
                        assert value is True, (name, row)
            if not checks.nextset():
                break
        assert conn.execute("SELECT policyname,qual,with_check FROM pg_policies WHERE policyname='fixture_runtime_rw'").fetchall() == baseline
        assert conn.execute("SELECT has_table_privilege('clinic_runtime','public.clinics','UPDATE')").fetchone()[0]
        for role in ('anon','authenticated','service_role'):
            for relation in ('public.clinic_cloudrun_readonly','provenance.clinic_cloudrun_comdesk_rows'):
                assert not conn.execute('SELECT has_table_privilege(%s,%s,%s)',(role,relation,'SELECT')).fetchone()[0]
            assert not conn.execute("SELECT has_function_privilege(%s,'public.clinic_cloudrun_effective_projection(text,bigint)','EXECUTE')",(role,)).fetchone()[0]
        conn.execute('''CREATE POLICY fixture_projection_rls_limit ON public.clinics
                        AS RESTRICTIVE FOR SELECT TO clinic_cloudrun_projection_owner USING(id<>999)''')
        with psycopg.connect(make_conninfo(dsn,user='clinic_cloudrun_reader'),autocommit=True) as reader:
            reader.execute(assertions)
            reader.execute((PACKAGE / 'reader_smoke.sql').read_text())
        subprocess.run([sys.executable, str(args.implementation / 'scripts/verify_cloudrun_readonly_projection_pg.py')],
            env={**os.environ,'CLINIC_READONLY_PROJECTION_TEST_DSN':make_conninfo(dsn,user='clinic_cloudrun_reader')}, check=True)
        try:
            conn.execute((PACKAGE / 'apply.sql').read_text())
        except psycopg.Error:
            conn.execute('ROLLBACK')
        else:
            raise AssertionError('Reapplication unexpectedly succeeded')
        try:
            conn.execute((PACKAGE / 'rollback.sql').read_text())
        except psycopg.Error:
            conn.execute('ROLLBACK')
        else:
            raise AssertionError('Rollback removed unrelated dependent policy')
        assert conn.execute("SELECT to_regclass('public.clinic_cloudrun_readonly') IS NOT NULL").fetchone()[0]
        conn.execute('DROP POLICY fixture_projection_rls_limit ON public.clinics')
        conn.execute((PACKAGE / 'rollback.sql').read_text())
        assert conn.execute("SELECT to_regclass('public.clinic_cloudrun_readonly') IS NULL").fetchone()[0]
        assert conn.execute("SELECT count(*) FROM public.clinics").fetchone()[0] == 11
        assert conn.execute("SELECT policyname,qual,with_check FROM pg_policies WHERE policyname='fixture_runtime_rw'").fetchall() == baseline
        assert not conn.execute("SELECT has_column_privilege('clinic_cloudrun_reader','public.clinics','effective_json','SELECT')").fetchone()[0]
        assert not conn.execute("SELECT has_column_privilege('clinic_cloudrun_reader','provenance.comdesk_original_rows','row_json','SELECT')").fetchone()[0]
    print('PASS: Supabase default ACL isolation, seven-relation package, RLS, app regression, atomic abort, scoped rollback, CRM policy preservation')


if __name__ == '__main__':
    main()
