"""DNS検査後の公開IPへ接続を固定。HTTPSのSNI・証明書検証は元のホスト名。"""
from dataclasses import dataclass
from urllib.parse import urlsplit, urljoin, quote
from urllib.robotparser import RobotFileParser
import http.client
import socket
import ssl
import ipaddress
import time
import re
from bs4 import UnicodeDammit
from src.enrichment.hp_analysis import Page, canonical_page, host

USER_AGENT = "ClinicResearchBot/2.0 (+public clinic information; low rate)"


class WebError(Exception):
    """利用者にそのまま見せてよい、秘密を含まない取得エラー。"""


def _host_key(value):
    """www有無だけを同一サイトとして扱う。任意の別サブドメインは許可しない。"""
    text = str(value or "")
    h = host(text) if "://" in text else text.lower().rstrip(".")
    return h[4:] if h.startswith("www.") else h


def _same_site_host(a, b):
    return bool(_host_key(a) and _host_key(a) == _host_key(b))


def validate_url(url, resolve=True):
    try:
        p = urlsplit(url)
        hostname = (p.hostname or "").lower().rstrip(".")
        if p.scheme not in {"http","https"} or not hostname or p.username or p.password or p.port not in {None,80,443}:
            raise WebError("公開HTTP/HTTPSのURLを指定してください。")
        if any(c in url for c in "\r\n\t\\") or any(hostname==s or hostname.endswith("."+s) for s in ["localhost","local","internal"]):
            raise WebError("ローカルネットワークへのアクセスはできません。")
        try:
            ip = ipaddress.ip_address(hostname)
            if not ip.is_global:
                raise WebError("公開IP以外へのアクセスはできません。")
            addresses = [str(ip)]
        except ValueError:
            addresses = []
        if resolve:
            addresses = list(dict.fromkeys(a[4][0] for a in socket.getaddrinfo(hostname,p.port or (443 if p.scheme=="https" else 80),type=socket.SOCK_STREAM)))
            if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
                raise WebError("DNSの接続先が公開IPではありません。")
        return hostname,addresses
    except (ValueError,UnicodeError,socket.gaierror,OSError) as exc:
        raise WebError("URLまたはDNSを確認できません。") from None


@dataclass
class WebResponse:
    status: int
    headers: dict
    body: bytes


