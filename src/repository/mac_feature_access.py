"""Private, feature-scoped Mac connections. No runtime-writer fallback."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
from urllib.parse import unquote, urlsplit

from src.repository.email_database import _database_identity


CONFIG_DIR = Path.home() / ".config" / "clinic-lead"
WRITERS_FILE = CONFIG_DIR / "feature-writers.env"
COMDESK_READER_FILE = CONFIG_DIR / "comdesk-reader.env"
WRITER_ROLES = {
    "comdesk_import": ("SUPABASE_COMDESK_IMPORT_WRITER_DB_URL", "clinic_comdesk_import_writer"),
    "kouseikyoku_import": ("SUPABASE_KOUSEIKYOKU_IMPORT_WRITER_DB_URL", "clinic_kouseikyoku_import_writer"),
    "maps_import": ("SUPABASE_MAPS_IMPORT_WRITER_DB_URL", "clinic_maps_import_writer"),
    "comdesk_sync": ("SUPABASE_COMDESK_SYNC_WRITER_DB_URL", "clinic_comdesk_sync_writer"),
}
READER_KEY = "SUPABASE_COMDESK_EXPORT_READER_DB_URL"
READER_ROLE = "clinic_comdesk_export_reader"
COMDESK_READER_COLUMNS = {
    "public.clinic_cloudrun_readonly": ("id", "hp_url", "maps_website_url"),
    "public.clinics": (
        "id", "uuid", "merged_into", "merge_hold", "active", "exclude_reason",
        "effective_json", "clinic_name", "base_json", "tel_match_key", "first_seen_at",
        "last_seen_at", "source_as_of_date", "is_new", "departments_json",
        "hp_status", "hp_url", "hp_rank", "maps_website_url", "maps_presence_status",
    ),
    "research.research_jobs": ("id", "kind", "status", "created_at"),
    "research.research_job_items": ("job_id", "clinic_id", "state", "result"),
    "research.research_results": ("clinic_id", "result_json"),
    "hp_research.clinic_hp_research": (
        "clinic_id", "machine_hp_rank", "fetch_status", "hp_url", "final_url", "treatment_categories",
    ),
    "provenance.hp_human_reviews": ("id", "clinic_id", "research_job_id", "human_decision", "reviewed_at"),
    "provenance.source_records": ("id", "clinic_id", "source"),
    "provenance.comdesk_original_rows": ("id", "clinic_id", "uuid", "row_json", "template_id"),
    "provenance.match_reviews": ("source_record_id", "status", "candidates_json"),
    "provenance.templates": ("id", "headers_json", "mapping_json"),
    "provenance.manual_overrides": ("clinic_id", "field"),
    "provenance.change_history": ("id",),
    "public.clinic_email_enrichment": ("clinic_id", "email", "status", "verified_on_official"),
    "public.sales_entity_clinics": ("clinic_id", "common_clinic_id"),
    "public.sales_entity_comdesk_identity": ("common_clinic_id", "comdesk_uuid"),
}


class FeatureConnectionUnavailable(RuntimeError):
    pass


def _private_values(path: Path, allowed: set[str]) -> dict[str, str]:
    if not path.is_file() or path.is_symlink():
        return {}
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise FeatureConnectionUnavailable("機能別接続設定はmode 600にしてください。")
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            raise FeatureConnectionUnavailable("機能別接続設定の形式が不正です。") from exc
        if parts and parts[0] == "export":
            parts = parts[1:]
        if len(parts) != 1 or "=" not in parts[0]:
            raise FeatureConnectionUnavailable("機能別接続設定の形式が不正です。")
        key, value = parts[0].split("=", 1)
        if key not in allowed or key in values:
            raise FeatureConnectionUnavailable("機能別接続設定に不正または重複した項目があります。")
        values[key] = value.strip()
    return values


def _validated_dsn(value: str, *, expected_role: str, runtime_url: str) -> str:
    if not value or not runtime_url:
        raise FeatureConnectionUnavailable("専用接続またはClinic Runtimeの設定がありません。")
    target = _database_identity(value)
    runtime = _database_identity(runtime_url)
    if target is None or runtime is None or target != runtime:
        raise FeatureConnectionUnavailable("専用接続とClinic RuntimeのSupabaseプロジェクトを照合できません。")
    if value == runtime_url:
        raise FeatureConnectionUnavailable("一般Writer接続を機能別接続として使用できません。")
    parsed = urlsplit(value)
    user = unquote(parsed.username or "")
    project = target[1] if target[0] == "supabase-project" else None
    allowed = {expected_role}
    if project:
        allowed.add(f"{expected_role}.{project}")
    if user not in allowed:
        raise FeatureConnectionUnavailable("機能別接続のロール名が一致しません。")
    return value


def feature_writer_configured(feature: str) -> bool:
    key, role = WRITER_ROLES[feature]
    try:
        value = _private_values(WRITERS_FILE, {item[0] for item in WRITER_ROLES.values()}).get(key, "")
        _validated_dsn(value, expected_role=role, runtime_url=os.environ.get("SUPABASE_RUNTIME_DB_URL", ""))
        return True
    except FeatureConnectionUnavailable:
        return False


def feature_writer_repositories(feature: str):
    """Open only the named writer; verify the server role before any DML."""
    from src.repository.write_backend import build_write_repositories, WRITE_BACKEND_SUPABASE

    key, role = WRITER_ROLES[feature]
    value = _private_values(WRITERS_FILE, {item[0] for item in WRITER_ROLES.values()}).get(key, "")
    dsn = _validated_dsn(value, expected_role=role, runtime_url=os.environ.get("SUPABASE_RUNTIME_DB_URL", ""))
    bundle = build_write_repositories(WRITE_BACKEND_SUPABASE, supabase_url=dsn)
    connection = bundle.clinics._conn
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            actual = cursor.fetchone()[0]
        connection.rollback()
        if actual != role:
            raise FeatureConnectionUnavailable("機能別WriterのDBロールが一致しません。")
        return bundle
    except Exception:
        connection.close()
        raise


def comdesk_reader_configured() -> bool:
    try:
        value = _private_values(COMDESK_READER_FILE, {READER_KEY}).get(READER_KEY, "")
        _validated_dsn(value, expected_role=READER_ROLE, runtime_url=os.environ.get("SUPABASE_RUNTIME_DB_URL", ""))
        return True
    except FeatureConnectionUnavailable:
        return False


def comdesk_reader_store():
    """Build the Step6/7 READ ONLY repository; never substitute another role."""
    from src.repository.backend import build_repositories
    from src.repository.runtime_store import SupabaseRuntimeStore
    from scripts.launch_v2_mac_reader import verify_reader

    value = _private_values(COMDESK_READER_FILE, {READER_KEY}).get(READER_KEY, "")
    dsn = _validated_dsn(value, expected_role=READER_ROLE, runtime_url=os.environ.get("SUPABASE_RUNTIME_DB_URL", ""))
    role, _count = verify_reader(dsn, required_tables=tuple(COMDESK_READER_COLUMNS),
                                 required_columns=COMDESK_READER_COLUMNS,
                                 count_relation="public.clinics")
    if role != READER_ROLE:
        raise FeatureConnectionUnavailable("Comdesk ReaderのDBロールが一致しません。")
    return SupabaseRuntimeStore(build_repositories("supabase", supabase_url=dsn))
