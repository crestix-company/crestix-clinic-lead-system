"""既存URLだけを使うWebサイト種別のoffline runtime分類。"""
import json
import sqlite3
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from src.master.hp_effective_rank import hp_batch_path


SITE_OFFICIAL = "OFFICIAL_VERIFIED"
SITE_PORTAL = "PORTAL"
SITE_OTHER = "OTHER_UNKNOWN"
SITE_TYPE_LABELS = {
    SITE_OFFICIAL: "公式HP確認済み",
    SITE_PORTAL: "ポータルサイト",
    SITE_OTHER: "その他・未確認",
}

# exact domainまたはsubdomainだけを一致させる。URL本文・医院名などから公式性は推測しない。
PORTAL_DOMAINS = {
    "doctorsfile.jp": "Doctors File",
    "hospita.jp": "Hospita",
    "byoinnavi.jp": "病院なび",
    "caloo.jp": "Caloo",
    "medicalnote.jp": "Medical Note",
    "qlife.jp": "QLife",
    "epark.jp": "EPARK",
    "medley.life": "MEDLEY",
    "scuel.me": "SCUEL",
    "tokyo-doctors.com": "東京ドクターズ",
    "e-doctors-net.com": "e-doctors",
    "kanja.jp": "患者さんのための医療機関検索",
    "iryou.teikyouseido.mhlw.go.jp": "医療情報ネット",
}


def portal_name_for_url(url):
    try:
        hostname = (urlparse(str(url or "").strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    for domain, name in PORTAL_DOMAINS.items():
        if hostname == domain or hostname.endswith("." + domain):
            return name
    return ""


def classify_site(hp_status, hp_url, source_url="", final_url=""):
    """ポータル判定を優先し、公式は既存VERIFIED根拠がある場合だけ返す。"""
    for url in (final_url, source_url, hp_url):
        portal = portal_name_for_url(url)
        if portal:
            return SITE_PORTAL, portal
    if str(hp_status or "").upper() == "VERIFIED" and str(hp_url or "").strip():
        return SITE_OFFICIAL, ""
    return SITE_OTHER, ""


@lru_cache(maxsize=8)
def _load_batch_urls_cached(path_string, mtime_ns, size):
    path = Path(path_string)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        return {
            int(cid): (source or "", final or "")
            for cid, source, final in conn.execute(
                "SELECT clinic_id,hp_url,final_url FROM hp_research_batch_results"
            )
        }


def load_batch_urls(path=None):
    path = Path(path or hp_batch_path())
    if not path.exists():
        return {}
    try:
        stat = path.stat()
        return _load_batch_urls_cached(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    except sqlite3.Error:
        return {}


@lru_cache(maxsize=8)
def _load_batch_treatments_cached(path_string, mtime_ns, size):
    path = Path(path_string)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        result = {}
        for cid, status, raw in conn.execute(
            "SELECT clinic_id,fetch_status,treatment_categories FROM hp_research_batch_results"
        ):
            if status != "OK":
                continue
            try:
                categories = json.loads(raw or "[]")
            except (TypeError, ValueError):
                categories = []
            result[int(cid)] = categories if isinstance(categories, list) else []
        return result


def load_batch_treatments(path=None):
    """完走済みbatchの治療カテゴリを表示用に読む。Production値は変更しない。"""
    path = Path(path or hp_batch_path())
    if not path.exists():
        return {}
    try:
        stat = path.stat()
        return _load_batch_treatments_cached(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    except sqlite3.Error:
        return {}
