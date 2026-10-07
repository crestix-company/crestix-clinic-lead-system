#!/usr/bin/env python3
"""Exact one-time cleanup for the Stage4-D persistent canary.

This is intentionally an admin/migration operation, not a runtime Repository capability.
Every target is fixed by exact PK and protected by a full-row canonical SHA-256 precondition.
The transaction aborts on any content, dependency, count, or sequence mismatch.
"""
import hashlib
import json
import os

import psycopg
from psycopg.rows import dict_row


TOKEN = "__stage4d_persistent_canary_20261007_codex_01"
CLINIC_ID = 1_000_000_000
SOURCE_RECORD_ID = 1_000_000_000
JOB_ID = "0c0d496248c54c99ade63145ab114626"

EXPECTED_COUNTS_BEFORE = {"public.clinics": 162259, "total_19": 632915}
EXPECTED_COUNTS_AFTER = {"public.clinics": 162258, "total_19": 632903}
EXPECTED_SEQUENCES = {
    ("public", "clinics_id_seq"): 1_000_000_000,
    ("provenance", "source_records_id_seq"): 1_000_000_000,
}

# Queries include every column. Hashes were captured from the read-only pre-cleanup manifest.
MANIFEST = {
    "clinic": {
        "query": "SELECT * FROM public.clinics WHERE id=%s AND uuid=%s ORDER BY id",
        "params": (CLINIC_ID, TOKEN), "rows": 1,
        "sha256": "67611c1ef0fdb8e9688d71b38f2a0ee209d1681551a537ca23b02182046e5569",
    },
    "source_record": {
        "query": "SELECT * FROM provenance.source_records WHERE id=%s ORDER BY id",
        "params": (SOURCE_RECORD_ID,), "rows": 1,
        "sha256": "50e2d39df0ae32e57c47bc4695497761e136fa902fe055be9103aa9f7389df2a",
    },
    "history": {
        "query": "SELECT * FROM provenance.change_history WHERE clinic_id=%s ORDER BY id",
        "params": (CLINIC_ID,), "rows": 3,
        "sha256": "816dc82d581b655496dacb0dfabd8d722a13227527fb6da660e0e199320fad67",
    },
    "job": {
        "query": "SELECT * FROM research.research_jobs WHERE id=%s ORDER BY id",
        "params": (JOB_ID,), "rows": 1,
        "sha256": "6759fd296feb3d424be9c0be804093126dd7acc2bd473d5f0a0d1f739955f783",
    },
    "job_items": {
        "query": "SELECT * FROM research.research_job_items WHERE job_id=%s ORDER BY job_id,clinic_id",
        "params": (JOB_ID,), "rows": 1,
        "sha256": "48aa23c3df3f0d7659e0ea37bbf221037ed5a19dc565190222216ed8634ce768",
    },
    "research_result": {
        "query": "SELECT * FROM research.research_results WHERE clinic_id=%s ORDER BY clinic_id",
        "params": (CLINIC_ID,), "rows": 1,
        "sha256": "5f5e5a484a6e3c4c71290b2d709f0b4e316b4b1b8a1f7f012e271bba5e6824fa",
    },
    "hp_pages": {
        "query": "SELECT * FROM research.hp_pages WHERE clinic_id=%s ORDER BY clinic_id,url",
        "params": (CLINIC_ID,), "rows": 1,
        "sha256": "4738d918442dbc41dafc285a1ae9d47d27dcbe99410de8bcd9f5d66fe03f5782",
    },
    "treatment_status": {
        "query": "SELECT * FROM treatment.clinic_research_status WHERE clinic_id=%s ORDER BY clinic_id",
        "params": (CLINIC_ID,), "rows": 1,
        "sha256": "7f86e5b8931e6770751286547a93f567112e652f93722e2b853112e63b7a03c8",
    },
    "treatment_rows": {
        "query": "SELECT * FROM treatment.clinic_treatment_research WHERE clinic_id=%s ORDER BY clinic_id,treatment_category_name",
        "params": (CLINIC_ID,), "rows": 1,
        "sha256": "d89a40af93c5399418c4e15f9591d9159139ae8373ec5eb0772f7f06251e5bb3",
    },
    "hp_result": {
        "query": "SELECT * FROM hp_research.clinic_hp_research WHERE clinic_id=%s ORDER BY clinic_id",
        "params": (CLINIC_ID,), "rows": 1,
        "sha256": "76fec9fb58cbf98534ff6fb2dbe81992f987f7828fccac7534cd3208f0bfd7dc",
    },
}

TABLES_19 = (
    "public.clinics", "provenance.templates", "provenance.source_records",
    "provenance.import_batches", "provenance.change_history",
    "provenance.comdesk_original_rows", "provenance.google_maps_results",
    "provenance.match_reviews", "provenance.manual_overrides", "research.research_jobs",
    "research.research_job_items", "research.research_results", "research.hp_pages",
    "research.search_usage", "research.search_cache", "app_config.settings",
    "treatment.clinic_research_status", "treatment.clinic_treatment_research",
    "hp_research.clinic_hp_research",
)


def canonical_hash(rows):
    raw = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def counts(cur):
    values = {}
    for table in TABLES_19:
        cur.execute("SELECT count(*) AS n FROM " + table)
        values[table] = cur.fetchone()["n"]
    return values


def sequences(cur):
    cur.execute(
        "SELECT schemaname,sequencename,last_value FROM pg_sequences WHERE "
        "(schemaname,sequencename) IN "
        "(('public','clinics_id_seq'),('provenance','source_records_id_seq'))"
    )
    return {(row["schemaname"], row["sequencename"]): row["last_value"] for row in cur.fetchall()}


