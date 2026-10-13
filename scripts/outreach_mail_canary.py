"""「メール送付文章」50医院Canary（READ ONLY）。

Clinic Master（Supabase runtime）とEmail調査Primary EmailをSELECTのみで読み、
記事原本CSV（READ ONLY）から文章を生成して監査します。DB書き込み・Gmail操作は行いません。
出力は --out ディレクトリ（ローカル）の canary_report.json と canary_results.csv のみ。

例:
  python scripts/outreach_mail_canary.py --articles 記事原本.csv --join article_join.csv \
      --staff 前田 --start 2026-10-19 --prefecture 東京都 --department 内科 --out artifacts/outreach_canary_50
"""
from __future__ import annotations

import argparse
import csv
import json
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
from src.outreach.targets import select_targets  # noqa: E402


def load_links(path):
    if not path:
        return []
    with Path(path).open(encoding="utf-8-sig", newline="") as fp:
        return [ArticleLink(r["clinic_uuid"].strip(), r["listing_status"].strip(), r["example_article_url"].strip(),
                            r["listing_url"].strip(), r["listing_kind"].strip(), r["source_row"].strip(),
                            r["article_id"].strip(), r["verified"].strip().lower() == "true")
                for r in csv.DictReader(fp)]


def read_records(args):
    from src.master.filters import Filters
    from src.repository.runtime_store import SupabaseRuntimeStore
    store = SupabaseRuntimeStore()  # runtime READ path (same as the app's Supabase backend)
    filters = Filters(active_only=True, hp_only=False, prefectures=args.prefecture, departments=args.department,
                      effective_ranks=args.rank)
    return store.query(filters, limit=args.scan)


def read_emails(ids):
    from src.repository.email_database import build_email_repositories
    bundle, status = build_email_repositories()
    if bundle is None:
        print(f"[warn] Email DB未接続: {status.message}（メールは全件未取得扱い）", file=sys.stderr)
        return {}
    wanted = set(ids)
    return {int(r["clinic_id"]): str(r.get("email") or "").strip()
            for r in bundle.email_research.primary_listing(limit=1_000_000)
            if int(r["clinic_id"]) in wanted and r.get("final_sales_eligible") is not False}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--articles", required=True)
    p.add_argument("--join")
    p.add_argument("--join-exhaustive", action="store_true")
    p.add_argument("--staff", required=True)
    p.add_argument("--start", type=date.fromisoformat, required=True)
    p.add_argument("--prefecture", action="append", default=[])
    p.add_argument("--department", action="append", default=[])
    p.add_argument("--rank", action="append", default=[])
    p.add_argument("--called-uuid", action="append", default=[])
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--scan", type=int, default=300)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()

    articles = read_master(args.articles)
    links = load_links(args.join)
    records = read_records(args)
    emails_seen: dict[int, str] = {}

    def lookup(ids):
        emails_seen.update(read_emails(ids))
        return emails_seen

    selection = select_targets(records, email_lookup=lookup, links=links, join_is_exhaustive=args.join_exhaustive,
                               limit=args.limit)
    settings = GenerationSettings(staff=args.staff, dates=five_weekdays(args.start),
                                  called_uuids=frozenset(args.called_uuid))
    results = generate_batch(selection.targets, articles, settings)
    report = audit(results, master_urls={a.url for a in articles},
                   source_uuids={int(r.get("id") or 0): str(r.get("uuid") or "").strip() for r in records},
                   emails=emails_seen, settings=settings)
    report.update(article_master_rows=len(articles), join_rows=len(links), scanned=selection.scanned,
                  skipped_no_uuid=selection.skipped_no_uuid, skipped_duplicate=selection.skipped_duplicate)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "canary_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.out / "canary_results.csv").write_bytes(to_csv_bytes(results, args.staff))
    print(json.dumps({k: v for k, v in report.items() if k != "issues"}, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
