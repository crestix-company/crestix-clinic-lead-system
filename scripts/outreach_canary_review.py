"""Build a private, human-readable review of five outreach Canary candidates.

This command is local and read-only apart from creating a new 0600 report. It never calls
Gmail, never reads OAuth tokens, and refuses to overwrite an existing artifact.
"""
from __future__ import annotations

import argparse
import csv
import html
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.outreach.article_recommender import read_master  # noqa: E402
from src.outreach.compose import (  # noqa: E402
    CALLED_LINE, NOT_CALLED_LINE, TEMPLATE_LISTED, TEMPLATE_RECOMMEND,
)
from src.outreach.secure_artifacts import write_private_text  # noqa: E402

LISTING_CLAIMS = ("掲載させていただいている", "掲載いただいている")


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


def _pre(value: str) -> str:
    return "<pre>" + html.escape(value or "") + "</pre>"


def build_report(candidate_csv: Path, article_master: Path) -> tuple[str, dict[str, int]]:
    rows = _rows(candidate_csv)
    if len(rows) != 5:
        raise ValueError(f"Canary候補は5件である必要があります: {len(rows)}")
    master_urls = {article.url for article in read_master(article_master)}
    sections = [
        "# Gmail Canary候補 事前監査",
        "",
        "- Gmail OAuth: ACCOUNT_MISMATCH",
        "- Gmail既存下書き重複確認: NOT_RUN_ACCOUNT_MISMATCH",
        "- Gmail書き込み・送信: 0",
        "- Production DB書き込み: 0",
        "",
        "全候補はGmailアカウント一致確認、既存下書き重複確認、候補日時設定、保存前テンプレート確認が終わるまでNEEDS_REVIEWです。",
    ]
    counts = {
        "candidates": len(rows), "url_valid": 0, "neutral_copy_valid": 0,
        "schedule_missing": 0, "ready": 0,
    }
    for number, row in enumerate(rows, 1):
        body = row.get("body", "")
        article_status = row.get("article_status", "")
        template = row.get("template", "")
        url = row.get("recommended_url", "")
        url_valid = bool(url and url in master_urls)
        if url_valid:
            counts["url_valid"] += 1
        schedule_missing = row.get("candidate_schedule", "") in {"", "（候補日時未設定）"}
        if schedule_missing:
            counts["schedule_missing"] += 1
        unconfirmed_safe = (
            article_status == "未確認"
            and template == TEMPLATE_RECOMMEND
            and NOT_CALLED_LINE in body
            and CALLED_LINE not in body
            and not any(claim in body for claim in LISTING_CLAIMS)
        )
        listed_v1 = article_status == "記事あり" and template == TEMPLATE_LISTED
        if unconfirmed_safe or listed_v1:
            counts["neutral_copy_valid"] += 1
        reasons = [item for item in (
            row.get("blocker_reasons", ""),
            row.get("gmail_blockers", ""),
            "接続Gmailアカウント不一致",
            "Gmail既存下書き重複確認未実施",
        ) if item]
        status = "READY" if not reasons and url_valid and not schedule_missing else "NEEDS_REVIEW"
        if status == "READY":
            counts["ready"] += 1
        template_check = (
            "LISTED_FIXED_V1一致" if listed_v1
            else "掲載未確認のためLISTED_FIXED_V1は不適用。RECOMMEND_RELATED_V2の中立文面に一致"
            if unconfirmed_safe else "テンプレート要確認"
        )
        sections.extend([
            "",
            f"## {number}. {row.get('clinic_name', '')}",
            "",
            f"- 判定: {status}",
            f"- Clinic UUID: `{row.get('clinic_uuid', '')}`",
            f"- Email Candidate ID: `{row.get('email_candidate_id', '')}`",
            f"- Primary Email: `{row.get('to_email', '')}`",
            f"- 掲載状況: {article_status}",
            f"- 院長名出典: {row.get('director_name_source', '')}",
            f"- 電話履歴: {row.get('phone_history_status', '')}",
            f"- 選定記事: {row.get('recommended_title', '')}",
            f"- 記事URL: {url}",
            f"- URL原本一致: {'PASS' if url_valid else 'FAIL'}",
            f"- 選定理由: {row.get('recommend_reason', '')}",
            f"- 候補日時: {row.get('candidate_schedule', '') or '未設定'}",
            f"- テンプレート確認: {template_check}",
            f"- ブロッカー: {' / '.join(reasons) or 'なし'}",
            "",
            "### 件名",
            "",
            _pre(row.get("subject", "")),
            "",
            "### 本文",
            "",
            _pre(body),
        ])
    return "\n".join(sections) + "\n", counts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--articles", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    report, counts = build_report(args.candidates, args.articles)
    write_private_text(args.out, report)
    print(" ".join(f"{key}={value}" for key, value in counts.items()) + f" out={args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
