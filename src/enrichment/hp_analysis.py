"""公式医院の本人確認、ページ構造の解析、媒体の帰属確認。"""
from dataclasses import dataclass, field
from urllib.parse import urlparse, urljoin, urldefrag
import re
import unicodedata
from bs4 import BeautifulSoup
from src.normalizer.clinic_name import normalize_clinic_name, normalize_person
from src.normalizer.address import normalize_address
from src.normalizer.phone import normalize_phone

NON_OFFICIAL = {"doctorsfile.jp","medicaldoc.jp","epark.jp","caloo.jp","caloo.com","mynavi.jp","mynavi-ms.jp",
               "instagram.com","facebook.com","x.com","twitter.com","youtube.com","youtu.be","tiktok.com",
               "tokyo-doctors.com","kanagawa-doctors.com","chiba-doctors.com","byoinnavi.jp","hospita.jp",
               "qlife.jp","scuel.me","haisha-yoyaku.jp","itp.ne.jp","maps.google.com","google.com","yahoo.co.jp",
               "job-medley.com","senshin-daido-life.jp","fdoc.jp","gmo-clinic-map.com","web-clover.net"}


def host(url):
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def domain_is(url, domain):
    h = host(url)
    return h == domain or h.endswith("."+domain)


def is_official_candidate(url):
    p = urlparse(url)
    return p.scheme in {"http","https"} and bool(host(url)) and not any(domain_is(url,d) for d in NON_OFFICIAL)


def canonical_page(url):
    p = urlparse(urldefrag(url)[0])
    if p.scheme not in {"http","https"} or not p.hostname:
        return ""
    # 計測パラメータとクエリの無限ループを避ける。通常HTMLページだけを対象。
    return p._replace(query="",fragment="").geturl()


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_text(value):
    """Strip NUL and other CSV/parser-breaking control chars from fetched-page text.

    Keeps tab/newline/carriage-return, Japanese text and punctuation intact; only
    removes bytes that a CSV parser (or Excel) treats as corrupt binary markers.
    """
    if not isinstance(value, str):
        return value
    return _CONTROL_CHARS_RE.sub("", value)


@dataclass
class Page:
    url: str
    html: str
    title: str = ""
    text: str = ""
    main_text: str = ""
    headings: str = ""
    links: list = field(default_factory=list)

    def __post_init__(self):
        soup = BeautifulSoup(self.html,"html.parser")
        self.title = sanitize_text(soup.title.get_text(" ",strip=True) if soup.title else "")
        self.headings = sanitize_text(" ".join(x.get_text(" ",strip=True) for x in soup.select("h1,h2")))
        self.links = [{"url":urljoin(self.url,a.get("href","")),"text":sanitize_text(a.get_text(" ",strip=True))} for a in soup.select("a[href]")]
        for x in soup.select("script,style,noscript,template"):
            x.decompose()
        self.text = sanitize_text(soup.get_text(" ",strip=True))
        for x in soup.select("nav,footer,header,aside,[role=navigation]"):
            x.decompose()
        self.main_text = sanitize_text(soup.get_text(" ",strip=True))

    def evidence(self):
        return {"url":self.url,"title":self.title,"headings":self.headings[:2000],"text":self.main_text[:15000]}


def page_soup(page):
    """読み取り専用の解析結果。同じPageの同じHTMLは html.parser で1回だけ解析して使い回す。

    キャッシュはPageオブジェクト自身に持つため、その医院の調査中だけ有効（医院を跨がない）。
    HTMLが差し替わった場合は解析し直す。返したsoupを変更（decompose等）する処理には渡さないこと。
    """
    cached = page.__dict__.get("_readonly_soup")
    if cached is not None and cached[0] is page.html:
        return cached[1]
    soup = BeautifulSoup(page.html,"html.parser")
    page.__dict__["_readonly_soup"] = (page.html,soup)
    return soup


def identity(record, page, official=True):
    if official and not is_official_candidate(page.url):
        return {"verified":False,"score":0,"reasons":["外部媒体・口コミ・SNSは公式HPとして採用しない"]}
    text = unicodedata.normalize("NFKC",page.text)
    name = normalize_clinic_name(record.get("clinic_name"))
    # 外部媒体の本文に複数医院があるだけでは本人とみなさない。
    name_scope = page.title+" "+page.headings
    name_ok = bool(name and len(name)>=3 and name in normalize_clinic_name(name_scope))
    phone = normalize_phone(record.get("phone"))
    found_phones = {normalize_phone(m[0]) for m in re.finditer(r"(?<!\d)0\d[\d()\s\-－ー]{6,18}\d(?!\d)",text)}
    soup = page_soup(page)
    found_phones.update(normalize_phone(x.get("href","")[4:]) for x in soup.select('a[href^="tel:"]'))
    exact_phone_pattern = r"(?<!\d)"+r"[\s()\-－ー]*".join(re.escape(d) for d in phone)+r"(?!\d)" if phone else r"(?!)"
    phone_ok = bool(phone and len(phone)>=9 and (phone in found_phones or re.search(exact_phone_pattern,text)))
    address = normalize_address(record.get("address"))
    addr_ok = bool(len(address)>=8 and address in normalize_address(text))
    person = normalize_person(record.get("manager_name"))
    person_ok = bool(person and len(person)>=3 and person in normalize_person(text))
    reasons = [s for ok,s in [(phone_ok,"電話番号一致"),(name_ok,"医院名がページ見出しに一致"),(addr_ok,"住所一致"),(person_ok,"院長名一致")] if ok]
    verified = name_ok and (phone_ok or addr_ok)
    return {"verified":bool(verified),"score":50*phone_ok+40*name_ok+30*addr_ok+20*person_ok,
            "reasons":reasons or ["医院の本人確認情報が不足"],"phone_match":phone_ok,"name_match":name_ok,"address_match":addr_ok}


def keyword_match(keyword, text):
    keyword,text = unicodedata.normalize("NFKC",keyword),unicodedata.normalize("NFKC",text)
    if re.fullmatch(r"[A-Za-z0-9]+",keyword):
        return len(re.findall(r"(?<![A-Za-z0-9])"+re.escape(keyword)+r"(?![A-Za-z0-9])",text,re.I))
    return len(re.findall(r"\s*".join(map(re.escape,keyword.split())),text,re.I))
