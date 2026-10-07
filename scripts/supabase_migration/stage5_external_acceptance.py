#!/usr/bin/env python3
"""Stage5 Windows and real multi-PC acceptance helper.

No secret is printed or stored.  The only persistent test mutation is a reversible numeric
marker in the existing ``monthly_limit`` setting, written through the production Repository and
restored byte-for-byte by the originating machine.  No clinic/business row is touched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.launch_v2 import load_runtime_env
from src.master.store import dumps
from src.repository.backend import build_repositories
from src.repository.write_backend import active_write_backend, build_write_repositories


SETTING_KEY = "monthly_limit"
TOKEN_RE = re.compile(r"^__stage5_(?:windows|multipc)_acceptance_[A-Za-z0-9-]{8,80}$")
TABLES = (
    ("public", "clinics"), ("provenance", "source_records"),
    ("provenance", "change_history"), ("provenance", "templates"),
    ("provenance", "comdesk_original_rows"), ("provenance", "google_maps_results"),
    ("provenance", "match_reviews"), ("provenance", "manual_overrides"),
    ("provenance", "import_batches"), ("research", "research_jobs"),
    ("research", "research_job_items"), ("research", "research_results"),
    ("research", "hp_pages"), ("research", "search_usage"),
    ("research", "search_cache"), ("app_config", "settings"),
    ("treatment", "clinic_research_status"),
    ("treatment", "clinic_treatment_research"),
    ("hp_research", "clinic_hp_research"),
)


def _load_production():
    load_runtime_env(ROOT)
    os.environ.pop("SUPABASE_DB_URL", None)
    if os.environ.get("CLINIC_DATA_BACKEND", "").lower() != "supabase":
        raise RuntimeError("READ backend is not Supabase")
    if active_write_backend() != "supabase":
        raise RuntimeError("WRITE backend is not Supabase")
    if not os.environ.get("SUPABASE_RUNTIME_DB_URL"):
        raise RuntimeError("SUPABASE_RUNTIME_DB_URL is not set")


def _validate_token(token, kind):
    if not TOKEN_RE.fullmatch(token) or not token.startswith(f"__stage5_{kind}_acceptance_"):
        raise ValueError("acceptance token format is invalid")


def _marker(token, phase):
    # Valid monthly_limit integer, deterministic across machines, far outside normal values.
    digest = hashlib.sha256(f"{token}:{phase}".encode()).digest()
    return 1_500_000_000 + int.from_bytes(digest[:4], "big") % 500_000_000


def _state_path(token):
    suffix = hashlib.sha256(token.encode()).hexdigest()[:20]
    return Path(tempfile.gettempdir()) / f"clinic-stage5-{suffix}.json"


def _snapshot(read_repositories):
    conn = read_repositories.clinics._conn
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_user,session_user,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole "
            "FROM pg_roles WHERE rolname=current_user"
        )
        identity = cur.fetchone()
        if identity != ("clinic_runtime", "clinic_runtime", False, False, False, False):
            raise RuntimeError("runtime identity contract failed")
        counts = []
        for schema, table in TABLES:
            cur.execute(f'SELECT count(*) FROM "{schema}"."{table}"')
            counts.append(cur.fetchone()[0])
        cur.execute("SELECT value FROM app_config.settings WHERE key=%s", (SETTING_KEY,))
        setting = cur.fetchone()
    if not setting:
        raise RuntimeError(f"{SETTING_KEY} setting is missing")
    original_text = setting[0]
    original_value = json.loads(original_text)
    if dumps(original_value) != original_text:
        raise RuntimeError("setting is not canonical JSON; refusing byte-changing restore")
    return identity, tuple(counts), original_text, original_value


def _read_setting(read_repositories):
    return read_repositories.settings.get(SETTING_KEY)


def _set_setting(value):
    repositories = build_write_repositories()
    try:
        repositories.settings.set(SETTING_KEY, value)
    finally:
        repositories.settings._conn.close()


def _assert_counts(read_repositories, expected):
    current = _snapshot(read_repositories)[1]
    if current != expected:
        raise RuntimeError("unexpected Supabase row-count mutation")


def windows_acceptance(token):
    _validate_token(token, "windows")
    _load_production()
    read = build_repositories()
    identity, counts, original_text, original = _snapshot(read)
    marker = _marker(token, "windows")
    wrote = False
    try:
        _set_setting(marker)
        wrote = True
        if _read_setting(read) != marker:
            raise RuntimeError("Repository write was not visible through the READ Repository")
    finally:
        if wrote:
            _set_setting(original)
    with read.settings._conn.cursor() as cur:
        cur.execute("SELECT value FROM app_config.settings WHERE key=%s", (SETTING_KEY,))
        restored = cur.fetchone()[0]
    if restored != original_text:
        raise RuntimeError("settings cleanup was not byte-identical")
    _assert_counts(read, counts)

    # Execute the actual Streamlit application with every persistent SQLite open forbidden.
    real_sqlite_connect = sqlite3.connect
    sqlite3.connect = lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("persistent SQLite open attempted")
    )
    try:
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_file(str(ROOT / "app_v2.py"), default_timeout=120).run()
        if app.exception or app.error:
            raise RuntimeError("application startup/UI smoke failed")
        app.radio[0].set_value("営業対象・出力")
        app.run(timeout=120)
        if app.exception or app.error:
            raise RuntimeError("application sales UI smoke failed")
        metrics = {item.label: item.value for item in app.metric}
        if metrics.get("営業対象") != "997件":
            raise RuntimeError("UI baseline mismatch")
    finally:
        sqlite3.connect = real_sqlite_connect
        read.clinics._conn.close()

    print("RUNTIME_ENV=SET")
    print("READ_BACKEND=SUPABASE")
    print("WRITE_BACKEND=SUPABASE")
    print("RUNTIME_ROLE=clinic_runtime")
    print("READ=PASS")
    print("WRITE=PASS")
    print("WRITE_CLEANUP=PASS")
    print("UI_SMOKE=PASS")
    print("SQLITE_REQUIRED=NO")
    print("SQLITE_OPEN=0")
    print("SECRET_OUTPUT=0")
    print("UNEXPECTED_MUTATION=0")
    print("WINDOWS_ACCEPTANCE=PASS")


def multipc_write(token, phase, save_original):
    _validate_token(token, "multipc")
    _load_production()
    read = build_repositories()
    identity, counts, original_text, original = _snapshot(read)
    state_path = _state_path(token)
    if save_original:
        if state_path.exists():
            raise RuntimeError("local acceptance state already exists; cleanup or use a new token")
        state_path.write_text(json.dumps({
            "token": token, "original_text": original_text, "original": original,
            "counts": list(counts),
        }, ensure_ascii=False), encoding="utf-8")
    marker = _marker(token, phase)
    _set_setting(marker)
    if _read_setting(read) != marker:
        raise RuntimeError("marker write was not observed")
    read.clinics._conn.close()
    print("RUNTIME_ROLE=clinic_runtime")
    print("WRITE=PASS")
    print("MARKER_PHASE=" + phase)
    print("LOCAL_DB_SYNC=0")


def multipc_read(token, phase):
    _validate_token(token, "multipc")
    _load_production()
    read = build_repositories()
    if _read_setting(read) != _marker(token, phase):
        raise RuntimeError("expected marker is not visible on this PC")
    read.clinics._conn.close()
    print("RUNTIME_ROLE=clinic_runtime")
    print("READ=PASS")
    print("MARKER_PHASE=" + phase)
    print("LOCAL_DB_SYNC=0")


def multipc_cleanup(token):
    _validate_token(token, "multipc")
    _load_production()
    state_path = _state_path(token)
    if not state_path.is_file():
        raise RuntimeError("originating PC acceptance state is missing")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("token") != token:
        raise RuntimeError("local acceptance state token mismatch")
    expected_markers = {_marker(token, "a_to_b"), _marker(token, "b_to_a")}
    read = build_repositories()
    if _read_setting(read) not in expected_markers:
        raise RuntimeError("live setting is not this acceptance marker; refusing cleanup")
    _set_setting(state["original"])
    with read.settings._conn.cursor() as cur:
        cur.execute("SELECT value FROM app_config.settings WHERE key=%s", (SETTING_KEY,))
        restored = cur.fetchone()[0]
    if restored != state["original_text"]:
        raise RuntimeError("cleanup did not restore the exact original value")
    _assert_counts(read, tuple(state["counts"]))
    state_path.unlink()
    read.clinics._conn.close()
    print("CLEANUP=PASS")
    print("REMAINING_ACCEPTANCE_ROWS=0")
    print("UNEXPECTED_MUTATION=0")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    windows = sub.add_parser("windows")
    windows.add_argument("--token", required=True)
    write = sub.add_parser("multipc-write")
    write.add_argument("--token", required=True)
    write.add_argument("--phase", choices=("a_to_b", "b_to_a"), required=True)
    write.add_argument("--save-original", action="store_true")
    read = sub.add_parser("multipc-read")
    read.add_argument("--token", required=True)
    read.add_argument("--phase", choices=("a_to_b", "b_to_a"), required=True)
    cleanup = sub.add_parser("multipc-cleanup")
    cleanup.add_argument("--token", required=True)
    args = parser.parse_args()
    if args.command == "windows":
        windows_acceptance(args.token)
    elif args.command == "multipc-write":
        multipc_write(args.token, args.phase, args.save_original)
    elif args.command == "multipc-read":
        multipc_read(args.token, args.phase)
    else:
        multipc_cleanup(args.token)


if __name__ == "__main__":
    main()
