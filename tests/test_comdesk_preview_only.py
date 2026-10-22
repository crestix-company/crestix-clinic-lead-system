import hashlib
import json
import sqlite3

import pandas as pd
import pytest

from src.io.input_loader import load_table
from src.master.comdesk import COMDESK_EXPORT_HEADERS, COMDESK_HEADERS
from src.master.comdesk_sync import apply_sync, plan_sync
from src.master.store import ClinicStore, dumps, now
from src.repository.sqlite_adapter import SqliteClinicRepository
from src.repository.sqlite_write_adapter import SqliteClinicWriteRepository
from src.safety.preview_mode import PreviewOnlyModeError


def _csv_bytes(rows):
    lines = [COMDESK_EXPORT_HEADERS]
    lines.extend([[str(row.get(header, "")) for header in COMDESK_EXPORT_HEADERS] for row in rows])
    return ("\n".join(",".join(value.replace(",", " ") for value in line) for line in lines) + "\n").encode()


def _seed_clinic(store, *, internal_uuid, comdesk_uuid, hp_rank="B", maps_url="", maps_status=""):
    effective = {
        "clinic_name": "Preview検証医院", "phone": "03-1111-2222", "address": "東京都千代田区1-1",
        "hp_url": "https://old.example.jp", "hp_rank": hp_rank, "hp_status": "VERIFIED",
    }
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO clinics(uuid,clinic_name,phone,address,hp_url,hp_rank,hp_status,maps_website_url,"
            "maps_presence_status,effective_json,base_json,first_seen_at,last_seen_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (internal_uuid, effective["clinic_name"], effective["phone"], effective["address"],
             effective["hp_url"], hp_rank, "VERIFIED", maps_url, maps_status,
             dumps(effective), dumps(effective), now(), now()),
        )
        clinic_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        original = [""] * len(COMDESK_HEADERS)
        original[COMDESK_HEADERS.index("UUID")] = comdesk_uuid
        original[COMDESK_HEADERS.index("URL")] = effective["hp_url"]
        original[COMDESK_HEADERS.index("診療日")] = "月火"
        conn.execute(
            "INSERT OR IGNORE INTO templates(id,headers_json,mapping_json,created_at) VALUES(?,?,?,?)",
            ("preview-template", dumps(COMDESK_HEADERS), "{}", now()),
        )
        conn.execute(
            "INSERT INTO comdesk_original_rows(clinic_id,template_id,row_json,uuid,source_hash,row_number,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (clinic_id, "preview-template", dumps(original), comdesk_uuid,
             f"preview-{comdesk_uuid}", clinic_id, now()),
        )
        return clinic_id


def _readonly_snapshot(path):
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return tuple(conn.execute(
            "SELECT id,uuid,hp_rank,hp_url,effective_json,base_json FROM clinics ORDER BY id"
        )) + tuple(conn.execute("SELECT id,source,result_json FROM import_batches ORDER BY id"))
    finally:
        conn.close()


def test_sqlite_preview_only_reads_30_columns_matches_holds_and_preserves_database(tmp_path, monkeypatch):
    monkeypatch.setenv("CLINIC_COMDESK_PREVIEW_ONLY", "1")
    path = tmp_path / "synthetic-preview.sqlite"
    store = ClinicStore(path)
    _seed_clinic(store, internal_uuid="INTERNAL-1", comdesk_uuid="COMDESK-1")
    _seed_clinic(
        store, internal_uuid="INTERNAL-2", comdesk_uuid="COMDESK-2",
        maps_url="https://official.example.jp", maps_status="MAPS_MATCHED_WEBSITE",
    )
    repo = SqliteClinicRepository(store)
    rows = [
        {"UUID": "COMDESK-1", "HPランク": "C", "メールアドレス": "candidate@example.jp"},
        {"UUID": "COMDESK-2", "URL": "https://other.example.jp"},
        {"UUID": "MISSING-UUID", "HPランク": "A"},
    ]
    table = load_table(_csv_bytes(rows), "synthetic_comdesk.csv")
    assert len(table.headers) == 30
    before = _readonly_snapshot(path)
    before_hash = hashlib.sha256(path.read_bytes()).hexdigest()

    plan = plan_sync(table, repo)

    assert plan["counts"] == {"AUTO_UPDATE": 1, "NO_CHANGE": 0, "HOLD": 2}
    assert plan["rows"][0]["changes"]["hp_rank"]["after"] == "C"
    assert plan["rows"][0]["changes"]["email"]["after"] == "candidate@example.jp"
    assert plan["rows"][1]["status"] == "HOLD"
    assert "hp_url" in plan["rows"][1]["reason"]
    assert plan["rows"][2]["status"] == "HOLD"
    assert _readonly_snapshot(path) == before
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash


