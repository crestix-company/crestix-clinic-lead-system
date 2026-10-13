#!/usr/bin/env python3
"""Export mutually-exclusive dental sales CSVs for effective HP rank A+B.

This is the reusable version of the one-off dental export used in October 2026.
It performs SELECT-only access to Production and writes CSVs under exports/.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path

from scripts.launch_v2 import load_runtime_env
from src.master.comdesk import COMDESK_EXPORT_HEADERS
from src.repository.runtime_store import SupabaseRuntimeStore
from src.repository.supabase_adapter import connect

CATEGORY_FILES = {
    "01": "01_マウスピース_インビザライン.csv",
    "02": "02_矯正歯科全般.csv",
    "03": "03_All-on-4.csv",
    "04": "04_インプラント全般.csv",
    "05": "05_セラミック治療.csv",
    "06": "06_審美歯科全般.csv",
    "07a": "07a_口腔外科.csv",
    "07b": "07b_審美系.csv",
    "08": "08_入れ歯_親知らず_根管治療.csv",
    "09": "09_一般歯科.csv",
}

CATEGORY_LABELS = {
    "01": "① マウスピース矯正 / インビザライン",
    "02": "② 矯正歯科全般",
    "03": "③ All-on-4",
    "04": "④ インプラント全般",
    "05": "⑤ セラミック治療",
    "06": "⑥ 審美歯科全般",
    "07a": "⑦ 口腔外科",
    "07b": "⑦ 審美系（ホワイトニング・ジルコニア・ラミネートベニア）",
    "08": "⑧ 入れ歯・義歯（自費）・親知らず・根管治療",
    "09": "⑨ 一般歯科・クリーニング・定期検診・虫歯治療",
}


def category_for(codes: set[str]) -> str:
    if "ALIGNER_INVISALIGN" in codes:
        return "01"
    if "ORTHODONTICS_GENERAL" in codes:
        return "02"
    if "ALL_ON_4" in codes:
        return "03"
    if "IMPLANT_GENERAL" in codes:
        return "04"
    if "CERAMIC" in codes:
        return "05"
    aesthetic_specific = {"WHITENING", "ZIRCONIA", "LAMINATE_VENEER"}
    if "AESTHETIC_GENERAL" in codes and not (codes & aesthetic_specific):
        return "06"
    if "ORAL_SURGERY" in codes:
        return "07a"
    if codes & aesthetic_specific:
        return "07b"
    if codes & {"PRIVATE_DENTURE", "WISDOM_TOOTH", "ROOT_CANAL"}:
        return "08"
    return "09"


def category_evidence(codes: set[str], category: str) -> str:
    labels = {
        "ALIGNER_INVISALIGN": "マウスピース矯正 / インビザライン",
        "ORTHODONTICS_GENERAL": "矯正歯科全般",
        "ALL_ON_4": "All-on-4",
        "IMPLANT_GENERAL": "インプラント全般",
        "CERAMIC": "セラミック治療",
        "AESTHETIC_GENERAL": "審美歯科全般",
        "ORAL_SURGERY": "口腔外科",
        "WHITENING": "ホワイトニング",
        "ZIRCONIA": "ジルコニア",
        "LAMINATE_VENEER": "ラミネートベニア",
        "PRIVATE_DENTURE": "入れ歯・義歯（自費）",
        "WISDOM_TOOTH": "親知らず",
        "ROOT_CANAL": "根管治療",
        "GENERAL_DENTISTRY": "一般歯科",
        "CLEANING": "クリーニング",
        "CHECKUP": "定期検診",
        "CARIES": "虫歯治療",
    }
    allowed = {
        "01": ("ALIGNER_INVISALIGN",),
        "02": ("ORTHODONTICS_GENERAL",),
        "03": ("ALL_ON_4",),
        "04": ("IMPLANT_GENERAL",),
        "05": ("CERAMIC",),
        "06": ("AESTHETIC_GENERAL",),
        "07a": ("ORAL_SURGERY",),
        "07b": ("WHITENING", "ZIRCONIA", "LAMINATE_VENEER"),
        "08": ("PRIVATE_DENTURE", "WISDOM_TOOTH", "ROOT_CANAL"),
        "09": ("CLEANING", "CHECKUP", "CARIES"),
    }[category]
    out = [labels[code] for code in allowed if code in codes]
    if category == "09" and not out:
        out = ["一般歯科"]
    return " / ".join(out)


def _query_rows(conn):
    sql = r"""
    WITH latest_review AS (
      SELECT DISTINCT ON (clinic_id,tag_code)
        clinic_id,tag_code,human_decision
      FROM provenance.dental_sales_tag_reviews
      ORDER BY clinic_id,tag_code,reviewed_at DESC,id DESC
    ),
    effective AS (
      SELECT
        t.clinic_id,t.tag_code,t.tag_label,t.priority_group,t.sort_order,
        t.confidence,t.source,t.rule_id
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
        confidence,source,rule_id
      FROM effective
      ORDER BY clinic_id,priority_group,sort_order,tag_code
    ),
    all_tags AS (
      SELECT
        clinic_id,
        string_agg(tag_label,' / ' ORDER BY priority_group,sort_order,tag_code) AS labels,
        string_agg(tag_code,' / ' ORDER BY priority_group,sort_order,tag_code) AS codes
      FROM effective
      GROUP BY clinic_id
    )
    SELECT
      c.id,c.uuid,c.clinic_name,c.phone,c.address,c.prefecture,
      COALESCE(NULLIF(c.hp_url,''),NULLIF(c.maps_website_url,''),'') AS hp_url,
      CASE
        WHEN (CASE WHEN h.fetch_status='OK' AND COALESCE(h.machine_hp_rank,'')<>''
                   THEN UPPER(BTRIM(h.machine_hp_rank)) ELSE 'UNKNOWN' END) IN ('A','B','C','D')
          THEN (CASE WHEN h.fetch_status='OK' AND COALESCE(h.machine_hp_rank,'')<>''
                     THEN UPPER(BTRIM(h.machine_hp_rank)) ELSE 'UNKNOWN' END)
        WHEN (CASE WHEN h.fetch_status='OK' AND COALESCE(h.machine_hp_rank,'')<>''
                   THEN UPPER(BTRIM(h.machine_hp_rank)) ELSE 'UNKNOWN' END)='UNKNOWN'
             AND UPPER(BTRIM(COALESCE(c.hp_rank,''))) IN ('A','B')
          THEN UPPER(BTRIM(COALESCE(c.hp_rank,'')))
        ELSE 'D'
      END AS effective_hp_rank,
      p.priority_group,p.tag_code,p.tag_label,p.confidence,p.source,p.rule_id,
      a.labels,a.codes
    FROM public.clinics c
    LEFT JOIN hp_research.clinic_hp_research h ON h.clinic_id=c.id
    JOIN primary_tag p ON p.clinic_id=c.id
    JOIN all_tags a ON a.clinic_id=c.id
    WHERE c.medical_type='歯科'
      AND c.merged_into IS NULL
      AND c.merge_hold=false
      AND c.active=true
      AND NOT (
        c.exclude_reason IN ('hospital','center')
        OR COALESCE(substring(c.effective_json from '"facility_type":"([^"]*)"'),'')='病院'
        OR c.clinic_name LIKE '%病院%'
        OR c.clinic_name LIKE '%センター%'
      )
      AND (
        CASE
          WHEN (CASE WHEN h.fetch_status='OK' AND COALESCE(h.machine_hp_rank,'')<>''
                     THEN UPPER(BTRIM(h.machine_hp_rank)) ELSE 'UNKNOWN' END) IN ('A','B','C','D')
            THEN (CASE WHEN h.fetch_status='OK' AND COALESCE(h.machine_hp_rank,'')<>''
                       THEN UPPER(BTRIM(h.machine_hp_rank)) ELSE 'UNKNOWN' END)
          WHEN (CASE WHEN h.fetch_status='OK' AND COALESCE(h.machine_hp_rank,'')<>''
                     THEN UPPER(BTRIM(h.machine_hp_rank)) ELSE 'UNKNOWN' END)='UNKNOWN'
               AND UPPER(BTRIM(COALESCE(c.hp_rank,''))) IN ('A','B')
            THEN UPPER(BTRIM(COALESCE(c.hp_rank,'')))
          ELSE 'D'
        END
      ) IN ('A','B')
    ORDER BY c.id
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        columns = [d.name for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def main():
    root = Path(__file__).resolve().parents[1]
    load_runtime_env(root)

    db_url = os.environ.get("SUPABASE_RUNTIME_DB_URL")
    if not db_url:
        raise RuntimeError("SUPABASE_RUNTIME_DB_URL is not set")

    conn = connect(db_url, autocommit=True)
    with conn.cursor() as cur:
        cur.execute("SET default_transaction_read_only = on")

    try:
        records = _query_rows(conn)
    finally:
        conn.close()

    grouped = {key: [] for key in CATEGORY_FILES}
    for row in records:
        codes = {x.strip() for x in str(row["codes"] or "").split("/") if x.strip()}
        cat = category_for(codes)
        row["_category"] = cat
        row["_category_evidence"] = category_evidence(codes, cat)
        grouped[cat].append(row)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = root / "exports" / f"dental_categories_{stamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Formal Comdesk output: preserve the existing 28-column source row when a
    # clinic already has Comdesk provenance, then append HP rank + official verified email.
    # Category information stays in the filename so no non-Comdesk columns are introduced.
    store = SupabaseRuntimeStore()
    by_id = {int(row["id"]): row for row in records}

    summary_rows = []
    for key, filename in CATEGORY_FILES.items():
        path = output_dir / filename
        items = grouped[key]
        summary_rows.append((CATEGORY_LABELS[key], len(items)))

        ids = [int(row["id"]) for row in items]
        master_records = store.repositories.clinics._batch_get(ids) if ids else []
        master_by_id = {int(row["id"]): row for row in master_records}
        ordered_records = [master_by_id[clinic_id] for clinic_id in ids if clinic_id in master_by_id]

        rows28 = store._export_records(ordered_records)
        rows30 = store._attach_export_metadata(ordered_records, rows28)

        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(COMDESK_EXPORT_HEADERS)
            writer.writerows(rows30)

    summary_path = output_dir / "00_カテゴリ件数.csv"
    with summary_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["カテゴリ", "件数"])
        for label, count in summary_rows:
            writer.writerow([label, count])
        writer.writerow(["合計", sum(count for _, count in summary_rows)])

    print("\n=== 歯科カテゴリ別CSV出力完了（HP ABC=A+B） ===")
    print(f"出力先: {output_dir}\n")
    for label, count in summary_rows:
        print(f"{label}: {count:,}件")
    print(f"合計: {sum(count for _, count in summary_rows):,}件\n")


if __name__ == "__main__":
    main()
