"""Analyze the 342 FETCH_FAILED clinics from the Phase 7 Full Run (Step 1 of the
transient-retry hotfix): read-only reporting only, no retry/mutation here.

progress.sqlite3's own last_error column is uninformative (every FETCH_FAILED
row says the literal coarse label "FETCH_ERROR" -- that's what
research_one_clinic() returns as fetch_status, not the underlying exception
text). The real WebError message text is in the shared final-results DB's
exclusion_context column (src/enrichment/safe_web.py raises WebError with one
of a small, exhaustive set of Japanese messages; _non_ok_rows() in
research_worker.py stores str(exc) there, one identical row per ACTIVE
category). This script joins the two DBs, classifies by the authoritative
message set, and recommends retry eligibility per the agreed policy -- it does
not retry anything itself.
"""
from __future__ import annotations

import csv
import re
import sqlite3
from collections import Counter
from pathlib import Path

PROGRESS_DB = Path("data/research_worker/phase7-fullrun-514bd9972b3bf20e/progress.sqlite3")
FINAL_DB = Path.home() / "CrestixData/clinic-lead/treatment_research_final.sqlite3"
OUT_CSV = PROGRESS_DB.parent / "fetch_failed_analysis.csv"

# Ordered (first match wins) against the exhaustive WebError message set in
# src/enrichment/safe_web.py (23 raise sites, grepped and enumerated by hand).
CATEGORY_RULES: list[tuple[str, re.Pattern, str, str]] = [
    ("RATE_LIMIT_429", re.compile(r"HTTP 429"), "retry", "429: 単発一時障害の可能性、Retry-After尊重のうえ1回だけ再試行"),
    ("HTTP_5XX", re.compile(r"HTTP 5\d\d"), "retry", "5xx: サーバ側一時障害の可能性、指数バックオフで再試行"),
    ("NOT_FOUND_404", re.compile(r"HTTP 404"), "no_retry", "404: 恒常的にページが存在しない"),
    ("BOT_BLOCK_403", re.compile(
        r"HTTP 40[13]|アクセス制限があるため|アクセス確認画面のため|verify you are human|checking your browser|cf-chl-"
    ), "no_retry", "401/403/Bot確認画面: Bot対策を回避しないため再試行しない"),
    ("DNS", re.compile(r"DNSの接続先|URLまたはDNSを確認できません"), "condition", "DNS解決失敗: 恒常的誤入力の場合が多く条件付き"),
    ("TIMEOUT_NETWORK", re.compile(r"接続・SSL・タイムアウトのエラーです|ページ取得が時間上限を超えました"), "retry",
     "接続/タイムアウト: 一時的ネットワーク障害の可能性が高い(現行ログはSSL単体を区別できない)"),
    ("ROBOTS", re.compile(r"robots\.txtで取得が許可されていません|取得間隔が長いため"), "no_retry", "robots.txt拒否/crawl-delay超過: 回避しない"),
    ("REDIRECT", re.compile(r"別ドメインへのリダイレクト|移転先URLが不明です|リダイレクト回数の上限"), "condition", "リダイレクト: 恒常的なドメイン移転の可能性があり条件付き"),
    ("CONTENT", re.compile(r"HTMLサイズが上限を超えました|未対応の圧縮形式です|HTMLページではありません"), "condition", "コンテンツ形式: 恒常的な場合が多く条件付き"),
]
DEFAULT_CATEGORY = ("OTHER", "no_retry", "既知パターン外、手動確認")


def classify(message: str) -> tuple[str, str, str]:
    for category, pattern, retry_flag, reason in CATEGORY_RULES:
        if pattern.search(message or ""):
            return category, retry_flag, reason
    return DEFAULT_CATEGORY


def main() -> int:
    prog = sqlite3.connect(f"file:{PROGRESS_DB}?mode=ro", uri=True)
    prog.row_factory = sqlite3.Row
    failed = prog.execute(
        "SELECT clinic_id, attempts, last_error FROM clinic_progress WHERE status='FETCH_FAILED' ORDER BY clinic_id"
    ).fetchall()
    print(f"FETCH_FAILED total (progress.sqlite3): {len(failed)}")

    final = sqlite3.connect(f"file:{FINAL_DB}?mode=ro", uri=True)
    final.row_factory = sqlite3.Row
    message_by_clinic: dict[int, str] = {}
    for row in final.execute(
        "SELECT DISTINCT clinic_id, exclusion_context FROM clinic_treatment_research_final "
        "WHERE research_status='FETCH_FAILED'"
    ):
        # Defensive: if a clinic somehow had >1 distinct message across its 43
        # category rows, keep the first seen and note it -- should not happen
        # since one clinic's fetch produces one WebError, one message, for all
        # ACTIVE categories (_non_ok_rows writes the identical text to every row).
        message_by_clinic.setdefault(row["clinic_id"], row["exclusion_context"])

    rows_out = []
    category_counts: Counter[str] = Counter()
    retry_counts: Counter[str] = Counter()
    exact_message_counts: Counter[str] = Counter()
    category_examples: dict[str, list[int]] = {}
    attempts_dist: Counter[int] = Counter()

    missing_message = 0
    for row in failed:
        clinic_id = row["clinic_id"]
        attempts_dist[row["attempts"]] += 1
        message = message_by_clinic.get(clinic_id, "")
        if not message:
            missing_message += 1
        exact_message_counts[message] += 1
        category, retry_flag, reason = classify(message)
        category_counts[category] += 1
        retry_counts[retry_flag] += 1
        category_examples.setdefault(category, [])
        if len(category_examples[category]) < 10:
            category_examples[category].append(clinic_id)
        rows_out.append({
            "clinic_id": clinic_id, "attempts": row["attempts"], "last_error": message,
            "category": category, "retry_recommended": "true" if retry_flag == "retry" else "false",
            "retry_reason": reason,
        })

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["clinic_id", "attempts", "last_error", "category",
                                                "retry_recommended", "retry_reason"])
        writer.writeheader()
        writer.writerows(rows_out)

    print(f"\nmissing exclusion_context in final DB: {missing_message}")
    print(f"\n=== 2. last_error (exclusion_context) exact-match counts ===")
    for msg, count in exact_message_counts.most_common():
        print(f"  {count:4d}  {msg!r}")

    print(f"\n=== 3. category breakdown ===")
    for category, _, _, _ in CATEGORY_RULES:
        print(f"  {category:16s}: {category_counts.get(category, 0)}")
    print(f"  {'OTHER':16s}: {category_counts.get('OTHER', 0)}")

    print(f"\n=== 4. representative clinic_ids per category (max 10) ===")
    for category in [c for c, *_ in CATEGORY_RULES] + ["OTHER"]:
        if category in category_examples:
            print(f"  {category}: {category_examples[category]}")

    print(f"\n=== 5. attempts distribution ===")
    for attempts, count in sorted(attempts_dist.items()):
        print(f"  attempts={attempts}: {count}")

    print(f"\n=== retry_recommended breakdown ===")
    for flag, count in retry_counts.items():
        print(f"  {flag}: {count}")

    print(f"\nWrote {OUT_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
