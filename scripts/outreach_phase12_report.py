"""Phase 1-2 audit and preview generation for Gmail outreach (no Gmail/DB writes).

This command deliberately stops at CSV/JSON artifacts.  It opens the Email database in
transaction-read-only mode, uses the existing Clinic Master read-only role, reads the Mynavi
CSV without modifying it, and never calls a Gmail API.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.audit_outreach_historical_choices import audit as audit_history  # noqa: E402
from scripts.launch_v2 import load_runtime_env  # noqa: E402
from scripts.validate_recommender_against_history import validate as validate_history  # noqa: E402
from src.outreach.article_recommender import read_master  # noqa: E402
from src.outreach.generation import (  # noqa: E402
    EXPECTED_GMAIL_ACCOUNT, FAILED, NEEDS_REVIEW, READY, REQUIRED_STAFF, GenerationSettings, generate_batch, summarize,
    to_csv_bytes,
)
from src.outreach.secure_artifacts import (  # noqa: E402
    ensure_private_directory, write_private_bytes, write_private_text,
)
from src.outreach.targets import DIRECTOR_OWNER_UNVERIFIED, select_targets  # noqa: E402
from src.outreach_ui import _load_links  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def raw_row_count(path: Path) -> int:
    with path.open(encoding="utf-8-sig", newline="") as source:
        return sum(1 for _ in csv.DictReader(source))


def _email_snapshot() -> tuple[dict, list[dict]]:
    """Read the Email sidecar in a transaction-read-only session."""
    from src.repository.email_database import build_email_repositories

    bundle, status = build_email_repositories()
    if bundle is None or not status.ready:
        raise RuntimeError(status.message)
    connection = bundle.email_research._conn
    try:
        # connect() has already issued a formatting-only SET, so end that transaction first.
        connection.rollback()
        with connection.cursor() as cursor:
            cursor.execute("SET default_transaction_read_only = on")
        connection.commit()
        summary = bundle.email_research.summary()
        rows = bundle.email_research.primary_listing(limit=1_000_000)
        return summary, rows
    finally:
        connection.close()


def _clinic_snapshot(ids: list[int]) -> tuple[list[dict], dict]:
    # The actively provisioned target-reader role is restricted to SELECT on exactly
    # public.clinics and hp_research.clinic_hp_research and verifies its own role/grants.
    from src.repository.email_target_reader import connect_email_target_reader
    from src.repository.supabase_adapter import SupabaseClinicRepository, SupabaseHpResearchRepository

    target_reader = connect_email_target_reader()
    connection = target_reader._conn
    repository = SupabaseClinicRepository(connection, SupabaseHpResearchRepository(connection))
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, active, merged_into, merge_hold, COALESCE(exclude_reason,''), uuid
                FROM public.clinics WHERE id = ANY(%s::bigint[])
                """,
                (ids,),
            )
            eligibility = {
                int(row[0]): {
                    "active": bool(row[1]), "merged_into": row[2], "merge_hold": bool(row[3]),
                    "exclude_reason": str(row[4] or ""), "uuid": str(row[5] or "").strip(),
                }
                for row in cursor.fetchall()
            }
        safe_ids = [
            clinic_id for clinic_id in ids
            if clinic_id in eligibility
            and eligibility[clinic_id]["active"]
            and eligibility[clinic_id]["merged_into"] is None
            and not eligibility[clinic_id]["merge_hold"]
            and not eligibility[clinic_id]["exclude_reason"].strip()
            and eligibility[clinic_id]["uuid"]
        ]
        return repository._batch_get(safe_ids), eligibility
    finally:
        connection.close()


