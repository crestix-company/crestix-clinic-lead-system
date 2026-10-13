"""Candidate selection for Crestix's existing Mynavi article master.

Pure local matching; no claims of live availability, no arbitrary invented URLs.
The master CSV is always read-only.
"""
from __future__ import annotations
import csv
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_PLACE_SUFFIX_RE = re.compile(r"[都道府県区市町村]")


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
            title, url, keyword = ((row[k] or "").strip() for k in ("タイトル", "URL", "KW"))
            parsed = urlsplit(url)
            if not title or not url or url in seen:
                continue
            if parsed.scheme != "https" or parsed.hostname != "clinic.mynavi.jp" or not parsed.path.startswith("/article/"):
                continue
            seen.add(url)
            output.append(Article(title, url, keyword, (row["No."] or "").strip()))
        return output


def _article_region_hint(article: Article) -> str:
    """記事の地域ヒント文字列。タイトルの「の」より前、無ければキーワードの最初の空白より前。

    例: "東京都中央区の歯周病治療におすすめの歯科クリニック5選" -> "東京都中央区"
        "埼玉県のおすすめ糖尿病内科クリニック10選" -> "埼玉県"
    """
    for text, sep in ((article.title, "の"), (article.keyword, " ")):
        head = text.split(sep, 1)[0].strip()
        if head and _PLACE_SUFFIX_RE.search(head):
            return head
    return ""


def _region_tier(hint: str, *, prefecture: str, municipality: str) -> int:
    """0=市区町村一致（最良） 1=都道府県一致（地域限定なしの一般記事） 2=地域不明または地域競合（最下位）。

    都道府県一致はしても、ヒントに医院自身とは異なる市区町村名が含まれる場合（地域競合、
    例: 小金井市の医院に対する「東京都中央区の...」）は2に下げ、都道府県ボーナスを与えない。
    """
    prefecture, municipality = prefecture.strip(), municipality.strip()
    if municipality and municipality in hint:
        return 0
    if prefecture and prefecture in hint:
        other = hint.replace(prefecture, "", 1)
        if other and _PLACE_SUFFIX_RE.search(other):
            return 2
        return 1
    return 2


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

    地域一致を診療科・治療内容のスコアより優先する（ただし診療科・治療内容が全く一致しない
    記事は対象外のまま）。市区町村限定記事は、その市区町村と一致しない医院に対しては自動で
    最上位にしない。都道府県外の記事は、都道府県内に候補が無い場合のみ最後の手段として残す
    （その場合は呼び出し側 generate_one() の地域不一致チェックでNEEDS_REVIEWになる）。
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
        hint = _article_region_hint(article)
        tier = _region_tier(hint, prefecture=prefecture, municipality=municipality)
        if tier == 0:
            score += 25
            reasons.append("市区町村一致")
        elif tier == 1:
            score += 12
            reasons.append("都道府県一致")
        # tier == 2: 他地域限定記事、または地域情報なし。地域ボーナスは付与しない。
        results.append((tier, Recommendation(article, score, tuple(reasons))))
    # 地域一致の階層(tier)を最優先、スコアは同階層内のタイブレークにのみ使用する。
    results.sort(key=lambda item: (item[0], -item[1].score, item[1].article.source_no, item[1].article.url))
    return [rec for _, rec in results[:limit]]
