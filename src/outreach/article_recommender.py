"""Candidate selection for Crestix's existing Mynavi article master.

Pure local matching; no claims of live availability, no arbitrary invented URLs.
The master CSV is always read-only.
"""
from __future__ import annotations
import csv
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Article:
    title: str
    url: str
    keyword: str
    source_no: str


@dataclass(frozen=True)
class Recommendation:
    article: Article
    score: int
    reasons: tuple[str, ...]


def read_master(path: str | Path) -> list[Article]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        if not reader.fieldnames or not {"No.", "タイトル", "URL", "KW"}.issubset(reader.fieldnames):
            raise ValueError("管理CSVの必要列がありません")
        output = []
        seen = set()
        for row in reader:
            title, url, keyword = (row[k] or "").strip() for k in ("タイトル", "URL", "KW")
            parsed = urlsplit(url)
            if not title or not url or url in seen:
                continue
            if parsed.scheme != "https" or parsed.hostname != "clinic.mynavi.jp" or not parsed.path.startswith("/article/"):
                continue
            seen.add(url)
            output.append(Article(title, url, keyword, (row["No."] or "").strip()))
        return output


def recommend(
    articles: list[Article], *,
    prefecture: str,
    municipality: str,
    departments: tuple[str, ...],
    treatments: tuple[str, ...] = (),
    limit: int = 3,
) -> list[Recommendation]:
    """Conservative deterministic ranking, to be reviewed against prior human decisions.

    Matches exact source text only. Does not use an LLM or make assumptions about
    treatment expertise from a clinic's advertised department.
    """
    if not 1 <= limit <= 3:
        raise ValueError("候補数は1〜3件")
    terms = tuple(dict.fromkeys(x.strip() for x in departments if x.strip()))
    care = tuple(dict.fromkeys(x.strip() for x in treatments if x.strip()))
    if not terms and not care:
        return []
    results = []
    for article in articles:
        hay = article.title + " " + article.keyword
        reasons = []
        score = 0
        # Treatment matching outweighs the same-city general specialty match.
        matched_care = [x for x in care if x in hay]
        matched_dept = [x for x in terms if x in hay]
        if matched_care:
            score += 100
            reasons.append("治療・検査一致:" + "、".join(matched_care))
        if matched_dept:
            score += 40
            reasons.append("標榜科一致:" + "、".join(matched_dept))
        if not matched_care and not matched_dept:
            continue
        if municipality and municipality.strip() in hay:
            score += 25
            reasons.append("市区町村一致")
        elif prefecture and prefecture.strip() in hay:
            score += 12
            reasons.append("都道府県一致")
        # No cross-prefecture rejection: specialty matches may merit human review.
        results.append(Recommendation(article, score, tuple(reasons)))
    results.sort(key=lambda item: (-item.score, item.article.source_no, item.article.url))
    return results[:limit]
