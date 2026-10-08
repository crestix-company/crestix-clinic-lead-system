#!/usr/bin/env python3
"""Backfill dental sales tags from existing clinic metadata and saved official HP pages.

Default is DRY RUN. Use --apply only after the additive dental_sales_tags schema is present.
This script never updates public.clinics, research.*, treatment.*, UUIDs, or HP results.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
import os
from pathlib import Path

from src.master.dental_sales_tags import classify_dental_sales_tags, primary_dental_sales_tag
from src.repository.dental_sales_tag_repository import SupabaseDentalSalesTagRepository
from src.repository.supabase_adapter import connect


def _json(value):
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return []
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return []


def _load_clinics(conn, *, limit=None):
    sql = """
        SELECT id,clinic_name,medical_type,departments_json
        FROM public.clinics
        WHERE medical_type='歯科'
          AND merged_into IS NULL
          AND merge_hold=false
          AND active=true
        ORDER BY id
    """
    params = ()
    if limit:
        sql += " LIMIT %s"
        params = (int(limit),)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [
            {
                "id": int(row[0]),
                "clinic_name": row[1] or "",
                "medical_type": row[2] or "",
                "departments_json": _json(row[3]),
            }
            for row in cur.fetchall()
        ]


def _load_pages(conn, clinic_ids):
    if not clinic_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT clinic_id,url,page_json
            FROM research.hp_pages
            WHERE clinic_id=ANY(%s::bigint[])
            ORDER BY clinic_id,url
            """,
            (list(clinic_ids),),
        )
        out = {}
        for clinic_id, url, page_json in cur.fetchall():
            out.setdefault(int(clinic_id), []).append({
                "url": url or "",
                "page_json": page_json or "",
            })
        return out


def _preview_rows(classifications, names):
    rows = []
    for clinic_id, tags in classifications:
        primary = primary_dental_sales_tag(tags)
        rows.append({
            "clinic_id": clinic_id,
            "clinic_name": names.get(clinic_id, ""),
            "priority_group": primary.priority_group if primary else "",
            "primary_tag_code": primary.tag_code if primary else "",
            "primary_tag": primary.tag_label if primary else "",
            "all_tags": " / ".join(item.tag_label for item in tags if item.auto_status == "CONFIRMED"),
            "review_tags": " / ".join(item.tag_label for item in tags if item.auto_status == "REVIEW"),
        })
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="persist derived tags to provenance.dental_sales_tags")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--batch-size", type=int, default=500)
    ap.add_argument("--preview-csv", type=Path, default=None)
    args = ap.parse_args(argv)

    url = os.environ.get("SUPABASE_RUNTIME_DB_URL")
    if not url:
        raise RuntimeError("SUPABASE_RUNTIME_DB_URL is not set")

    conn = connect(url, autocommit=False)
    repo = SupabaseDentalSalesTagRepository(conn)
    if args.apply and not repo.available():
        raise RuntimeError(
            "dental_sales_tags schema is not available. Apply "
            "scripts/supabase_migration/dental_sales_tags_schema.sql first."
        )

    clinics = _load_clinics(conn, limit=args.limit)
    totals = Counter()
    preview = []
    applied_clinics = applied_tags = 0

    for start in range(0, len(clinics), max(1, args.batch_size)):
        chunk = clinics[start:start + max(1, args.batch_size)]
        ids = [row["id"] for row in chunk]
        pages = _load_pages(conn, ids)
        classifications = []
        names = {}
        for row in chunk:
            tags = classify_dental_sales_tags(row, pages.get(row["id"], ()))
            classifications.append((row["id"], tags))
            names[row["id"]] = row["clinic_name"]
            primary = primary_dental_sales_tag(tags)
            totals[f"priority_{primary.priority_group if primary else 'none'}"] += 1
            for tag in tags:
                if tag.auto_status == "CONFIRMED":
                    totals[f"tag:{tag.tag_code}"] += 1
                else:
                    totals[f"review:{tag.tag_code}"] += 1
        preview.extend(_preview_rows(classifications, names))
        if args.apply:
            result = repo.replace_auto_tags_bulk(classifications)
            applied_clinics += result["clinics"]
            applied_tags += result["tags"]

    if args.preview_csv:
        args.preview_csv.parent.mkdir(parents=True, exist_ok=True)
        with args.preview_csv.open("w", encoding="utf-8-sig", newline="") as f:
            fields = [
                "clinic_id","clinic_name","priority_group","primary_tag_code",
                "primary_tag","all_tags","review_tags",
            ]
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(preview)

    result = {
        "mode": "APPLY" if args.apply else "DRY_RUN",
        "dental_clinics": len(clinics),
        "applied_clinics": applied_clinics,
        "applied_tags": applied_tags,
        "counts": dict(sorted(totals.items())),
        "preview_csv": str(args.preview_csv) if args.preview_csv else "",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
