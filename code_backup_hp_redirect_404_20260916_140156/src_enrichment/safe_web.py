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
        except (OSError,ssl.SSLError,http.client.HTTPException,ValueError):
            raise WebError("接続・SSL・タイムアウトのエラーです。") from None
        finally:
            conn.close()


class SafeFetcher:
    def __init__(self,transport=None,timeout=10,max_bytes=2_000_000,interval=.6,sleeper=time.sleep):
        self.transport = transport or PinnedTransport()
        self.timeout,self.max_bytes,self.interval,self.sleep = timeout,max_bytes,interval,sleeper
        self.last,self.robots,self.blocked,self.delays = {},{},set(),{}

    def _get(self,url,allowed_host=None,plain=False):
        for _ in range(5):
            validate_url(url,resolve=False)
            h = host(url)
            if h in self.blocked:
                raise WebError("アクセス制限があるため、このサイトの取得を停止しました。")
            if allowed_host and h!=allowed_host:
                raise WebError("別ドメインへのリダイレクトを停止しました。")
            wait = max(self.interval,self.delays.get(h,0))-(time.monotonic()-self.last.get(h,0))
            if wait>0:
                self.sleep(wait)
            self.last[h] = time.monotonic()
            response = self.transport.get(url,self.timeout,self.max_bytes)
            if response.status in {401,403,429}:
                self.blocked.add(h)
            if response.status in {301,302,303,307,308}:
                if not response.headers.get("location"):
                    raise WebError("移転先URLが不明です。")
                url = urljoin(url,response.headers["location"])
                validate_url(url,resolve=False)
                if allowed_host and host(url)!=allowed_host:
                    raise WebError("別ドメインへのリダイレクトを停止しました。")
                if not plain and not self.allowed(url):
                    raise WebError("移転先のrobots.txtで取得が許可されていません。")
                continue
            if response.status==404 and plain:
                return url,""
            if response.status!=200:
                raise WebError(f"HTTP {response.status}。手動で確認してください。")
            if len(response.body)>self.max_bytes:
                raise WebError("HTMLサイズが上限を超えました。")
            content_type = response.headers.get("content-type", "").lower()
            if not plain and "html" not in content_type:
                raise WebError("HTMLページではありません。")
            encoding = re.search(r"charset=([\w-]+)",content_type)
            dammit = UnicodeDammit(response.body,known_definite_encodings=[encoding[1]] if encoding else [],is_html=not plain)
            html = dammit.unicode_markup or ""
            if not plain and re.search(r"verify you are human|checking your browser|cf-chl-|just a moment\.\.\.|アクセスが制限",html,re.I):
                self.blocked.add(h)
                raise WebError("アクセス確認画面のため取得できません。")
            return url,html
        raise WebError("リダイレクト回数の上限です。")

    def allowed(self,url):
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self.robots:
            _,text = self._get(origin+"/robots.txt",allowed_host=host(url),plain=True)
            parser = RobotFileParser()
            parser.parse(text.splitlines())
            self.robots[origin] = parser
            delay = parser.crawl_delay(USER_AGENT) or parser.crawl_delay("*") or 0
            if delay>60:
                self.blocked.add(host(url))
                raise WebError("サイトの取得間隔が長いため、手動確認をご利用ください。")
            self.delays[host(url)] = delay
        return self.robots[origin].can_fetch(USER_AGENT,url)

    def fetch(self,url,allowed_host=None):
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
                final,html = self._get(candidate,allowed_host)
                if final!=candidate and not self.allowed(final):
                    raise WebError("移転先のrobots.txtで取得が許可されていません。")
                return Page(final,html)
            except WebError as exc:
                last_error = exc
                # HTTPS優先候補が接続できない場合だけ、元のHTTPを試す。
                if candidate == candidates[-1]:
                    raise
        raise last_error or WebError("ページを取得できません。")


PRIORITY = re.compile(r"内視鏡|白内障|緑内障|治療|手術|検査|料金|費用|院長|医院紹介|診療|自費|予約|doctor|staff|profile|treatment|endoscop|cataract|glaucoma|medical|price|fee|access|about|clinic|lp/",re.I)
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
