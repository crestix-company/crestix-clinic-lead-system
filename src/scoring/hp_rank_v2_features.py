"""HP ABC v2 candidate用の追加特徴量抽出（HP Rank Recalibration v2, Stage2で検証済み）。

本番rank_hp()には未接続。既存の11特徴（rank_hp()のreasons）と合わせて
`src/scoring/hp_rank_recalibration_v2.candidate_hp_rank_v2()`へ渡すためのfeature名のsetを返す。
Treatmentカテゴリは一切使わない（HP ABC判定とTreatmentを独立に保つ）。
"""
import re
from urllib.parse import urlparse

from src.enrichment.hp_analysis import is_official_candidate, page_soup, host
from src.scoring.research_scoring import production_companies

FREE_BUILDER_SUFFIXES = (
    "jimdofree.com", "jimdo.com", "wixsite.com", "weebly.com", "amebaownd.com",
    "goope.jp", "shopify.com", "crayonsite.com", "crayonsite.net", "fc2.com",
    "studio.site", "strikingly.com", "google.com",
)

LEGACY_TAGS = re.compile(r"<font\b|<center\b|<marquee\b|<blink\b", re.I)
FLASH_RE = re.compile(r"<object\b[^>]*flash|<embed\b[^>]*swf|\.swf\b", re.I)
GENERATOR_WP = re.compile(r"wordpress", re.I)
GENERATOR_WIX = re.compile(r"\bwix\b", re.I)
GENERATOR_JIMDO = re.compile(r"jimdo", re.I)
COPYRIGHT_YEAR = re.compile(r"(?:copyright|©|\(c\))[^0-9]{0,15}(\d{4})", re.I)
RECRUIT_PATH = re.compile(r"/(?:recruit|saiyo|careers?)(?:/|$)", re.I)
RECRUIT_TEXT = re.compile(r"採用情報|スタッフ募集|求人")
FAQ_TEXT = re.compile(r"よくある質問|Q&A|FAQ", re.I)
SYMPTOM_TEXT = re.compile(r"症状|疾患|こんな症状|このような方")
NEWS_PATH = re.compile(r"/(?:news|blog|topics?|information|notice)(?:/|$)", re.I)
DATE_RE = re.compile(r"(20\d{2})[./\-年](\d{1,2})[./\-月](\d{1,2})")


def extract_v2_features(pages, record=None):
    """取得済みページ(Pageのlist)から、HP ABC v2 candidate用の新規特徴name setを返す。
    pagesはTreatment Research・旧rank_hp()と同じfetch結果を再利用する想定（再取得しない）。
    """
    pages = [p for p in pages if is_official_candidate(p.url)]
    feats = set()
    if not pages:
        return feats
    all_html = "\n".join(p.html for p in pages)
    all_text = "\n".join(p.main_text for p in pages)
    home = pages[0]
    home_soup = page_soup(home)

    has_stylesheet = any('rel="stylesheet"' in p.html or "rel='stylesheet'" in p.html for p in pages)
    table_layout_pages = sum(1 for p in pages if len(page_soup(p).select("table")) >= 3)
    if not has_stylesheet:
        feats.add("no_linked_stylesheet")
    if table_layout_pages >= 1:
        feats.add("heavy_table_layout")
    if LEGACY_TAGS.search(all_html):
        feats.add("legacy_html_tags")
    if FLASH_RE.search(all_html):
        feats.add("flash_or_applet")

    generator = " ".join(m.get("content", "") for m in home_soup.select('meta[name="generator"]'))
    h = host(home.url)
    is_free_builder_domain = any(h.endswith(suf) for suf in FREE_BUILDER_SUFFIXES)
    if GENERATOR_WP.search(generator):
        feats.add("cms_wordpress")
    if GENERATOR_WIX.search(generator) or h.endswith("wixsite.com"):
        feats.add("builder_wix")
    if GENERATOR_JIMDO.search(generator) or "jimdo" in h:
        feats.add("builder_jimdo")
    if is_free_builder_domain:
        feats.add("free_builder_subdomain")
    else:
        feats.add("custom_apex_domain")

    if home_soup.select('meta[property^="og:"]'):
        feats.add("has_ogp")
    if home_soup.select('script[type="application/ld+json"]') or re.search(r"schema\.org", home.html):
        feats.add("has_structured_data")
    if home_soup.select('link[rel="icon"]') or home_soup.select('link[rel="shortcut icon"]'):
        feats.add("has_favicon")
    if home_soup.select('link[rel="canonical"]'):
        feats.add("has_canonical")
    title = (home_soup.title.get_text(strip=True) if home_soup.title else "")
    desc_tag = home_soup.select_one('meta[name="description"]')
    desc = (desc_tag.get("content", "") if desc_tag else "")
    if title and 10 <= len(title) <= 60 and desc and len(desc) >= 40:
        feats.add("good_seo_title_desc")

    try:
        pc = production_companies(pages)
        if pc.get("hp_production_companies"):
            feats.add("hp_production_company_credited")
    except Exception:
        pass

    if len(pages) <= 2:
        feats.add("single_page_or_near_single_page_site")
    if len(pages) >= 6:
        feats.add("many_pages_rich_site")
    if RECRUIT_PATH.search(" ".join(p.url for p in pages)) or RECRUIT_TEXT.search(all_text):
        feats.add("has_recruit_page")
    if FAQ_TEXT.search(all_text):
        feats.add("has_faq")
    if SYMPTOM_TEXT.search(all_text) and len(all_text) > 500:
        feats.add("symptom_disease_content")

    years = [int(y) for y in COPYRIGHT_YEAR.findall(all_html)]
    if years:
        latest = max(years)
        feats.add("copyright_year_present")
        if latest >= 2024:
            feats.add("copyright_year_recent")
        elif latest <= 2018:
            feats.add("copyright_year_stale")
    news_pages = [p for p in pages if NEWS_PATH.search(urlparse(p.url).path)]
    dates_found = []
    for p in (news_pages or pages):
        for y, m, d in DATE_RE.findall(p.main_text[:3000]):
            try:
                dates_found.append(int(y))
            except ValueError:
                pass
    if dates_found:
        feats.add("has_news_or_blog_dates")
        if max(dates_found) >= 2025:
            feats.add("news_blog_recently_updated")
        elif max(dates_found) <= 2020:
            feats.add("news_blog_stale")

    return feats
