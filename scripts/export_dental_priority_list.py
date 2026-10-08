#!/usr/bin/env python3
"""Export dental clinics ordered by effective dental sales priority.

Requires provenance.dental_sales_tags. Human REJECTED overrides an auto tag.
The output is clinic-level (one row per clinic), sorted from priority 1 to 9.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path

from src.repository.supabase_adapter import connect


def _rows(conn, priorities):
    priority_filter = ""
    params = []
    if priorities:
        priority_filter = " AND effective.priority_group=ANY(%s::smallint[])"
        params.append([int(x) for x in priorities])
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH latest_review AS (
              SELECT DISTINCT ON (clinic_id,tag_code)
                clinic_id,tag_code,human_decision,reviewed_at,id
              FROM provenance.dental_sales_tag_reviews
              ORDER BY clinic_id,tag_code,reviewed_at DESC,id DESC
            ),
            effective AS (
              SELECT
                t.clinic_id,t.tag_code,t.tag_label,t.priority_group,t.sort_order,
                t.confidence,t.source,t.rule_id,
                COALESCE(r.human_decision,'') AS human_decision
              FROM provenance.dental_sales_tags t
              LEFT JOIN latest_review r
                ON r.clinic_id=t.clinic_id AND r.tag_code=t.tag_code
              WHERE t.active=true
                AND (
                  (t.auto_status='CONFIRMED' AND COALESCE(r.human_decision,'')<>'REJECTED')
                  OR r.human_decision='CONFIRMED'
                )
            ),
            primary_tag AS (
              SELECT DISTINCT ON (clinic_id)
                clinic_id,tag_code,tag_label,priority_group,sort_order,
                confidence,source,rule_id,human_decision
              FROM effective
              ORDER BY clinic_id,priority_group,sort_order,tag_code
            ),
            all_tags AS (
              SELECT clinic_id,
                     string_agg(tag_label,' / ' ORDER BY priority_group,sort_order,tag_code) AS labels,
                     string_agg(tag_code,' / ' ORDER BY priority_group,sort_order,tag_code) AS codes
              FROM effective
              GROUP BY clinic_id
            )
            SELECT
              c.id,c.uuid,c.clinic_name,c.phone,c.address,c.prefecture,
              COALESCE(NULLIF(c.hp_url,''),NULLIF(c.maps_website_url,''),'') AS hp_url,
              p.priority_group,p.tag_code,p.tag_label,p.confidence,p.source,p.rule_id,
              a.labels,a.codes
            FROM public.clinics c
            JOIN primary_tag p ON p.clinic_id=c.id
            JOIN all_tags a ON a.clinic_id=c.id
            JOIN effective ON effective.clinic_id=c.id AND effective.tag_code=p.tag_code
            WHERE c.medical_type='歯科'
              AND c.merged_into IS NULL
              AND c.merge_hold=false
              AND c.active=true
            """ + priority_filter + """
            ORDER BY p.priority_group,p.sort_order,c.id
            """,
            tuple(params),
        )
        return cur.fetchall()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--priority", action="append", type=int, choices=range(1, 10), default=[])
    ap.add_argument("--output", type=Path, default=Path("artifacts/dental_sales_tags/dental_sales_priority.csv"))
    ap.add_argument("--preview", action="store_true", help="show counts only; write no CSV")
    args = ap.parse_args(argv)

    url = os.environ.get("SUPABASE_RUNTIME_DB_URL")
    if not url:
        raise RuntimeError("SUPABASE_RUNTIME_DB_URL is not set")
    conn = connect(url, autocommit=True)

    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('provenance.dental_sales_tags')")
        if cur.fetchone()[0] is None:
            raise RuntimeError("dental_sales_tags schema is not available")

    rows = _rows(conn, args.priority)
    counts = {}
    for row in rows:
        counts[str(row[7])] = counts.get(str(row[7]), 0) + 1

    summary = {
        "rows": len(rows),
        "priority_counts": counts,
        "priorities": args.priority or list(range(1, 10)),
        "output": "" if args.preview else str(args.output),
    }
    if args.preview:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return

    args.output.parent.mkdir(parents=True, exist_ok=True)
    headers = [
        "営業優先順位","管理番号","UUID","医院名","電話番号","住所","都道府県","HP URL",
        "最優先タグコード","最優先タグ","確度","判定ソース","判定ルール",
        "全歯科営業タグ","全タグコード",
    ]
    with args.output.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for row in rows:
            writer.writerow([
                row[7],row[0],row[1] or "",row[2] or "",row[3] or "",row[4] or "",row[5] or "",
                row[6] or "",row[8],row[9],row[10],row[11],row[12],row[13],row[14],
            ])
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
