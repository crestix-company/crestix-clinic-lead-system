"""現行の記事選定ロジック(recommend())を25医院の判断履歴で検証する。

既存の audit_outreach_historical_choices.py は「過去にAIが提示した候補URL」と
「人間選定URL」を履歴CSVの記載どおり比較するだけで、現行ロジックは実行しない。
このスクリプトは、履歴CSVに医院側の入力(都道府県/市区町村/標榜科/治療)が
存在する場合に限り、現行の recommend() を実際に呼び出してTop1/Top3一致を測る。

入力(都道府県・市区町村・標榜科・治療)が履歴CSVに存在しない場合は、
推測せず「入力情報不足」として分類する（医院名や記事タイトルからの逆算はしない）。
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from src.outreach.article_recommender import read_master, recommend

INPUT_FIELD_CANDIDATES = {
    "prefecture": ("都道府県",),
    "municipality": ("市区町村",),
    "departments": ("標榜科", "診療科"),
    "treatments": ("治療", "検査", "治療・検査"),
}


def detect_input_columns(fieldnames: list[str]) -> dict:
    found = {}
    for key, candidates in INPUT_FIELD_CANDIDATES.items():
        for candidate in candidates:
            if candidate in fieldnames:
                found[key] = candidate
                break
    return found


def validate(review_csv: Path, article_csv: Path) -> dict:
    articles = read_master(article_csv)
    with review_csv.open(encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    input_columns = detect_input_columns(fieldnames)
    missing_required = [k for k in ("prefecture", "municipality", "departments") if k not in input_columns]

    reviewed = [r for r in rows if r.get("選定状態", "") != "未レビュー"]
    results = []
    for row in reviewed:
        human_url = (row.get("人間選定URL") or "").strip()
        if missing_required:
            results.append({
                "No": row.get("No"), "医院名": row.get("医院名"),
                "判定": "入力情報不足",
                "不足項目": missing_required,
                "理由": "履歴CSVに都道府県/市区町村/標榜科の入力列が無く、医院名や記事タイトルからの推測は行っていない",
            })
            continue
        prefecture = (row.get(input_columns["prefecture"]) or "").strip()
        municipality = (row.get(input_columns["municipality"]) or "").strip()
        departments = tuple(x.strip() for x in (row.get(input_columns["departments"]) or "").split("、") if x.strip())
        treatments = tuple(x.strip() for x in (row.get(input_columns.get("treatments", ""), "") or "").split("、") if x.strip()) \
            if "treatments" in input_columns else ()
        recs = recommend(articles, prefecture=prefecture, municipality=municipality,
                         departments=departments, treatments=treatments)
        urls = [r.article.url for r in recs]
        label = (
            "Top1一致" if urls and human_url == urls[0] else
            "Top3内" if human_url in urls else
            "候補なし" if not urls else
            "候補外"
        )
        results.append({"No": row.get("No"), "医院名": row.get("医院名"), "判定": label})

    counts = {}
    for r in results:
        counts[r["判定"]] = counts.get(r["判定"], 0) + 1
    scoreable = [r for r in results if r["判定"] != "入力情報不足"]

    return {
        "note": "これは現行recommend()を実行した新規の精度検証であり、既存audit_outreach_historical_choices.pyが報告する"
                "「過去のAI候補vs人間選定」の一致率(73.3%)とは別の指標。両者を混同しないこと。",
        "履歴CSVの入力列検出結果": input_columns,
        "不足している必須入力列": missing_required,
        "reviewed_total": len(reviewed),
        "入力情報不足件数": sum(1 for r in results if r["判定"] == "入力情報不足"),
        "新ロジックで評価できた件数": len(scoreable),
        "新ロジックTop1一致": sum(1 for r in scoreable if r["判定"] == "Top1一致"),
        "新ロジックTop3内(Top1含む)": sum(1 for r in scoreable if r["判定"] in {"Top1一致", "Top3内"}),
        "counts": counts,
        "details": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reviews", type=Path, required=True)
    parser.add_argument("--articles", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(validate(args.reviews, args.articles), ensure_ascii=False, indent=2))
