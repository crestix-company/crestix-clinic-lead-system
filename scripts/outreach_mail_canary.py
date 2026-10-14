"""「メール送付文章」実データ50医院Canary（READ ONLY）。

- Clinic Master: CLINIC_READONLY_DB_URL のREAD ONLY専用ロールでSELECTのみ（権限を事前検証、不可なら停止）
- 記事原本: --articles または OUTREACH_ARTICLE_MASTER_CSV を読み取りのみ（実行前後のSHA-256で無変更を確認）
- 記事JOIN CSV: 任意。無い場合は全医院「未確認」としてNEEDS_REVIEWで生成
- Email: --with-email 指定時のみEmail調査のPrimary EmailをSELECT
DB書き込み・Migration・HP/Email調査ジョブ操作・Gmail操作は行いません。
出力は --out ディレクトリの canary_results.csv（全件の文章・記事選定結果）と canary_report.json のみ。

  python scripts/outreach_mail_canary.py --staff 前田 --start 2026-10-19 --prefecture 東京都 \
      --out artifacts/outreach_canary_50
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.outreach.article_adapter import ArticleLink  # noqa: E402
from src.outreach.article_recommender import read_master  # noqa: E402
from src.outreach.canary import audit  # noqa: E402
from src.outreach.email_template import five_weekdays  # noqa: E402
from src.outreach.generation import GenerationSettings, generate_batch, to_csv_bytes  # noqa: E402
from src.outreach.readonly_access import ReadOnlyAccessError, open_readonly_clinic_repository  # noqa: E402
from src.outreach.staff import load_staff, validate_staff_name  # noqa: E402
from src.outreach.targets import select_targets  # noqa: E402

ARTICLE_MASTER_ENV = "OUTREACH_ARTICLE_MASTER_CSV"
EXPECTED_ARTICLE_ROWS = 3901
EXIT_PERMISSION = 2
EXIT_INPUT = 3


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:  # read only
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_links(path):
    if not path:
        return []
    with Path(path).open(encoding="utf-8-sig", newline="") as fp:
        return [ArticleLink(r["clinic_uuid"].strip(), r["listing_status"].strip(), r["example_article_url"].strip(),
                            r["listing_url"].strip(), r["listing_kind"].strip(), r["source_row"].strip(),
                            r["article_id"].strip(), r["verified"].strip().lower() == "true")
                for r in csv.DictReader(fp)]


def read_emails(ids):
    from src.repository.email_database import build_email_repositories
    bundle, status = build_email_repositories()
    if bundle is None:
        raise ReadOnlyAccessError(f"Email調査DBに接続できません: {status.message}")
    wanted = set(ids)
    return {int(r["clinic_id"]): str(r.get("email") or "").strip()
            for r in bundle.email_research.primary_listing(limit=1_000_000)
            if int(r["clinic_id"]) in wanted and r.get("final_sales_eligible") is not False}


def stop(code, message):
    print(f"[STOP] {message}", file=sys.stderr)
    return code


def main(argv=None, *, connect_fn=None, email_reader=read_emails):
    p = argparse.ArgumentParser()
    p.add_argument("--articles", help=f"記事原本CSV（省略時は {ARTICLE_MASTER_ENV}）")
    p.add_argument("--join", help="記事JOIN CSV（任意）")
    p.add_argument("--staff", required=True)
    p.add_argument("--start", type=date.fromisoformat, required=True)
    p.add_argument("--prefecture", action="append", default=[])
    p.add_argument("--municipality", action="append", default=[])
    p.add_argument("--department", action="append", default=[])
    p.add_argument("--rank", action="append", default=[])
    p.add_argument("--called-uuid", action="append", default=[])
    p.add_argument("--with-email", action="store_true")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--scan", type=int, default=300)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)

    # --- preflight: inputs (no DB access yet) ---
    master = Path(args.articles or os.environ.get(ARTICLE_MASTER_ENV, "").strip() or "-")
    if not master.is_file():
        return stop(EXIT_INPUT, f"記事原本CSVが見つかりません。--articles か {ARTICLE_MASTER_ENV} で指定してください: {master}")
    staff_error = validate_staff_name(args.staff)
    staff_config = load_staff()
    if staff_error:
        return stop(EXIT_INPUT, f"IS担当者名が不正です: {staff_error}")
    if staff_config.names and args.staff.strip() not in staff_config.names:
        return stop(EXIT_INPUT, f"IS担当者 {args.staff!r} が {staff_config.path.name} に登録されていません")
    master_hash_before = sha256(master)
    articles = read_master(master)

    # --- preflight: READ ONLY permission (stop without workaround if missing) ---
    try:
        repository, conn = open_readonly_clinic_repository(connect_fn=connect_fn)
    except ReadOnlyAccessError as exc:
        return stop(EXIT_PERMISSION, str(exc))

    try:
        from src.master.filters import Filters
        from src.master.scope import SCOPE_ALL
        records = repository.query(Filters(
            active_only=True, hp_only=False, prefectures=args.prefecture, municipalities=args.municipality,
            departments=args.department, effective_ranks=args.rank, scope=SCOPE_ALL), args.scan)
    finally:
        conn.close()

    emails: dict[int, str] = {}
    email_note = "未使用（--with-email 未指定のため全件 宛先未取得→NEEDS_REVIEW）"
    if args.with_email:
        try:
            emails = email_reader([int(r.get("id") or 0) for r in records])
            email_note = "Email調査 Primary Email（SELECTのみ）"
        except ReadOnlyAccessError as exc:
            return stop(EXIT_PERMISSION, str(exc))

    links = load_links(args.join)
    selection = select_targets(records, email_lookup=lambda ids: emails, links=links, limit=args.limit)
    settings = GenerationSettings(staff=args.staff.strip(), dates=five_weekdays(args.start),
                                  called_uuids=frozenset(args.called_uuid))
    results = generate_batch(selection.targets, articles, settings)
    report = audit(results, master_urls={a.url for a in articles},
                   source_uuids={int(r.get("id") or 0): str(r.get("uuid") or "").strip() for r in records},
                   emails=emails, settings=settings)
    master_hash_after = sha256(master)
    report.update(
        article_master={"file": master.name, "rows_valid": len(articles), "expected_rows": EXPECTED_ARTICLE_ROWS,
                        "sha256_before": master_hash_before, "sha256_after": master_hash_after,
                        "unchanged": master_hash_before == master_hash_after},
        join_rows=len(links), email_source=email_note, scanned=selection.scanned,
        skipped_no_uuid=selection.skipped_no_uuid, skipped_duplicate=selection.skipped_duplicate,
        filters={"prefecture": args.prefecture, "municipality": args.municipality,
                 "department": args.department, "rank": args.rank},
    )
    if master_hash_before != master_hash_after:
        report["passed"] = False
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "canary_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.out / "canary_results.csv").write_bytes(
        to_csv_bytes(results, settings.staff, settings.generation_batch_id))
    print(json.dumps({k: v for k, v in report.items() if k != "issues"}, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
