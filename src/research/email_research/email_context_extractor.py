"""メール候補の周辺文脈(nearby_text等)を抽出する共通ユーティリティ(Hardening Phase 2)。

背景(Problem B): Fresh 100-clinic Canaryで、VERIFIED_EMAILの大半(4/5)がEmail Role
Classificationで UNKNOWN になった。原因は、mailtoリンクの"anchor text"(多くの場合メール
文字列そのもの)だけをevidence_text/context_textとして保存しており、実際にページ上で
メールを説明している周辺の見出し・段落(例:「初診予約：…メールでご予約の場合：」)を
一切捨てていたため。

本モジュールはHTML解析だけを行う純粋関数(推測・外部通信は一切しない)。ページ全文は
保存しない(nearby_textは NEARBY_TEXT_MAX_CHARS 文字に丸める)。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

NEARBY_TEXT_MAX_CHARS = 800
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "strong", "b")
_BLOCK_TAGS = ("p", "li")
_EMAIL_LIKE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+\-]*@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


@dataclass(frozen=True)
class EmailContext:
    page_title: str = ""
    anchor_text: str = ""
    mailto_href: str = ""
    immediate_text: str = ""
    parent_text: str = ""
    grandparent_text: str = ""
    nearest_heading: str = ""
    section_heading: str = ""
    previous_heading: str = ""
    previous_paragraph: str = ""
    next_paragraph: str = ""
    nearby_text: str = ""
    footer_text: str = ""
    operator_text: str = ""
    organization_names: str = ""
    element_tag: str = ""  # "a"(mailtoリンク) | "text"(本文プレーンテキスト) | ""(見つからず)
    content_hash: str = ""  # nearby_textのsha256先頭12桁(Offline Replay時の同一性確認用)


def _clean(text: str, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit]


def _find_email_node(soup: BeautifulSoup, email: str):
    """mailtoリンクを優先して探す。無ければ本文中のプレーンテキスト一致を探す。

    戻り値のnodeは常に「メールを含む要素自身を囲む、直近のコンテナ要素」
    (= immediate_textの基準にしたい要素)を指す。どちらも見つからなければ
    (None, "", "")を返す(推測しない)。
    """
    email_lower = email.lower()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("mailto:") and email_lower in href.lower():
            return a.parent, a.get_text(" ", strip=True), href

    pattern = re.compile(re.escape(email), re.I)
    for text_node in soup.find_all(string=pattern):
        parent = text_node.parent
        if parent is not None:
            return parent, "", ""
    return None, "", ""


def extract_email_context(html: str, email: str, page_title: str = "") -> EmailContext:
    """html中でemailに言及する要素の周辺文脈を組み立てる。

    見つからない場合はpage_titleだけを持つ空のEmailContextを返す(推測で埋めない)。
    """
    soup = BeautifulSoup(html or "", "html.parser")
    parent, anchor_text, mailto_href = _find_email_node(soup, email)
    if parent is None:
        return EmailContext(page_title=_clean(page_title))

    grandparent = parent.parent if parent is not None else None

    immediate = parent.get_text(" ", strip=True) if parent is not None else ""
    grandparent_text = grandparent.get_text(" ", strip=True) if grandparent is not None else ""

    heading = parent.find_previous(_HEADING_TAGS) if parent is not None else None
    section_heading = heading.get_text(" ", strip=True) if heading is not None else ""
    previous_heading = section_heading

    prev_block = parent.find_previous(_BLOCK_TAGS) if parent is not None else None
    next_block = parent.find_next(_BLOCK_TAGS) if parent is not None else None
    previous_paragraph = prev_block.get_text(" ", strip=True) if prev_block is not None else ""
    next_paragraph = next_block.get_text(" ", strip=True) if next_block is not None else ""

    # 前後の段落が「別のメールアドレス」を含む場合、それは別の連絡先ブロック(例:
    # 別部署/別メールの案内)である可能性が高いため、nearby_textへは混ぜない
    # (1件のメールのownership判定に、別の連絡先の文脈を汚染させないため)。
    email_lower = email.lower()

    def _belongs_to_different_contact(text: str) -> bool:
        return bool(_EMAIL_LIKE_RE.search(text)) and email_lower not in text.lower()

    if _belongs_to_different_contact(previous_paragraph):
        previous_paragraph = ""
    if _belongs_to_different_contact(next_paragraph):
        next_paragraph = ""

    nearby_parts = [section_heading, previous_paragraph, immediate, next_paragraph]
    nearby_text = _clean(" ".join(p for p in nearby_parts if p), limit=NEARBY_TEXT_MAX_CHARS)

    footer = soup.find("footer")
    footer_text = _clean(footer.get_text(" ", strip=True) if footer is not None else "", limit=500)
    operator_nodes = soup.select(
        '[class*="operator"], [id*="operator"], [class*="company"], [id*="company"], '
        '[class*="corporate"], [id*="corporate"], [class*="organization"], [id*="organization"]'
    )
    operator_text = _clean(" ".join(n.get_text(" ", strip=True) for n in operator_nodes), limit=500)
    ownership_scope = " ".join((page_title, section_heading, previous_paragraph, immediate,
                                next_paragraph, footer_text, operator_text))
    organization_names = _clean(" / ".join(dict.fromkeys(re.findall(
        r"[^\s、。|/]{2,40}(?:病院|診療所|医院|クリニック|医師会|歯科医師会|学会|財団|協会|医療法人)",
        ownership_scope,
    ))), limit=500)

    return EmailContext(
        page_title=_clean(page_title),
        anchor_text=_clean(anchor_text),
        mailto_href=mailto_href,
        immediate_text=_clean(immediate),
        parent_text=_clean(immediate),
        grandparent_text=_clean(grandparent_text),
        nearest_heading=_clean(section_heading),
        section_heading=_clean(section_heading),
        previous_heading=_clean(previous_heading),
        previous_paragraph=_clean(previous_paragraph),
        next_paragraph=_clean(next_paragraph),
        nearby_text=nearby_text,
        footer_text=footer_text,
        operator_text=operator_text,
        organization_names=organization_names,
        element_tag="a" if mailto_href else "text",
        content_hash=hashlib.sha256(nearby_text.encode("utf-8")).hexdigest()[:12] if nearby_text else "",
    )
