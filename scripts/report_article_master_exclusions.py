"""記事マスター原本から read_master() が除外した行を分類して報告するだけのスクリプト。

原本は一切書き込まない（読み取りのみ）。URLの自動補正は行わない。
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from urllib.parse import urlsplit

REVIEW_TEXT_MARKERS = ("つ星",)


def classify(rows: list[dict]) -> dict:
    review_text_excluded = []
    url_format_needs_review = []
    seen = set()
    for row in rows:
        no = (row.get("No.") or "").strip()
        title = (row.get("タイトル") or "").strip()
        url = (row.get("URL") or "").strip()
        published = (row.get("公開日") or "").strip()

        if any(marker in title for marker in REVIEW_TEXT_MARKERS) and not url.startswith(("http://", "https://")):
            review_text_excluded.append({
                "No": no, "タイトル": title, "URL先頭60文字": url[:60],
                "理由": "記事データではなく口コミ星評価テキストが混入（原本側のデータ問題）",
                "対応": "記事選定対象外（read_master()で自動的に除外済み・URL補正なし）",
            })
            continue

        if not title or not url:
            continue  # read_master()も同様に無視する空行

        if url in seen:
            continue
        seen.add(url)

        parsed = urlsplit(url)
        valid = parsed.scheme == "https" and parsed.hostname == "clinic.mynavi.jp" and parsed.path.startswith("/article/")
        if valid:
            continue

        if parsed.scheme not in ("https", "http") or not url.lstrip("https://").lstrip("http://"):
            reason = f"URLの先頭に不正な文字が混入している可能性: {url!r}"
        elif parsed.scheme != "https":
            reason = f"スキームがhttpsではない: {parsed.scheme}"
        elif not parsed.path.startswith("/article/"):
            reason = f"パスが/article/ではない: {parsed.path}"
        else:
            reason = "URL形式が不正（詳細不明）"
        url_format_needs_review.append({
            "No": no, "タイトル": title, "公開日": published, "URL（原本のまま・補正なし）": url,
            "理由": reason, "対応": "要確認（原本は未変更。自動補正は行っていない）",
        })
    return {
        "口コミテキスト混入_除外件数": len(review_text_excluded),
        "口コミテキスト混入_除外一覧": review_text_excluded,
        "URL形式要確認_件数": len(url_format_needs_review),
        "URL形式要確認_一覧": url_format_needs_review,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--articles", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    with args.articles.open(encoding="utf-8-sig", newline="") as fp:
        rows = list(csv.DictReader(fp))

    report = classify(rows)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "article_master_exclusions.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    with (args.out / "url_format_needs_review.csv").open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=["No", "タイトル", "公開日", "URL（原本のまま・補正なし）", "理由", "対応"])
        writer.writeheader()
        writer.writerows(report["URL形式要確認_一覧"])

    print(json.dumps({k: v for k, v in report.items() if not k.endswith("一覧")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
