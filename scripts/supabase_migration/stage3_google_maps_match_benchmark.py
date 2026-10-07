"""Read-only benchmark for the batched Google Maps clinic matcher.

Builds representative input rows from canonical Production clinic IDs and times only the
candidate prefetch plus in-memory resolution. It never calls import_maps_results or executes
business DML. Run with SUPABASE_RUNTIME_DB_URL set.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.repository.supabase_write_adapter import SupabaseProvenanceWriteRepository


class TimedCursor:
    def __init__(self, cursor):
        self.cursor = cursor
        self.execute_count = 0
        self.execute_seconds = 0.0
        self.fetch_seconds = 0.0
        self.query_groups = {}

    def execute(self, query, params=None):
        sql = " ".join(str(query).split())
        if "base_json->>'medical_institution_number'=ANY" in sql:
            group = "legacy_official_identifier"
        elif "base_json->>'clinic_id'=ANY" in sql:
            group = "canonical_clinic_id"
        elif "tel_match_key=ANY" in sql:
            group = "phone_candidates"
        elif "input_pairs" in sql:
            group = "name_address_candidates"
        elif "effective_json FROM public.clinics" in sql:
            group = "duplicate_phone_details"
        else:
            group = "other"
        start = time.perf_counter()
        self.cursor.execute(query, params)
        elapsed = time.perf_counter() - start
        self.execute_count += 1
        self.execute_seconds += elapsed
        self.query_groups[group] = self.query_groups.get(group, 0) + 1

    def fetchall(self):
        start = time.perf_counter()
        rows = self.cursor.fetchall()
        self.fetch_seconds += time.perf_counter() - start
        return rows


def run_size(conn, size):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT base_json->>'clinic_id' FROM public.clinics "
            "WHERE merged_into IS NULL AND COALESCE(base_json->>'clinic_id','')<>'' "
            "ORDER BY id LIMIT %s", (size,),
        )
        identifiers = [row[0] for row in cur.fetchall()]
    if len(identifiers) < size:
        raise RuntimeError(f"requested {size} samples, received {len(identifiers)}")
    records = [{"medical_institution_number": value} for value in identifiers]
    timed = TimedCursor(conn.cursor())
    start = time.perf_counter()
    indexes = SupabaseProvenanceWriteRepository._prefetch_maps_targets(timed, records)
    identity_total = time.perf_counter() - start
    start = time.perf_counter()
    results = [SupabaseProvenanceWriteRepository._find_target_prefetched(row, indexes) for row in records]
    resolve_total = time.perf_counter() - start
    matched = sum(1 for result in results if result[0] is not None)
    return {
        "rows": size,
        "total_elapsed_seconds": identity_total + resolve_total,
        "matched": matched,
        "unmatched": sum(1 for result in results if result[0] is None and result[1] == "NOT_FOUND"),
        "ambiguous": sum(1 for result in results if result[1] == "AMBIGUOUS"),
        "query_count": timed.execute_count,
        "query_groups": timed.query_groups,
        "execute_seconds": timed.execute_seconds,
        "fetch_seconds": timed.fetch_seconds,
        "identity_seconds": identity_total,
        "in_memory_matching_seconds": resolve_total,
        "rows_per_second": size / max(identity_total + resolve_total, 1e-9),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", nargs="+", type=int, default=[100, 1000, 10000])
    args = parser.parse_args()
    url = os.environ.get("SUPABASE_RUNTIME_DB_URL")
    if not url:
        raise RuntimeError("SUPABASE_RUNTIME_DB_URL is not set")
    print("benchmark_start read_only=YES credential_value=REDACTED", flush=True)
    with psycopg.connect(url, connect_timeout=20) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_user")
            role = cur.fetchone()[0]
        if role != "clinic_runtime":
            raise RuntimeError("benchmark requires clinic_runtime")
        print(f"runtime_role={role} DML=0")
        for size in args.sizes:
            print(f"phase=benchmark_size rows={size}", flush=True)
            print(run_size(conn, size))


if __name__ == "__main__":
    main()
