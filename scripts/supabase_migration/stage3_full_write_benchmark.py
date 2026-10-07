"""Isolated full-path benchmark for the batched Step3 Google Maps writer.

This deliberately uses no database connection. A psycopg-shaped in-memory executor supplies
synthetic clinic states and records SQL statements, so no Production DML, sequence consumption,
locks, provenance rows, or batch markers are possible.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.master.google_maps import MAPS_RESULT_HEADERS
from src.repository.supabase_write_adapter import SupabaseProvenanceWriteRepository


class SyntheticCursor:
    def __init__(self, clinics):
        self.clinics = clinics
        self.result = []
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        sql = " ".join(str(query).split())
        self.statements.append(sql)
        if "FROM provenance.import_batches" in sql and sql.startswith("SELECT"):
            self.result = []
        elif "SELECT id,base_json->>'medical_institution_number'" in sql:
            self.result = [
                (cid, None, f"official-{cid}", None, None, None)
                for cid in self.clinics
            ]
        elif "SELECT id,base_json,uuid,medical_key FROM public.clinics" in sql:
            self.result = [
                (cid, state["base"], state["uuid"], state["medical_key"])
                for cid, state in self.clinics.items()
            ]
        elif "FROM research.research_results" in sql or "FROM provenance.manual_overrides" in sql:
            self.result = []
        else:
            self.result = []

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return self.result


class SyntheticConnection:
    def __init__(self, clinics):
        self.cur = SyntheticCursor(clinics)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def run_size(size):
    first_id = 1_000_000_000
    clinics = {
        first_id + offset: {
            "base": {
                "clinic_id": f"official-{first_id + offset}",
                "clinic_name": f"Synthetic Clinic {offset}",
                "address": "東京都千代田区",
                "status": "現存",
                "facility_type": "診療所",
            },
            "uuid": f"synthetic-uuid-{offset}",
            "medical_key": "",
        }
        for offset in range(size)
    }
    rows = []
    for offset, cid in enumerate(clinics):
        row = {header: "" for header in MAPS_RESULT_HEADERS}
        row.update({
            "internal_clinic_id": str(cid),
            "maps_match_status": "MAPS_MATCHED_WEBSITE",
            "maps_website_url": f"https://synthetic-{offset}.example/",
            "maps_profile_url": f"https://maps.example/{offset}",
            "website_status": "WEBSITE",
            "scraped_at": "2026-01-01T00:00:00+00:00",
        })
        rows.append(row)
    connection = SyntheticConnection(clinics)
    repository = SupabaseProvenanceWriteRepository(connection)
    started = time.perf_counter()
    counts = repository.import_maps_results(pd.DataFrame(rows))
    elapsed = time.perf_counter() - started
    metrics = repository._last_import_metrics
    assert connection.commits == 1 and connection.rollbacks == 0
    assert counts["MATCHED"] == size
    assert not any("FOR UPDATE" in sql and "id=%s" in sql for sql in connection.cur.statements)
    return {
        "rows": size,
        "elapsed_seconds": round(elapsed, 3),
        "rows_per_second": round(size / max(elapsed, 1e-9), 1),
        "sql_executions": metrics["sql_execute_count"],
        "history_rows_simulated": size,
        "projection_rows": size,
        "phases": {key: round(value, 3) for key, value in metrics.items()
                   if key.endswith("_seconds")},
        "production_connection": False,
        "persistent_mutations": 0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", nargs="+", type=int, default=[100, 1000, 10000])
    args = parser.parse_args()
    for size in args.sizes:
        print(run_size(size), flush=True)


if __name__ == "__main__":
    main()