def test_preview_only_streamlit_page_shows_only_preview_and_never_apply(tmp_path, monkeypatch):
    import app_v2

    monkeypatch.setenv("CLINIC_COMDESK_PREVIEW_ONLY", "1")
    path = tmp_path / "ui-preview.sqlite"
    store = ClinicStore(path)
    _seed_clinic(store, internal_uuid="INTERNAL-1", comdesk_uuid="COMDESK-1")
    data = _csv_bytes([{"UUID": "COMDESK-1", "HPランク": "C", "メールアドレス": "candidate@example.jp"}])
    calls = {"buttons": [], "frames": []}

    class Uploaded:
        name = "synthetic.csv"

        def getvalue(self):
            return data

    class FakeStreamlit:
        session_state = {}

        def set_page_config(self, **_kwargs): pass
        def title(self, value): calls.setdefault("titles", []).append(value)
        def caption(self, value): calls.setdefault("captions", []).append(value)
        def header(self, value): calls.setdefault("headers", []).append(value)
        def warning(self, value): calls.setdefault("warnings", []).append(value)
        def info(self, value): calls.setdefault("info", []).append(value)
        def error(self, value): calls.setdefault("errors", []).append(value)
        def file_uploader(self, *_args, **_kwargs): return Uploaded()
        def button(self, label, **kwargs):
            calls["buttons"].append((label, kwargs.get("key")))
            return True
        def write(self, value): calls.setdefault("writes", []).append(value)
        def dataframe(self, value, **_kwargs): calls["frames"].append(value)

    fake_st = FakeStreamlit()
    monkeypatch.setattr(app_v2, "st", fake_st)
    repo = SqliteClinicRepository(store)
    monkeypatch.setattr(app_v2, "comdesk_preview_repository_for", lambda _key: repo)
    monkeypatch.setattr(app_v2, "write_repositories_for", lambda *_args, **_kwargs: pytest.fail("WRITE path reached"))

    app_v2.main()

    assert calls["headers"] == ["Comdesk Step7 — Preview専用"]
    assert len(calls["frames"]) == 1
    rendered = calls["frames"][0].to_dict("records")
    assert any(row["項目"] == "HPランク" and row["変更後"] == "C" for row in rendered)
    assert any(row["項目"] == "メール候補" and row["変更後"] == "candidate@example.jp" for row in rendered)
    assert [key for _, key in calls["buttons"]] == ["comdesk_preview_only_run"]
    assert not any("Job" in title or "Email" in title for title in calls.get("titles", []))


def test_preview_only_guards_apply_write_and_automation_entrypoints(monkeypatch):
    from src.cloud_run.email_executor import create_email_run, dispatch_email_worker
    from src.cloud_run.hp_executor import dispatch_hp_job
    from src.master.email_auto_run import EmailAutoRunner
    from src.master.hp_auto_run import AutoHpRunner
    from src.repository.write_backend import build_write_repositories, write_repositories_for

    monkeypatch.setenv("CLINIC_COMDESK_PREVIEW_ONLY", "1")
    dummy_plan = {"file_hash": "test", "rows": []}
    with pytest.raises(PreviewOnlyModeError):
        apply_sync(object(), dummy_plan)
    with pytest.raises(PreviewOnlyModeError):
        SqliteClinicWriteRepository.__new__(SqliteClinicWriteRepository).apply_comdesk_sync([], "test")
    with pytest.raises(PreviewOnlyModeError):
        build_write_repositories(backend="sqlite")
    with pytest.raises(PreviewOnlyModeError):
        write_repositories_for(object())
    with pytest.raises(PreviewOnlyModeError):
        AutoHpRunner().start(object())
    with pytest.raises(PreviewOnlyModeError):
        EmailAutoRunner.__new__(EmailAutoRunner).start()
    with pytest.raises(PreviewOnlyModeError):
        dispatch_hp_job("job-id")
    with pytest.raises(PreviewOnlyModeError):
        create_email_run()
    with pytest.raises(PreviewOnlyModeError):
        dispatch_email_worker("run-id")


def test_preview_only_blocks_gmail_api_connection_before_oauth_or_network(monkeypatch):
    from src.outreach.gmail_oauth import _google_dependencies
    from src.outreach.gmail_drafts import list_existing_drafts, list_existing_messages

    monkeypatch.setenv("CLINIC_COMDESK_PREVIEW_ONLY", "1")
    with pytest.raises(PreviewOnlyModeError):
        _google_dependencies()
    with pytest.raises(PreviewOnlyModeError):
        list_existing_drafts(object())
    with pytest.raises(PreviewOnlyModeError):
        list_existing_messages(object(), query="")
