"""HPランク改善 Phase2.1: 実HTMLから抽出するD detector候補特徴量（未採用・検証専用）。

現行rank_hp()のfeature抽出（hp_rank_features）とは別に、cacheされた
テキストだけでは検証できなかった特徴（画像枚数・写真候補・HTMLサイズ等）を
実HTMLから直接抽出する。本番scoring pathには接続していない。
"""
import re
from bs4 import BeautifulSoup
from src.enrichment.hp_analysis import Page, host, domain_is

SNS_DOMAINS = ["instagram.com", "youtube.com", "tiktok.com", "facebook.com", "x.com"]
YEAR_RE = re.compile(r"(19|20)\d{2}")
COPYRIGHT_CTX = re.compile(r"(©|copyright|all rights reserved)[^\n]{0,40}", re.I)
UPDATE_CTX = re.compile(r"(更新日|last\s*updated|updated:)[^\n]{0,40}", re.I)
DEPRECATED_TAGS = ["font", "center", "marquee", "blink", "frame", "frameset"]


def _year_near(html, pattern):
    years = []
    for m in pattern.finditer(html):
        s, e = m.span()
        seg = html[max(0, s - 20):e + 30]
        years += [int(y.group(0)) for y in YEAR_RE.finditer(seg)]
    return max(years) if years else None


def extract_html_features(url, html):
    """rank_hp()のhp_rank_featuresとは独立の、D detector候補特徴セット。"""
    page = Page(url, html)
    soup = BeautifulSoup(html, "html.parser")
    imgs = soup.select("img")
    links = page.links
    same_host_links = [a for a in links if host(a["url"]) == host(url)]
    tables = soup.select("table")
    iframes = soup.select("iframe")
    all_tags = soup.find_all(True)
    styled_tags = [t for t in all_tags if t.get("style")]
    deprecated_found = any(soup.select(tag) for tag in DEPRECATED_TAGS) or bool(soup.select("[bgcolor]"))
    has_media_query = bool(re.search(r"@media", html, re.I))

    max_copyright_year = _year_near(html, COPYRIGHT_CTX)
    max_update_year = _year_near(html, UPDATE_CTX)

    doctor_photo_candidate = any(
        re.search(r"院長|医師|ドクター|doctor", (img.get("alt") or ""), re.I) for img in imgs
    )
    video_present = bool(soup.select("video")) or any(
        domain_is(f.get("src", ""), d) for f in iframes for d in ["youtube.com", "youtube-nocookie.com", "vimeo.com"]
    )
    sns_present = any(any(domain_is(a["url"], d) for d in SNS_DOMAINS) for a in links)
    reservation_cta = any(re.search(r"予約|受付|reserve|reservation|booking", a["text"] + a["url"], re.I) for a in links)
    contact_cta = any(re.search(r"お問い合わせ|問合せ|tel:", a["text"] + a["url"]) for a in links)
    phone_cta = any(a["url"].lower().startswith("tel:") for a in links)
    viewport = bool(soup.select('meta[name="viewport"]'))

    return {
        "html_byte_size": len(html.encode("utf-8")),
        "visible_text_length": len(page.main_text),
        "image_count": len(imgs),
        "internal_link_count": len(same_host_links),
        "iframe_count": len(iframes),
        "table_count": len(tables),
        "viewport_meta": viewport,
        "responsive_indicator": bool(viewport and has_media_query),
        "table_layout_indicator": len(tables) >= 3,
        "video_present": video_present,
        "sns_link_present": sns_present,
        "web_reservation_cta": reservation_cta,
        "contact_cta": contact_cta,
        "phone_cta": phone_cta,
        "doctor_photo_candidate": doctor_photo_candidate,
        "clinic_photo_many": len(imgs) >= 10,
        "copyright_year": max_copyright_year,
        "copyright_year_recent": bool(max_copyright_year and max_copyright_year >= 2024),
        "copyright_year_old": bool(max_copyright_year and max_copyright_year <= 2019),
        "update_year": max_update_year,
        "legacy_html_indicator": deprecated_found,
        "deprecated_markup": deprecated_found,
        "inline_style_ratio": (len(styled_tags) / len(all_tags)) if all_tags else 0.0,
        "https_status": url.startswith("https://"),
        "mobile_usability_proxy": bool(viewport and not deprecated_found),
    }
