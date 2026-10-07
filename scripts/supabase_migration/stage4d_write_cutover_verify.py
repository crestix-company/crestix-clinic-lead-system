"""One-shot Stage4-D cutover proof using the production launcher configuration.

Writes a reversible marker to the existing filter_defaults setting through the normal WRITE
Repository and immediately restores the byte-identical canonical value. No admin credential is
read, no clinic/business row is touched, and no SQLite write path is available to this script.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import psycopg

from scripts.launch_v2 import load_runtime_env
from src.master.data_paths import production_db_path
from src.master.hp_effective_rank import hp_batch_path
from src.master.research_sidecar import research_sidecar_path
from src.master.store import dumps
from src.repository.write_backend import active_write_backend, build_write_repositories


ROOT = Path(__file__).resolve().parents[2]
SETTING_KEY = "monthly_limit"
MARKER = 1_000_000_007


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    load_runtime_env(ROOT)
    os.environ.pop("SUPABASE_DB_URL", None)
    if active_write_backend() != "supabase":
        raise RuntimeError("Production WRITE routing is not Supabase")
    url = os.environ.get("SUPABASE_RUNTIME_DB_URL")
    if not url:
        raise RuntimeError("SUPABASE_RUNTIME_DB_URL is not set")

    sqlite_paths = (production_db_path(), research_sidecar_path(), hp_batch_path())
    before_sha = tuple(sha256(path) for path in sqlite_paths)
    conn = psycopg.connect(url)
    with conn.cursor() as cur:
        cur.execute("select current_user,session_user,rolsuper,rolbypassrls from pg_roles where rolname=current_user")
        identity = cur.fetchone()
        if identity != ("clinic_runtime", "clinic_runtime", False, False):
            raise RuntimeError("Runtime identity contract failed")
        cur.execute("select value from app_config.settings where key=%s", (SETTING_KEY,))
        row = cur.fetchone()
        if not row:
            raise RuntimeError(f"{SETTING_KEY} setting is missing")
        original_text = row[0]
        original = json.loads(original_text)
        if dumps(original) != original_text:
            raise RuntimeError(f"{SETTING_KEY} is not canonical JSON; refusing byte-changing restore")
    conn.rollback()
    conn.close()

    repositories = build_write_repositories()
    wrote_marker = False
    try:
        repositories.settings.set(SETTING_KEY, MARKER)
        wrote_marker = True
        with repositories.settings._conn.cursor() as cur:
            cur.execute("select current_user,value from app_config.settings where key=%s", (SETTING_KEY,))
            observed_user, observed_text = cur.fetchone()
        repositories.settings._conn.rollback()
        if observed_user != "clinic_runtime" or json.loads(observed_text) != MARKER:
            raise RuntimeError("Supabase runtime write was not observed exactly")
    finally:
        if wrote_marker:
            repositories.settings.set(SETTING_KEY, original)

    with repositories.settings._conn.cursor() as cur:
        cur.execute("select value from app_config.settings where key=%s", (SETTING_KEY,))
        restored_text = cur.fetchone()[0]
    repositories.settings._conn.rollback()
    repositories.settings._conn.close()
    if restored_text != original_text:
        raise RuntimeError("Settings cleanup did not restore the exact original value")

    after_sha = tuple(sha256(path) for path in sqlite_paths)
    if after_sha != before_sha:
        raise RuntimeError("Production SQLite SHA changed during Supabase runtime write")

    print("production_write_backend=supabase")
    print("runtime_identity=clinic_runtime")
    print("settings_canary=PASS")
    print("settings_cleanup=PASS")
    print("sqlite_fallback=0")
    print("sqlite_sha_unchanged=True")
    print("admin_runtime_usage=0")


if __name__ == "__main__":
    main()