def assert_no_unmanifested_dependencies(cur):
    checks = {
        "provenance.comdesk_original_rows": ("clinic_id=%s", (CLINIC_ID,)),
        "provenance.google_maps_results": ("clinic_id=%s", (CLINIC_ID,)),
        "provenance.manual_overrides": ("clinic_id=%s", (CLINIC_ID,)),
        "provenance.match_reviews_clinic": ("resolved_clinic_id=%s", (CLINIC_ID,)),
        "provenance.match_reviews_source": ("source_record_id=%s", (SOURCE_RECORD_ID,)),
        "public.clinic_email_enrichment": ("clinic_id=%s", (CLINIC_ID,)),
        "public.new_clinic_candidates": ("existing_clinic_id=%s", (CLINIC_ID,)),
        "public.clinics_merged_into": ("merged_into=%s", (CLINIC_ID,)),
        "research.search_usage": ("job_id=%s", (JOB_ID,)),
    }
    for label, (predicate, params) in checks.items():
        table = label.rsplit("_", 1)[0] if label.startswith("provenance.match_reviews_") else label
        if label == "public.clinics_merged_into":
            table = "public.clinics"
        cur.execute(f"SELECT count(*) AS n FROM {table} WHERE {predicate}", params)
        actual = cur.fetchone()["n"]
        if actual != 0:
            raise RuntimeError(f"unmanifested dependency {label}: {actual}")


def main():
    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        raise SystemExit("SUPABASE_DB_URL is not set")
    report = {"token": TOKEN, "manifest": {}, "deleted": {}}
    with psycopg.connect(url, connect_timeout=20, row_factory=dict_row) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                before = counts(cur)
                if before["public.clinics"] != EXPECTED_COUNTS_BEFORE["public.clinics"]:
                    raise RuntimeError(f"clinics precondition mismatch: {before['public.clinics']}")
                if sum(before.values()) != EXPECTED_COUNTS_BEFORE["total_19"]:
                    raise RuntimeError(f"19-table total precondition mismatch: {sum(before.values())}")
                if sequences(cur) != EXPECTED_SEQUENCES:
                    raise RuntimeError(f"sequence precondition mismatch: {sequences(cur)}")

                for name, item in MANIFEST.items():
                    cur.execute(item["query"] + " FOR UPDATE", item["params"])
                    rows = cur.fetchall()
                    digest = canonical_hash(rows)
                    report["manifest"][name] = {"rows": len(rows), "sha256": digest}
                    if len(rows) != item["rows"] or digest != item["sha256"]:
                        raise RuntimeError(
                            f"manifest mismatch for {name}: rows={len(rows)} sha256={digest}"
                        )
                if sum(item["rows"] for item in MANIFEST.values()) != 12:
                    raise RuntimeError("internal manifest does not total 12 rows")
                assert_no_unmanifested_dependencies(cur)

                deletes = (
                    ("research.research_job_items", "job_id=%s AND clinic_id=%s", (JOB_ID, CLINIC_ID), 1),
                    ("research.research_jobs", "id=%s", (JOB_ID,), 1),
                    ("research.hp_pages", "clinic_id=%s AND url=%s", (CLINIC_ID, f"https://example.invalid/{TOKEN}"), 1),
                    ("research.research_results", "clinic_id=%s", (CLINIC_ID,), 1),
                    ("treatment.clinic_treatment_research", "clinic_id=%s AND treatment_category_name=%s", (CLINIC_ID, TOKEN), 1),
                    ("treatment.clinic_research_status", "clinic_id=%s", (CLINIC_ID,), 1),
                    ("hp_research.clinic_hp_research", "clinic_id=%s", (CLINIC_ID,), 1),
                    ("provenance.change_history", "id=ANY(%s) AND clinic_id=%s", ([179108, 179109, 179110], CLINIC_ID), 3),
                    ("provenance.source_records", "id=%s AND clinic_id=%s AND source_hash=%s", (SOURCE_RECORD_ID, CLINIC_ID, TOKEN), 1),
                    ("public.clinics", "id=%s AND uuid=%s", (CLINIC_ID, TOKEN), 1),
                )
                for table, predicate, params, expected in deletes:
                    cur.execute(f"DELETE FROM {table} WHERE {predicate}", params)
                    report["deleted"][table] = cur.rowcount
                    if cur.rowcount != expected:
                        raise RuntimeError(f"delete count mismatch for {table}: {cur.rowcount}")

                after = counts(cur)
                if after["public.clinics"] != EXPECTED_COUNTS_AFTER["public.clinics"]:
                    raise RuntimeError(f"clinics postcondition mismatch: {after['public.clinics']}")
                if sum(after.values()) != EXPECTED_COUNTS_AFTER["total_19"]:
                    raise RuntimeError(f"19-table total postcondition mismatch: {sum(after.values())}")
                if sequences(cur) != EXPECTED_SEQUENCES:
                    raise RuntimeError(f"sequence changed during cleanup: {sequences(cur)}")
                report["counts_before"] = {
                    "public.clinics": before["public.clinics"], "total_19": sum(before.values())
                }
                report["counts_after"] = {
                    "public.clinics": after["public.clinics"], "total_19": sum(after.values())
                }
                report["sequences_after"] = {
                    f"{schema}.{name}": value for (schema, name), value in sequences(cur).items()
                }
    report["deleted_total"] = sum(report["deleted"].values())
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
