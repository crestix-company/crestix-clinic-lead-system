from dataclasses import dataclass, asdict
from datetime import timedelta
from urllib.parse import urlparse
import hashlib
import json
import re
import time
import pandas as pd
import requests
from bs4 import BeautifulSoup
from src.utils.cache import read_cache, merge_cache
from src.utils.date_utils import parse_date, today_japan
from src.enrichment.doctor_license import is_true

EPARK_COLUMNS = ["url", "listing", "status", "reason", "source", "checked_at", "verified",
                 "features_json", "html_sha256", "rules_version"]


def canonical_epark_url(value):
    try:
        parsed = urlparse(str(value).strip())
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"epark.jp", "www.epark.jp"}:
            return ""
        if parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
            return ""
        match = re.fullmatch(r"/shopinfo/(hpl\d+)/?(?:[^?]*)", parsed.path)
        return f"https://epark.jp/shopinfo/{match[1]}/" if match else ""
    except ValueError:
        return ""


@dataclass
class EparkResult:
    url: str = ""
    listing: str = "不明"
    status: str = "不明"
    reason: str = "EPARK URLなし"
    source: str = ""
    checked_at: str = ""
    verified: str = ""
    features_json: str = "{}"
    html_sha256: str = ""
    rules_version: str = ""


class EparkChecker:
    def __init__(self, config, path=None, frame=None, session=None, as_of=None):
        self.config = config
        self.path = path
        base = read_cache(path, EPARK_COLUMNS) if path else pd.DataFrame(columns=EPARK_COLUMNS)
        self.frame = pd.concat([base, frame], ignore_index=True).fillna("").astype(str) if frame is not None else base
        self.frame = self.frame.reindex(columns=EPARK_COLUMNS, fill_value="")
        self.session = session or requests.Session()
        self.last_request = 0.
        self.request_count = 0
        self.host_blocked = False
        self.as_of = as_of or today_japan()

    def _cached(self, url):
        hits = self.frame[self.frame["url"].map(canonical_epark_url) == url]
        valid = []
        for _, row in hits.iterrows():
            checked = parse_date(row["checked_at"][:10])
            ttl = self.config["blocked_ttl_days"] if "403" in row["reason"] or "429" in row["reason"] else self.config["cache_ttl_days"]
            if checked and 0 <= (self.as_of - checked).days <= ttl:
                valid.append(row)
        if not valid:
            return None
        confirmed = [r for r in valid if is_true(r["verified"]) and r["status"] in {"課金済み", "無課金"}]
        if len({r["status"] for r in confirmed}) > 1:
            return EparkResult(url=url, reason="手動確認済み課金情報が競合")
        row = sorted(confirmed or valid, key=lambda r: r["checked_at"])[-1]
        if row["status"] != "不明" and not is_true(row["verified"]):
            if row["rules_version"] != self.config["rules_version"] or not self.config["validated_rules"]:
                return EparkResult(url=url, listing=row["listing"] or "不明", reason="課金情報が未検証。手動確認が必要")
        return EparkResult(**{k: row[k] for k in EPARK_COLUMNS})

    def inspect_html(self, url, html):
        raw = html.encode("utf-8") if isinstance(html, str) else html
        if len(raw) > self.config["max_html_bytes"]:
            return EparkResult(url=url, reason="HTMLサイズ上限")
        soup = BeautifulSoup(raw, "html.parser")
        text = soup.get_text(" ", strip=True)
        if soup.select('[id*="captcha"], [class*="captcha"]') or re.search(r"verify you are human|access denied|アクセスが制限|ロボットではない", text, re.I):
            return EparkResult(url=url, reason="CAPTCHA/アクセス制限。自動取得を停止", source="手動HTML",
                               checked_at=today_japan().isoformat())
        canonical = soup.select_one('link[rel="canonical"]')
        if canonical and canonical_epark_url(canonical.get("href", "")) != url:
            return EparkResult(url=url, reason="HTMLのcanonical URLが指定医院と不一致")
        features = {
            "reservation_link_count": sum(bool(re.search(r"予約|受付|reserve|reservation", a.get_text(" ") + str(a.get("href", "")), re.I)) for a in soup.select("a,button")),
            "image_count": len(soup.select("img")),
            "text_length": len(text),
            "json_ld_count": len(soup.select('script[type="application/ld+json"]')),
        }
        listing = "あり" if canonical or ("/shopinfo/hpl" in str(soup) and "EPARK" in str(soup)) else "不明"
        matched = []
        for rule in self.config["validated_rules"]:
            if soup.select(rule["selector"]):
                matched.append(rule)
        labels = {r["label"] for r in matched}
        if len(labels) == 1:
            status = next(iter(labels))
            if status not in {"課金済み", "無課金"}:
                raise ValueError("EPARKルールのlabelは課金済み/無課金にしてください。")
            reason = "検証済みHTMLルールによる推定: " + " / ".join(r["reason"] for r in matched)
        else:
            status = "不明"
            reason = "課金ルールが競合" if labels else "予約導線・写真等は抽出済み。課金専用要素は未検証"
        return EparkResult(url, listing, status, reason, "保存HTML", today_japan().isoformat(), "",
                           json.dumps(features, ensure_ascii=False), hashlib.sha256(raw).hexdigest(), self.config["rules_version"])

    def check(self, value, html=None, allow_network=False):
        url = canonical_epark_url(value)
        if not url:
            return EparkResult(reason="EPARK URLなし" if not value else "EPARK URL形式が不正")
        cached = self._cached(url)
        if cached and (html is None or is_true(cached.verified)):
            return cached
        if html is not None:
            result = self.inspect_html(url, html)
        elif allow_network:
            result = self._fetch(url)
        else:
            return EparkResult(url=url, reason="URLあり。HTML/課金確認マスタは未取得")
        if result.checked_at:
            frame = pd.DataFrame([asdict(result)])
            self.frame = pd.concat([self.frame, frame], ignore_index=True)
            if self.path:
                merge_cache(self.path, frame, EPARK_COLUMNS, keys=["url", "source"])
        return result

    def _fetch(self, url):
        if self.host_blocked:
            return EparkResult(url=url, reason="同一実行中のアクセス制限により以降の取得を停止")
        if self.request_count >= self.config["max_requests_per_run"]:
            return EparkResult(url=url, reason="1回の取得上限。保存HTMLで補完可能")
        wait = self.config["minimum_interval_seconds"] - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(min(wait, 10))
        self.last_request, self.request_count = time.monotonic(), self.request_count + 1
        try:
            response = self.session.get(url, timeout=self.config["timeout_seconds"], allow_redirects=False,
                                        stream=True, headers={"User-Agent": "ClinicListFilter/0.1"})
            with response:
                if response.status_code in {401, 403, 429}:
                    self.host_blocked = True
                if response.status_code != 200:
                    return EparkResult(url=url, reason=f"HTTP {response.status_code}。再試行せず手動確認",
                                       source="HTTP", checked_at=today_japan().isoformat())
                if "html" not in response.headers.get("Content-Type", "").lower():
                    return EparkResult(url=url, reason="HTML以外の応答")
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > self.config["max_html_bytes"]:
                        return EparkResult(url=url, reason="HTMLサイズ上限")
                    chunks.append(chunk)
                result = self.inspect_html(url, b"".join(chunks))
                if "CAPTCHA" in result.reason:
                    self.host_blocked = True
                result.source = "HTTP"
                return result
        except requests.RequestException:
            return EparkResult(url=url, reason="接続/タイムアウト。手動HTMLまたは確認マスタで補完",
                               source="HTTP", checked_at=today_japan().isoformat())
