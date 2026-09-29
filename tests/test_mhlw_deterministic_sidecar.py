"""Deterministic MHLW sidecar inputs and fail-closed validation."""
import csv
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from mhlw_dry_run.finalize_mhlw_sidecar import (
    DECISIONS_PATH,
    DeterministicBuildError,
    build_sidecar,
    load_and_validate_decisions,
    validate_source_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
FINAL = ROOT / "mhlw_dry_run" / "clinic_mhlw_departments_final.sqlite3"
DB = ROOT / "data" / "clinics.sqlite3"


def read_decisions():
    with DECISIONS_PATH.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path, fieldnames, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def validate(rows):
    return load_and_validate_decisions(
        DECISIONS_PATH,
        {row["clinic_id"] for row in rows},
        {row["mhlw_facility_id"] for row in rows},
    )


def test_decision_integrity_is_exactly_55_promotions():
    rows = read_decisions()
    assert len(validate(rows)) == 168


def test_unknown_clinic_id_fails_closed():
    rows = read_decisions()
    with pytest.raises(DeterministicBuildError, match="unknown clinic_id"):
        load_and_validate_decisions(
            DECISIONS_PATH,
            {row["clinic_id"] for row in rows} - {rows[0]["clinic_id"]},
            {row["mhlw_facility_id"] for row in rows},
        )


def test_unknown_mhlw_facility_id_fails_closed():
    rows = read_decisions()
    with pytest.raises(DeterministicBuildError, match="unknown mhlw_facility_id"):
        load_and_validate_decisions(
            DECISIONS_PATH,
            {row["clinic_id"] for row in rows},
            {row["mhlw_facility_id"] for row in rows} - {rows[0]["mhlw_facility_id"]},
        )


def test_duplicate_decision_id_fails_closed(tmp_path):
    rows = read_decisions()
    path = tmp_path / "decisions.csv"
    write_csv(path, rows[0].keys(), rows + [rows[0]])
    with pytest.raises(DeterministicBuildError, match="duplicate clinic_id"):
        load_and_validate_decisions(
            path,
            {row["clinic_id"] for row in rows},
            {row["mhlw_facility_id"] for row in rows},
        )


def test_source_hash_mismatch_fails_closed(tmp_path):
    source = tmp_path / "source.csv"
    source.write_bytes(b"wrong")
    db = tmp_path / "clinics.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE clinics(id INTEGER PRIMARY KEY)")
    manifest = {
        "files": [{"filename": source.name, "size": 5, "sha256": "0" * 64}],
        "clinic_master": {
            "path": db.name, "count": 0, "size": db.stat().st_size,
            "sha256": hashlib.sha256(db.read_bytes()).hexdigest(),
        },
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(DeterministicBuildError, match="MHLW source fingerprint mismatch"):
        validate_source_manifest(manifest_path, tmp_path, db)


def test_build_needs_no_audit_csv_and_makes_no_http_requests(tmp_path, monkeypatch):
    rows = read_decisions()
    clinic_db = tmp_path / "clinics.sqlite3"
    with sqlite3.connect(clinic_db) as conn:
        conn.execute("CREATE TABLE clinics(id INTEGER PRIMARY KEY)")
        conn.executemany("INSERT INTO clinics(id) VALUES(?)", [(int(row["clinic_id"]),) for row in rows])

    joins = []
    departments = []
    for row in rows:
        joins.append({
            "clinic_id": row["clinic_id"], "mhlw_facility_id": row["mhlw_facility_id"],
            "match_method": "UNIQUE_ADDRESS_MATCH", "match_confidence": "MEDIUM",
            "join_status": "MATCHED", "review_reason": "",
        })
        departments.append({
            "mhlw_facility_id": row["mhlw_facility_id"], "department_code": "01",
            "department_name": "内科", "source_date": "2026-06-01",
        })
    joins_path, departments_path = tmp_path / "joins.csv", tmp_path / "departments.csv"
    mapping_path, output_path = tmp_path / "mapping.csv", tmp_path / "sidecar.sqlite3"
    write_csv(joins_path, joins[0].keys(), joins)
    write_csv(departments_path, departments[0].keys(), departments)
    write_csv(mapping_path, ["department_code", "department_name", "status", "crestix_department"], [{
        "department_code": "01", "department_name": "内科", "status": "EXCLUDE", "crestix_department": "",
    }])

    def deny_network(*args, **kwargs):
        raise AssertionError("HTTP/network access is forbidden during final sidecar build")

    monkeypatch.setattr("socket.create_connection", deny_network)
    summary = build_sidecar(
        joins_path, departments_path, mapping_path, DECISIONS_PATH, clinic_db, output_path
    )
    assert summary["promotion_allowed"] == 55
    assert summary["final_matched"] == 55
    with sqlite3.connect(output_path) as conn:
        assert conn.execute("SELECT count(DISTINCT clinic_id) FROM clinic_mhlw_departments_final").fetchone()[0] == 55


@pytest.mark.skipif(not (FINAL.exists() and DB.exists()), reason="audited local sidecar/Production DB unavailable")
def test_rebuilt_logical_sidecar_matches_audited_final(tmp_path):
    rebuilt = tmp_path / "rebuilt.sqlite3"
    build_sidecar(
        ROOT / "mhlw_dry_run" / "phase4_join_v2.csv",
        ROOT / "mhlw_dry_run" / "mhlw_department_master.csv",
        ROOT / "mhlw_dry_run" / "mhlw_to_crestix_department_mapping.csv",
        DECISIONS_PATH, DB, rebuilt,
    )
    columns = (
        "clinic_id,mhlw_facility_id,mhlw_department_code,mhlw_department_name,"
        "crestix_department,match_method,identity_status"
    )
    query = f"SELECT {columns} FROM clinic_mhlw_departments_final ORDER BY {columns}"
    with sqlite3.connect(FINAL) as old, sqlite3.connect(rebuilt) as new:
        assert old.execute(query).fetchall() == new.execute(query).fetchall()
