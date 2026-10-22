"""Narrow, fail-closed PostgreSQL reader for the Comdesk Step7 Preview UI."""
from __future__ import annotations

import os


PREVIEW_ROLE = "clinic_comdesk_preview_reader"
REQUIRED_COLUMNS = {
    "public.sales_entity_comdesk_identity": {"comdesk_uuid", "common_clinic_id"},
    "public.sales_entity_clinics": {"common_clinic_id", "clinic_id"},
    "provenance.comdesk_original_rows": {"id", "uuid", "clinic_id", "row_json", "template_id"},
    "public.clinics": {
        "id", "hp_url", "hp_rank", "base_json", "effective_json",
        "maps_presence_status", "maps_website_url", "merged_into",
    },
    "provenance.templates": {"id", "headers_json"},
    "provenance.manual_overrides": {"clinic_id", "field"},
}


class PreviewReaderConfigurationError(RuntimeError):
    """The connection does not satisfy the dedicated reader contract."""


def validate_preview_connection(conn) -> None:
    """Verify role, membership, read-only session, grants, RLS, and function boundary."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(
                "SELECT current_user, current_setting('default_transaction_read_only'), "
                "r.rolcanlogin,r.rolsuper,r.rolinherit,r.rolbypassrls,r.rolcreatedb,r.rolcreaterole,r.rolreplication "
                "FROM pg_roles r WHERE r.rolname=current_user"
            )
            identity = cur.fetchone()
            if not identity or identity[0] != PREVIEW_ROLE or identity[1] != "on" or identity[2:] != (
                True, False, False, False, False, False, False
            ):
                raise PreviewReaderConfigurationError("専用Preview Readerの接続属性が要件に一致しません。")

            cur.execute(
                "SELECT count(*) FROM pg_auth_members m "
                "WHERE m.member=(SELECT oid FROM pg_roles WHERE rolname=current_user) "
                "OR m.roleid=(SELECT oid FROM pg_roles WHERE rolname=current_user)"
            )
            if cur.fetchone()[0] != 0:
                raise PreviewReaderConfigurationError("専用Preview Readerにロールメンバーシップがあります。")

            cur.execute(
                "SELECT has_schema_privilege(current_user,'public','USAGE'), "
                "has_schema_privilege(current_user,'public','CREATE'), "
                "has_schema_privilege(current_user,'provenance','USAGE'), "
                "has_schema_privilege(current_user,'provenance','CREATE')"
            )
            if cur.fetchone() != (True, False, True, False):
                raise PreviewReaderConfigurationError("専用Preview Readerのschema権限が要件に一致しません。")

            for relation, expected in REQUIRED_COLUMNS.items():
                cur.execute(
                    "SELECT c.relrowsecurity, "
                    "has_table_privilege(current_user,c.oid,'SELECT'), "
                    "has_table_privilege(current_user,c.oid,'INSERT'), "
                    "has_table_privilege(current_user,c.oid,'UPDATE'), "
                    "has_table_privilege(current_user,c.oid,'DELETE'), "
                    "has_table_privilege(current_user,c.oid,'TRUNCATE'), "
                    "has_table_privilege(current_user,c.oid,'REFERENCES'), "
                    "has_table_privilege(current_user,c.oid,'TRIGGER'), "
                    "has_any_column_privilege(current_user,c.oid,'INSERT'), "
                    "has_any_column_privilege(current_user,c.oid,'UPDATE') "
                    "FROM pg_class c WHERE c.oid=%s::regclass",
                    (relation,),
                )
                privileges = cur.fetchone()
                if not privileges or privileges != (True, False, False, False, False, False, False, False, False, False):
                    raise PreviewReaderConfigurationError(f"{relation}のRLSまたは書込権限が要件に一致しません。")
                cur.execute(
                    "SELECT a.attname,has_column_privilege(current_user,a.attrelid,a.attnum,'SELECT') "
                    "FROM pg_attribute a WHERE a.attrelid=%s::regclass AND a.attnum>0 AND NOT a.attisdropped",
                    (relation,),
                )
                granted = {name for name, allowed in cur.fetchall() if allowed}
                if granted != expected:
                    raise PreviewReaderConfigurationError(f"{relation}のSELECT列が最小権限設計と一致しません。")
                cur.execute(
                    "SELECT count(*) FROM pg_policies "
                    "WHERE (schemaname||'.'||tablename)=%s AND cmd='SELECT' "
                    "AND current_user::name=ANY(roles)",
                    (relation,),
                )
                if cur.fetchone()[0] < 1:
                    raise PreviewReaderConfigurationError(f"{relation}にPreview Reader用SELECT policyがありません。")

            relation_names = list(REQUIRED_COLUMNS)
            cur.execute(
                "SELECT n.nspname,c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
                "AND n.nspname !~ '^pg_toast' AND c.relkind IN ('r','p','v','m','f') "
                "AND (has_table_privilege(current_user,c.oid,'SELECT') "
                "OR has_any_column_privilege(current_user,c.oid,'SELECT'))"
            )
            unexpected = {
                f"{schema}.{table}" for schema, table in cur.fetchall()
                if f"{schema}.{table}" not in relation_names
            }
            if unexpected:
                raise PreviewReaderConfigurationError("専用Preview Readerに対象外テーブルのSELECT権限があります。")

            cur.execute(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE c.relkind='S' AND n.nspname NOT IN ('pg_catalog','information_schema') "
                "AND n.nspname !~ '^pg_toast' "
                "AND (has_sequence_privilege(current_user,c.oid,'USAGE') "
                "OR has_sequence_privilege(current_user,c.oid,'SELECT') "
                "OR has_sequence_privilege(current_user,c.oid,'UPDATE'))"
            )
            if cur.fetchone()[0] != 0:
                raise PreviewReaderConfigurationError("専用Preview Readerに対象外シーケンス権限があります。")

            cur.execute(
                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE p.prosecdef AND n.nspname IN ('public','provenance') "
                "AND has_function_privilege(current_user,p.oid,'EXECUTE')"
            )
            if cur.fetchone()[0] != 0:
                raise PreviewReaderConfigurationError("実行可能なSECURITY DEFINER関数があるため接続を拒否しました。")


def build_comdesk_preview_repository():
    """Build only the read repository, never the application's general WRITE bundle."""
    from src.repository.backend import BACKEND_SUPABASE, active_backend
    from src.repository.supabase_adapter import connect, SupabaseClinicRepository, SupabaseHpResearchRepository
    from src.safety.preview_mode import comdesk_preview_only_enabled

    if not comdesk_preview_only_enabled():
        raise PreviewReaderConfigurationError("Preview専用モードが有効ではありません。")
    if active_backend() != BACKEND_SUPABASE:
        raise PreviewReaderConfigurationError("Preview専用モードではSupabase READ backendのみ利用できます。")
    url = os.environ.get("SUPABASE_RUNTIME_DB_URL", "").strip()
    if not url:
        raise PreviewReaderConfigurationError("専用Preview Readerの接続設定がありません。")
    conn = connect(url, autocommit=True)
    try:
        validate_preview_connection(conn)
    except Exception:
        conn.close()
        raise
    hp_repository = SupabaseHpResearchRepository(conn)
    return SupabaseClinicRepository(conn, hp_repository)