class PinnedTransport:
    def get(self,url,timeout,max_bytes):
        p = urlsplit(url)
        hostname,ips = validate_url(url)
        port = p.port or (443 if p.scheme=="https" else 80)
        cls = http.client.HTTPSConnection if p.scheme=="https" else http.client.HTTPConnection
        conn = cls(hostname,port=port,timeout=timeout)
        # HTTPConnection.connectとHTTPSConnection.connectの両方で使われる。
        conn._create_connection = lambda address,timeout,source_address=None: socket.create_connection((ips[0],port),timeout,source_address)
        deadline = time.monotonic()+timeout*2
        try:
            request_path = quote(p.path or "/",safe="/%:@!$&'()*+,;=-._~")
            if p.query:
                request_path += "?"+quote(p.query,safe="%=&;:+,/?@!$'()*-._~")
            conn.request("GET",request_path,
                         headers={"User-Agent":USER_AGENT,"Accept":"text/html,text/plain;q=0.9","Accept-Encoding":"identity"})
            response = conn.getresponse()
            headers = {k.lower():v for k,v in response.getheaders()}
            if response.status in {301,302,303,307,308}:
                return WebResponse(response.status,headers,b"")
            if int(headers.get("content-length","0") or 0)>max_bytes:
                raise WebError("HTMLサイズが上限を超えました。")
            if headers.get("content-encoding","").lower() not in {"","identity"}:
                raise WebError("未対応の圧縮形式です。手動で確認してください。")
            data = bytearray()
            while True:
                if time.monotonic()>deadline:
                    raise WebError("ページ取得が時間上限を超えました。")
                chunk = response.read(min(65536,max_bytes+1-len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data)>max_bytes:
                    raise WebError("HTMLサイズが上限を超えました。")
            return WebResponse(response.status,headers,bytes(data))
        except WebError:
            raise
        except ssl.SSLError:
            # Certificate/TLS validation failures are permanent for this host, not a
            # transient network blip -- keep this message distinct (no "タイムアウト"/
            # "HTTP 5xx" substring) so callers' retry-on-transient-error checks never
            # match it and retry a certificate problem.
            raise WebError("SSL証明書の検証に失敗しました。") from None
        except (OSError,http.client.HTTPException,ValueError):
            raise WebError("接続・SSL・タイムアウトのエラーです。") from None
        finally:
            conn.close()


MAX_RETRY_AFTER_SECONDS = 30  # 429のRetry-Afterがこれを超える場合は再試行せずFETCH_FAILEDとする
MAX_ROBOTS_REDIRECT_HOPS = 5  # RFC 9309 §2.3.1.2: robots.txt専用、別authorityへのredirectを許可


def _parse_retry_after(value):
    """Only the numeric-seconds form of Retry-After (the common case for rate
    limiting). The HTTP-date form is treated as "no usable wait time" -- safer
    to skip the retry than to mis-parse a date and wait the wrong amount."""
    if value is None:
        return None
    try:
        seconds = int(str(value).strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


class SafeFetcher:
    def __init__(self,transport=None,timeout=10,max_bytes=2_000_000,interval=.6,sleeper=time.sleep):
        self.transport = transport or PinnedTransport()
        self.timeout,self.max_bytes,self.interval,self.sleep = timeout,max_bytes,interval,sleeper
        self.last,self.robots,self.blocked,self.delays = {},{},set(),{}

    def _get(self,url,allowed_host=None,max_cross_domain_hops=0):
        """max_cross_domain_hops is 0 for every existing caller (support-link
        fetches, crawl() page fetches) -- identical behavior to before. Only the
        initial official-HP fetch in research_worker.py passes 1, to allow a
        single legitimate cross-domain redirect (a real domain migration)
        through to the caller for identity() verification -- see
        _check_or_consume_hop(). It is never applied inside crawl(). robots.txt
        fetching does not use this method at all -- see _fetch_robots_text(),
        which has its own, more permissive, cross-authority redirect policy
        (RFC 9309 §2.3.1.2) while applying the exact same validate_url/SSRF
        checks at every hop."""
        retried_rate_limit = False
        cross_domain_hops_used = 0

        def _check_or_consume_hop(target_host):
            nonlocal allowed_host, cross_domain_hops_used
            if not allowed_host or _same_site_host(target_host, allowed_host):
                return
            if cross_domain_hops_used < max_cross_domain_hops:
                cross_domain_hops_used += 1
                allowed_host = target_host
                return
            raise WebError("別ドメインへのリダイレクトを停止しました。")

        for _ in range(5):
            validate_url(url,resolve=False)
            h = host(url)
            if h in self.blocked:
                raise WebError("アクセス制限があるため、このサイトの取得を停止しました。")
            _check_or_consume_hop(h)
            wait = max(self.interval,self.delays.get(h,0))-(time.monotonic()-self.last.get(h,0))
            if wait>0:
                self.sleep(wait)
            self.last[h] = time.monotonic()
            response = self.transport.get(url,self.timeout,self.max_bytes)
            if response.status==429 and not retried_rate_limit:
                # Bounded, Retry-After-respecting single retry -- never hammer a
                # rate-limited host repeatedly. Missing or excessive Retry-After
                # means we cannot retry responsibly, so fall through to the normal
                # 429 handling below (blocked + WebError, no retry).
                retry_after = _parse_retry_after(response.headers.get("retry-after"))
                if retry_after is not None and retry_after <= MAX_RETRY_AFTER_SECONDS:
                    retried_rate_limit = True
                    self.sleep(retry_after)
                    continue
            if response.status in {401,403,429}:
                self.blocked.add(h)
            if response.status in {301,302,303,307,308}:
                if not response.headers.get("location"):
                    raise WebError("移転先URLが不明です。")
                url = urljoin(url,response.headers["location"])
                validate_url(url,resolve=False)
                _check_or_consume_hop(host(url))
                if not self.allowed(url):
                    raise WebError("移転先のrobots.txtで取得が許可されていません。")
                continue
            if response.status!=200:
                raise WebError(f"HTTP {response.status}。手動で確認してください。")
            if len(response.body)>self.max_bytes:
                raise WebError("HTMLサイズが上限を超えました。")
            content_type = response.headers.get("content-type", "").lower()
            if "html" not in content_type:
                raise WebError("HTMLページではありません。")
            encoding = re.search(r"charset=([\w-]+)",content_type)
            dammit = UnicodeDammit(response.body,known_definite_encodings=[encoding[1]] if encoding else [],is_html=True)
            html = dammit.unicode_markup or ""
            if re.search(r"verify you are human|checking your browser|cf-chl-|just a moment\.\.\.|アクセスが制限",html,re.I):
                self.blocked.add(h)
                raise WebError("アクセス確認画面のため取得できません。")
            return url,html
        raise WebError("リダイレクト回数の上限です。")

    def _fetch_robots_text(self, origin):
        """robots.txt-only redirect policy (RFC 9309 §2.3.1.2): unlike page
        fetches, a robots.txt redirect MAY cross to a different authority/host
        -- up to MAX_ROBOTS_REDIRECT_HOPS of them. Every hop still goes through
        the exact same validate_url() SSRF/public-IP/port/userinfo checks as
        any other fetch; this never weakens that.

        Returns the robots.txt text to apply under `origin`'s OWN rules (the
        text is parsed by the caller and cached under the ORIGINAL origin, not
        the redirect destination), "" if robots.txt is legitimately absent
        (RFC 9309: 404/4xx other than 429 -> unavailable -> no restrictions),
        or None if the result cannot be trusted -- callers MUST fail closed
        (block the fetch) on None, never silently treat it as "no
        restrictions". None covers: unsafe destination, network-unreachable,
        5xx, 429 (deliberately NOT treated like 404), a redirect loop beyond
        the hop budget, and a 2xx response that is HTML without any robots
        directive (e.g. a repurposed/expired domain's generic error page --
        real example seen in production: a clinic's robots.txt redirecting to
        a completely different business's live homepage)."""
        current = origin + "/robots.txt"
        for _ in range(MAX_ROBOTS_REDIRECT_HOPS + 1):
            try:
                # resolve=False here, matching _get()'s own per-hop convention: a
                # cheap syntactic/IP-literal/userinfo/port check at this loop
                # level. The real DNS-resolution-based public-IP check
                # (resolve=True) happens inside self.transport.get() itself --
                # PinnedTransport.get() calls validate_url(url) (default
                # resolve=True) before every real connection, so this is not
                # weakened for actual production traffic; it just keeps this
                # method testable with a stub transport the same way every
                # other SafeFetcher method already is.
                validate_url(current, resolve=False)
            except WebError:
                return None
            try:
                response = self.transport.get(current, self.timeout, self.max_bytes)
            except WebError:
                return None
            if response.status in {301,302,303,307,308} and response.headers.get("location"):
                current = urljoin(current, response.headers["location"])
                continue
            if response.status == 429:
                return None
            if 400 <= response.status < 500:
                return ""
            if response.status != 200:
                return None
            if len(response.body) > self.max_bytes:
                return None
            content_type = response.headers.get("content-type", "").lower()
            encoding = re.search(r"charset=([\w-]+)", content_type)
            dammit = UnicodeDammit(response.body, known_definite_encodings=[encoding[1]] if encoding else [], is_html=False)
            text = dammit.unicode_markup or ""
            is_html = "html" in content_type or bool(re.search(r"(?i)<html|<!doctype html", text))
            has_rep_directive = bool(re.search(r"(?im)^\s*(user-agent|allow|disallow|crawl-delay)\s*:", text))
            if is_html and not has_rep_directive:
                return None
            return text
        return None

    def allowed(self,url):
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self.robots:
            text = self._fetch_robots_text(origin)
            if text is None:
                raise WebError("robots.txtを安全に確認できないため取得を見送りました。")
            parser = RobotFileParser()
            parser.parse(text.splitlines())
            self.robots[origin] = parser
            delay = parser.crawl_delay(USER_AGENT) or parser.crawl_delay("*") or 0
            if delay>60:
                self.blocked.add(host(url))
                raise WebError("サイトの取得間隔が長いため、手動確認をご利用ください。")
            self.delays[host(url)] = delay
        return self.robots[origin].can_fetch(USER_AGENT,url)

    def fetch(self,url,allowed_host=None,max_cross_domain_hops=0):
        # 初回取得も別サイトへは追従しない。www有無だけ同一サイトとして許可する。
        # max_cross_domain_hops>0のときだけ例外的に、その回数まで別hostへの
        # redirectを許可する（呼び出し元が最終ページでidentity()検証する前提）。
        allowed_host = allowed_host or host(url)
        # Google Business Profile等が http:// を返しても、実サイトがHTTPS運用の
        # 場合がある。ブラウザ同様にHTTPSを先に試し、失敗時だけ元のHTTPへ戻す。
        p = urlsplit(url)
        candidates = [url]
        if p.scheme == "http" and p.port in {None,80}:
            https_netloc = p.hostname or ""
            https_url = p._replace(scheme="https",netloc=https_netloc).geturl()
            candidates = [https_url,url]

        last_error = None
        for candidate in candidates:
            try:
                if not self.allowed(candidate):
                    raise WebError("robots.txtで取得が許可されていません。")
                final,html = self._get(candidate,allowed_host,max_cross_domain_hops=max_cross_domain_hops)
                if final!=candidate and not self.allowed(final):
                    raise WebError("移転先のrobots.txtで取得が許可されていません。")
                return Page(final,html)
            except WebError as exc:
                last_error = exc
                # HTTPS優先候補が接続できない場合だけ、元のHTTPを試す。
                if candidate == candidates[-1]:
                    raise
        raise last_error or WebError("ページを取得できません。")


PRIORITY = re.compile(r"内視鏡|白内障|緑内障|治療|手術|施術|検査|処置|外来|料金|費用|院長|医師紹介|経歴|略歴|医院紹介|診療時間|受付時間|診療|自費|予約|doctor|staff|profile|career|schedule|hours|treatment|endoscop|cataract|glaucoma|medical|price|fee|access|about|clinic|lp/",re.I)
EXCLUDE = re.compile(r"\.(?:pdf|jpe?g|png|gif|webp|svg|ico|zip|mp4|mp3|css|js|xlsx?)(?:$|[?#])",re.I)


def crawl(first,fetcher,max_pages=20,should_stop=lambda:False):
    max_pages = min(30,max(1,int(max_pages)))
    pages,errors = [first],[]
    visited = {canonical_page(first.url)}
    queue = []
    def enqueue(page):
        for link in page.links:
            url = canonical_page(link["url"])
            if not url or host(url)!=host(first.url) or url in visited or EXCLUDE.search(url):
                continue
            visited.add(url)
            if len(visited)<=300:
                queue.append((0 if PRIORITY.search(link["text"]+url) else 1,url))
        queue.sort(key=lambda x:x[0])
    enqueue(first)
    attempts = 1
    while queue and attempts<max_pages and not should_stop():
        _,url = queue.pop(0)
        attempts += 1
        try:
            page = fetcher.fetch(url,allowed_host=host(first.url))
            if page.url not in {p.url for p in pages}:
                pages.append(page)
                enqueue(page)
        except WebError as exc:
            errors.append({"url":url,"reason":str(exc)})
    return pages,errors
