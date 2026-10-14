"""Audit existing human reviews against historical AI candidates (not a new-model backtest)."""
from __future__ import annotations
import argparse
import csv
import json
from collections import Counter
from pathlib import Path


def audit(review_csv: Path, article_csv: Path):
    with article_csv.open(encoding="utf-8-sig", newline="") as f:
        urls = {r["URL"].strip() for r in csv.DictReader(f) if r.get("URL", "").strip()}
    with review_csv.open(encoding="utf-8-sig", newline="") as f:
        reviews = list(csv.DictReader(f))
    output = []
    for r in reviews:
        human = r["人間選定URL"].strip()
        candidates = [r[k].strip() for k in ("AI第一推奨URL", "AI第2候補URL", "AI第3候補URL") if r[k].strip()]
        reviewed = r["選定状態"] != "未レビュー"
        label = (
            "未レビュー" if not reviewed else
            "Top1一致" if human and candidates and human == candidates[0] else
            "Top3内" if human and human in candidates else
            "候補なし" if not candidates else
            "候補外"
        )
        output.append({"No": r["No"], "医院名": r["医院名"],
                       "判定": label, "人間選定URL原本一致": bool(human in urls),
                       "AI候補すべて原本一致": all(x in urls for x in candidates)})
    checked = [r for r in output if r["判定"] != "未レビュー"]
    return {
        "total": len(output), "reviewed": len(checked), "counts": dict(Counter(r["判定"] for r in output)),
        "top1_exact": sum(x["判定"] == "Top1一致" for x in checked),
        "top3_including_first": sum(x["判定"] in {"Top1一致", "Top3内"} for x in checked),
        "invalid_human_url_rows": [x["No"] for x in checked if not x["人間選定URL原本一致"]],
        "invalid_ai_url_rows": [x["No"] for x in output if not x["AI候補すべて原本一致"]],
        "note": "Historical decisions only. Does not measure the new recommender's accuracy.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reviews", type=Path, required=True)
    parser.add_argument("--articles", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.reviews, args.articles), ensure_ascii=False, indent=2))
