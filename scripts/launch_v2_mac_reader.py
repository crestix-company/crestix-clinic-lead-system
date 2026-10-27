"""Mac preview launcher: require a dedicated least-privilege Supabase reader.

Credentials are stored outside the repository and are never printed.
This launcher deliberately does not fall back to the runtime writer or SQLite.
"""
import argparse
import os
from pathlib import Path
import shlex
import subprocess
import sys


READER_FILE = Path.home() / ".config/clinic-lead/preview-reader.env"
REQUIRED_TABLES = (
    "public.clinics",
    "provenance.source_records",
    "provenance.comdesk_original_rows",
    "provenance.google_maps_results",
    "hp_research.clinic_hp_research",
    "email_research.clinic_email_status",
    "email_research.email_candidates",
)
WRITE_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def load_reader_dsn(path=READER_FILE):
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"専用Reader設定がありません: {path}")
    if path.stat().st_mode & 0o077:
        raise RuntimeError("専用Reader設定の権限を600にしてください。")
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = shlex.split(line)
        if parts and parts[0] == "export":
            parts = parts[1:]
        if len(parts) != 1 or "=" not in parts[0]:
            raise RuntimeError("Reader設定の形式が不正です。")
        key, value = parts[0].split("=", 1)
        if key != "SUPABASE_RUNTIME_DB_URL" or key in values:
            raise RuntimeError("Reader設定はSUPABASE_RUNTIME_DB_URLの1行だけにしてください。")
        values[key] = value
    if not values.get("SUPABASE_RUNTIME_DB_URL"):
        raise RuntimeError("Reader接続URLが未設定です。")
    return values["SUPABASE_RUNTIME_DB_URL"]


def verify_reader(dsn, *, connect=None):
    if connect is None:
        import psycopg
        connect = psycopg.connect
    with connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT current_user, r.rolsuper, r.rolbypassrls, r.rolcreatedb,
                       r.rolcreaterole, has_database_privilege(current_user, current_database(), 'CREATE')
                FROM pg_roles r WHERE r.rolname=current_user
            """)
            role, superuser, bypassrls, createdb, createrole, dbcreate = cur.fetchone()
            if role == "clinic_runtime" or any((superuser, bypassrls, createdb, createrole, dbcreate)):
                raise RuntimeError("Writer/管理者権限の接続はプレビューに使用できません。")
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_namespace n
                    WHERE n.nspname NOT LIKE 'pg_%'
                      AND n.nspname <> 'information_schema'
                      AND has_schema_privilege(current_user, n.oid, 'CREATE')
                )
            """)
            if cur.fetchone()[0]:
                raise RuntimeError("スキーマ作成権限があります。専用Readerを使用してください。")
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE c.relkind IN ('r','p','v','m','f')
                      AND n.nspname NOT LIKE 'pg_%'
                      AND n.nspname <> 'information_schema'
                      AND (
                        has_table_privilege(current_user,c.oid,'INSERT')
                        OR has_table_privilege(current_user,c.oid,'UPDATE')
                        OR has_table_privilege(current_user,c.oid,'DELETE')
                        OR has_table_privilege(current_user,c.oid,'TRUNCATE')
                        OR has_table_privilege(current_user,c.oid,'REFERENCES')
                        OR has_table_privilege(current_user,c.oid,'TRIGGER')
                      )
                )
            """)
            if cur.fetchone()[0]:
                raise RuntimeError("DBオブジェクトに書き込み権限があります。起動を停止しました。")
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE c.relkind='S'
                      AND n.nspname NOT LIKE 'pg_%'
                      AND n.nspname <> 'information_schema'
                      AND (
                        has_sequence_privilege(current_user,c.oid,'UPDATE')
                        OR has_sequence_privilege(current_user,c.oid,'USAGE')
                      )
                )
            """)
            if cur.fetchone()[0]:
                raise RuntimeError("シーケンス更新権限があります。起動を停止しました。")
            for table in REQUIRED_TABLES:
                cur.execute("SELECT to_regclass(%s)", (table,))
                if cur.fetchone()[0] is None:
                    raise RuntimeError(f"必要なテーブルがありません: {table}")
                cur.execute("SELECT has_table_privilege(current_user, %s, 'SELECT')", (table,))
                if not cur.fetchone()[0]:
                    raise RuntimeError(f"参照権限がありません: {table}")
            cur.execute("SELECT count(*) FROM public.clinics")
            count = cur.fetchone()[0]
            if count == 0:
                raise RuntimeError("医院データを参照できません。RLSポリシーを確認してください。")
        conn.rollback()
    return role, count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--port", type=int, default=8502)
    parser.add_argument("--strict-port", action="store_true")
    args = parser.parse_args()
    dsn = load_reader_dsn()
    role, count = verify_reader(dsn)
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    for key in (
        "SUPABASE_RUNTIME_DB_URL", "CLINIC_DATA_BACKEND", "CLINIC_WRITE_BACKEND",
        "CLINIC_DB_PATH", "CLINIC_DEMO_DB_PATH",
    ):
        env.pop(key, None)
    env.update({
        "SUPABASE_RUNTIME_DB_URL": dsn,
        "CLINIC_DATA_BACKEND": "supabase",
        "CLINIC_WRITE_BACKEND": "supabase",
        "CLINIC_COMDESK_PREVIEW_ONLY": "1",
        "CLINIC_READ_ONLY_UI": "1",
        "CLINIC_COMMON_IDENTITY_ENABLED": "0",
        "EMAIL_AUTO_RUN_ENABLED": "false",
        "OUTREACH_GMAIL_DRAFTS_ENABLED": "false",
    })
    print(f"Supabase Reader確認OK: role={role}, 医院={count}件", flush=True)
    args_list = [sys.executable, str(root / "scripts/launch_v2.py"), "--port", str(args.port), "--strict-port"]
    if args.headless:
        args_list.append("--headless")
    return subprocess.call(args_list, cwd=root, env=env)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError) as exc:
        print(f"安全な起動を中止しました: {exc}", file=sys.stderr)
        sys.exit(1)