def run(args) -> dict:
    load_runtime_env(ROOT)
    master = Path(args.articles or os.environ.get("OUTREACH_ARTICLE_MASTER_CSV", ""))
    join = Path(args.join or os.environ.get("OUTREACH_ARTICLE_JOIN_CSV", "")) if (
        args.join or os.environ.get("OUTREACH_ARTICLE_JOIN_CSV")) else None
    reviews = Path(args.reviews) if args.reviews else None
    if not master.is_file():
        raise RuntimeError("マイナビ記事マスターが見つかりません")
    if join is not None and not join.is_file():
        raise RuntimeError("記事JOIN CSVが見つかりません")

    before = sha256(master)
    articles = read_master(master)
    links = _load_links(join) if join else []
    email_summary, primary_rows = _email_snapshot()
    email_by_id = {
        int(row["clinic_id"]): {
            "email": str(row.get("email") or "").strip(),
            "candidate_id": str(row.get("candidate_id") or "").strip(),
        }
        for row in primary_rows
        if row.get("final_sales_eligible") is True and not (row.get("hard_block_codes") or [])
    }
    ids = list(email_by_id)
    records, eligibility = _clinic_snapshot(ids)
    selection = select_targets(
        records, email_lookup=lambda requested: {i: email_by_id[i] for i in requested if i in email_by_id},
        links=links, email_filter="取得済み", limit=max(1, len(records)),
    )
    settings = GenerationSettings(
        staff=REQUIRED_STAFF,
        # No calendar connection or administrator-confirmed slots are available in this
        # read-only audit. Do not infer that generic business hours are actually free.
        dates=(),
        called_uuids=frozenset(),
        calendar_confirmed=False,
        provisional_template_approved=False,
        as_of=args.as_of,
    )
    results = generate_batch(selection.targets, articles, settings)
    counts = summarize(results)
    eligible_ids = {int(record["id"]) for record in records}
    clinic_missing = sum(clinic_id not in eligibility for clinic_id in ids)
    excluded = len(ids) - clinic_missing - len(eligible_ids)
    duplicate_pairs = sum(
        count - 1 for count in Counter(
            (row["clinic_id"], str(row.get("email") or "").strip().casefold()) for row in primary_rows
        ).values() if count > 1
    )

    ensure_private_directory(args.out)
    write_private_bytes(args.out / "phase2_previews.csv", to_csv_bytes(
        results, settings.staff, settings.generation_batch_id,
        calendar_confirmed=settings.calendar_confirmed,
    ))
    history = audit_history(reviews, master) if reviews and reviews.is_file() else None
    current_history = validate_history(reviews, master) if reviews and reviews.is_file() else None
    after = sha256(master)
    article_states = Counter(result.target.article.status for result in results)
    director_confirmed = sum(
        bool(result.target.director_name and result.target.director_name_confirmed)
        for result in results
    )
    director_owner_unverified = sum(
        result.target.director_name_source == DIRECTOR_OWNER_UNVERIFIED for result in results
    )
    director_missing = len(results) - director_confirmed - director_owner_unverified
    phone_history_unconfirmed = sum(not result.called for result in results)
    calendar_pending = len(results) if not settings.calendar_confirmed else 0
    # Every classified row is a preview/result record, including FAILED rows whose body is
    # intentionally blank. Keep the rendered-body count separate so the 227-row cohort is not
    # confused with the 222 rows for which safe provisional text could be rendered.
    preview_generated = len(results)
    preview_bodies_generated = sum(bool(result.body) for result in results)
    article_matched = sum(bool(result.recommendations) for result in results)
    article_missing = len(results) - article_matched
    article_region_review = sum("第一推奨記事が地域不一致" in result.reasons for result in results)
    article_ready_match = article_matched - article_region_review
    content_ready = sum(result.status == READY for result in results)
    gmail_ready = sum(result.gmail_ready for result in results)
    general_addressee_approved = sum(result.general_addressee_approved for result in results)
    canary_candidates = [
        result for result in results
        if result.body
        and result.status == NEEDS_REVIEW
        and set(result.reasons) == {"候補日時が未設定"}
        and not result.general_addressee_approved
    ][:5]
    write_private_bytes(args.out / "canary_candidates.csv", to_csv_bytes(
        canary_candidates, settings.staff, settings.generation_batch_id,
        calendar_confirmed=settings.calendar_confirmed,
    ))
    report = {
        "generated_at_date": args.as_of.isoformat(),
        "phase": "1-2",
        "gmail": {
            "expected_account": EXPECTED_GMAIL_ACCOUNT,
            "oauth_implemented": False,
            "authenticated_account": None,
            "draft_writes": 0,
            "emails_sent": 0,
            "note": "Phase 3未実施。Gmail API呼び出しなし。",
        },
        "email": {
            "summary": email_summary,
            "primary_rows": len(primary_rows),
            "sales_eligible": len(email_by_id),
            "duplicate_clinic_email_pairs": duplicate_pairs,
        },
        "clinic_master": {
            "matched_active_rows": len(records),
            "missing_rows": clinic_missing,
            "excluded_or_inactive_rows": excluded,
            "uuid_missing": sum(not item.get("uuid") for item in eligibility.values()),
        },
        "article_master": {
            "file": master.name,
            "rows_source": raw_row_count(master),
            "rows_valid": len(articles),
            "rows_rejected_or_duplicate": raw_row_count(master) - len(articles),
            "sha256_before": before,
            "sha256_after": after,
            "unchanged": before == after,
            "join_path": str(join) if join else None,
            "join_rows": len(links),
        },
        "listing_status": {
            "LISTED_VERIFIED": article_states.get("LISTED", 0),
            "UNLISTED_VERIFIED": article_states.get("NOT_LISTED", 0),
            "UNCONFIRMED": article_states.get("UNCONFIRMED", 0),
        },
        "generation": {
            "staff": settings.staff,
            "candidate_dates": [item.isoformat() for item in settings.dates],
            "calendar_confirmed": settings.calendar_confirmed,
            "phone_history_confirmed": 0,
            "phone_history_unconfirmed": phone_history_unconfirmed,
            "provisional_template_approved": settings.provisional_template_approved,
            "preview_generated": preview_generated,
            "preview_bodies_generated": preview_bodies_generated,
            "article_matched": article_matched,
            "article_missing": article_missing,
            "article_ready_match": article_ready_match,
            "article_region_review": article_region_review,
            "director_name_confirmed": director_confirmed,
            "director_owner_unverified": director_owner_unverified,
            "director_name_missing": director_missing,
            "general_addressee_approved": general_addressee_approved,
            "article_information_unconfirmed": article_states.get("UNCONFIRMED", 0),
            "calendar_pending": calendar_pending,
            "calendar_ready": len(results) - calendar_pending,
            "content_ready": content_ready,
            "gmail_ready": gmail_ready,
            "canary_candidates": len(canary_candidates),
            **counts,
        },
        "historical_25": history,
        "current_recommender_history_validation": current_history,
        "safety": {
            "gmail_writes": 0, "email_sends": 0, "production_writes": 0,
            "production_migrations": 0, "hp_auto_actions": 0, "email_auto_actions": 0,
        },
        "overall": "NO-GO",
        "blockers": [
            "記事JOIN CSVが未設定のため掲載状況は全件UNCONFIRMED（中立的な関連記事型でのみ生成）",
            "電話済み・メール送付依頼履歴が未連携（電話を示唆しない中立文へ分岐）",
            "Googleカレンダーまたは管理者入力による候補日時が未確定",
            "関連記事紹介型テンプレートのGmail保存前最終確認が未完了",
            "Gmail OAuth/アカウント一致確認はPhase 3未実施",
        ] + (["開設者名はあるが院長本人との同一性が未確認"] if director_owner_unverified else []),
    }
    write_private_text(
        args.out / "phase12_report.json",
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
    )
    print(json.dumps({
        "Email Candidates": email_summary.get("email_obtained"),
        "Sales Eligible": len(email_by_id),
        "Article Master Source/Valid": [raw_row_count(master), len(articles)],
        "LISTED_VERIFIED": report["listing_status"]["LISTED_VERIFIED"],
        "UNLISTED_VERIFIED": report["listing_status"]["UNLISTED_VERIFIED"],
        "UNCONFIRMED": report["listing_status"]["UNCONFIRMED"],
        "READY": counts[READY], "NEEDS_REVIEW": counts[NEEDS_REVIEW], "FAILED": counts[FAILED],
        "Preview Generated": preview_generated,
        "Article Matched": article_matched,
        "Article Ready Match": article_ready_match,
        "Article Region Review": article_region_review,
        "Article Missing": article_missing,
        "Content READY": content_ready,
        "Gmail READY": gmail_ready,
        "Canary Candidates": len(canary_candidates),
        "Director Confirmed": director_confirmed,
        "Owner-derived Held": director_owner_unverified,
        "Director Missing": director_missing,
        "Phone History Unconfirmed": phone_history_unconfirmed,
        "Calendar Pending": calendar_pending,
        "Emails Sent": 0, "Overall": "NO-GO", "out": str(args.out),
    }, ensure_ascii=False, indent=2))
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--articles")
    parser.add_argument("--join")
    parser.add_argument("--reviews")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
