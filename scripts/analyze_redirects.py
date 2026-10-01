"""Dry analysis of the 123 REDIRECT-category FETCH_FAILED clinics (Step 1-5 of
the redirect-rescue investigation): read-only re-probing only, no retry/writes
anywhere. Reuses validate_url/PinnedTransport for SSRF/public-IP/port checks
(unchanged, not weakened) and applies its own self-contained robots.txt check
(see _robots_allows -- deliberately not SafeFetcher.allowed(), which couples
robots.txt fetching to the main page's cross-domain restriction).

The original FETCH_FAILED error message never recorded the redirect
destination (safe_web.py's WebError messages are generic, e.g. "別ドメインへ
のリダイレクトを停止しました。"), so the only way to classify these 123
clinics is to safely re-observe the actual redirect chain.
"""
from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from urllib.parse import urljoin

from src.enrichment.hp_analysis import NON_OFFICIAL, domain_is, host
from src.enrichment.safe_web import PinnedTransport, WebError, validate_url

MANIFEST = Path("data/manifests/phase7-fullrun-514bd9972b3bf20e.csv")
ANALYSIS_CSV = Path("data/research_worker/phase7-fullrun-514bd9972b3bf20e/fetch_failed_analysis.csv")
OUT_CSV = Path("data/research_worker/phase7-fullrun-514bd9972b3bf20e/redirect_analysis.csv")
OUT_JSON = OUT_CSV.with_suffix(".meta.json")

MAX_HOPS = 6
COMPOUND_JP_SLD = {"co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp", "gr.jp", "ed.jp", "lg.jp"}
PARKED_INDICATORS = (
    "ドメインは移転", "ドメインが見つかりません", "このドメインは売り", "domain for sale",
    "buy this domain", "this domain is parked", "default web page", "coming soon",
    "account has been suspended", "website has expired", "expired domain",
    "under construction", "準備中です", "このページは存在しません", "may be for sale",
    "is for sale", "inquire about this domain", "domain name registration",
)
# Known domain marketplace / parking-page hosts -- checking the hostname itself is far
# more reliable than sniffing page text, since these pages are usually mostly JS/CSS
# boilerplate with the actual "for sale" copy rendered client-side or far down the body.
PARKED_HOSTS = (
    "hugedomains.com", "sedo.com", "dan.com", "afternic.com", "above.com",
    "parkingcrew.net", "bodis.com", "parklogic.com", "godaddy.com", "namecheap.com",
    "domainnamesales.com", "uniregistry.com", "buydomains.com",
)
EXTERNAL_SERVICE_HOST_HINTS = (
    "reserva.be", "yoyaku", "curon", "hpki", "mapps", "recaire", "coubic", "square.site",
)


def _registrable_domain(h: str) -> str:
    """Approximate eTLD+1 for classification only (not a security boundary --
    SSRF/host checks remain validate_url/_same_site_host, unaffected)."""
    parts = h.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in COMPOUND_JP_SLD:
        return ".".join(parts[-3:])
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return h


def _strip_www(h: str) -> str:
    return h[4:] if h.startswith("www.") else h


def load_targets() -> list[dict]:
    analysis_rows = list(csv.DictReader(ANALYSIS_CSV.open(encoding="utf-8-sig")))
    redirect_ids = {int(r["clinic_id"]) for r in analysis_rows if r["category"] == "REDIRECT"}
    manifest_rows = {int(r["clinic_id"]): r for r in csv.DictReader(MANIFEST.open(encoding="utf-8-sig"))}
    targets = []
    for cid in sorted(redirect_ids):
        row = manifest_rows.get(cid)
        if row is None:
            continue
        targets.append(row)
    return targets


def _robots_allows(transport, url, cache):
    """Minimal, self-contained robots.txt check for diagnosis -- deliberately
    does NOT reuse SafeFetcher.allowed()/._get(), because those apply the
    cross-domain "allowed_host" restriction to the robots.txt sub-fetch itself
    (origin host vs. the host passed in), which is the wrong check here: a
    robots.txt that happens to be served from a different host/CDN than the
    page is unrelated to whether THIS page's redirect crosses domains. Any
    failure fetching/parsing robots.txt defaults to "allowed" (same effective
    behavior as a 404 robots.txt in the production fetcher)."""
    from urllib.parse import urlsplit
    from urllib.robotparser import RobotFileParser
    from src.enrichment.safe_web import USER_AGENT
    p = urlsplit(url)
    origin = f"{p.scheme}://{p.netloc}"
    if origin not in cache:
        try:
            validate_url(origin + "/robots.txt", resolve=False)
            response = transport.get(origin + "/robots.txt", timeout=10, max_bytes=2_000_000)
            text = response.body.decode("utf-8", "ignore") if response.status == 200 else ""
        except Exception:
            text = ""
        parser = RobotFileParser()
        parser.parse(text.splitlines())
        cache[origin] = parser
    return cache[origin].can_fetch(USER_AGENT, url)


def probe_chain(start_url: str) -> dict:
    """Bounded, safety-preserving redirect walk. Every hop goes through
    validate_url() (SSRF/public-IP/port/userinfo) and a robots.txt check.
    Returns the full hop list plus final resolved state -- never raises;
    errors are recorded, not thrown, since this is read-only diagnosis of
    already-attempted URLs."""
    transport = PinnedTransport()
    robots_cache = {}
    chain = []
    current = start_url
    final_status = None
    final_text = ""
    error = ""
    for _ in range(MAX_HOPS):
        try:
            validate_url(current, resolve=False)
        except WebError as exc:
            chain.append({"url": current, "status": None, "location": "", "error": str(exc)})
            error = str(exc)
            break
        if not _robots_allows(transport, current, robots_cache):
            chain.append({"url": current, "status": None, "location": "", "error": "robots_disallowed"})
            error = "robots_disallowed"
            break
        try:
            response = transport.get(current, timeout=10, max_bytes=2_000_000)
        except WebError as exc:
            chain.append({"url": current, "status": None, "location": "", "error": str(exc)})
            error = str(exc)
            break
        location = response.headers.get("location", "")
        chain.append({"url": current, "status": response.status, "location": location})
        if response.status in {301, 302, 303, 307, 308} and location:
            current = urljoin(current, location)
            continue
        final_status = response.status
        if response.status == 200:
            try:
                final_text = response.body.decode("utf-8", "ignore")
            except Exception:
                final_text = ""
        break
    final_url = chain[-1]["url"] if chain else start_url
    return {
        "chain": chain, "final_url": final_url, "final_status": final_status,
        "final_text_snippet": final_text[:4000], "error": error,
    }


def classify(original_url: str, probe: dict, record: dict) -> tuple[str, bool, str]:
    original_host = host(original_url)
    final_host = host(probe["final_url"])
    same_host = _strip_www(original_host) == _strip_www(final_host)
    same_registrable = _registrable_domain(_strip_www(original_host)) == _registrable_domain(_strip_www(final_host))

    if not final_host:
        return "OTHER", False, "リダイレクト先ホストを確認できず判定不能"

    # Host-based categories first -- these don't require a successful 200 fetch of
    # the destination (e.g. line.me often blocks the specific path via robots.txt,
    # but it is unambiguously an SNS destination regardless of what its robots.txt
    # allows us to read).
    if any(hint in final_host for hint in PARKED_HOSTS):
        return "PARKED_OR_DEAD", False, "既知のドメイン販売/パーキングサービスへ着地"
    if any(domain_is(probe["final_url"], d) for d in NON_OFFICIAL):
        return "EXTERNAL_SERVICE", False, "既知の外部ポータル/SNS/予約サービスドメイン"
    if any(hint in final_host for hint in EXTERNAL_SERVICE_HOST_HINTS):
        return "EXTERNAL_SERVICE", False, "予約/外部サービス系ホスト名パターンに一致"

    if same_host and original_host != final_host:
        return "WWW_VARIANT", False, "www有無のみの差異（本来cross-domainブロック対象外のはず、要目視確認）"
    if same_host:
        return "SAME_HOST", False, "同一host内（scheme変更やredirectループ等、既存same-site扱いで再試行可能のはず）"

    if probe["error"] and probe["final_status"] is None:
        return "OTHER", False, f"最終的に取得できず判定不能（{probe['error'][:60]}）"

    text_lower = probe["final_text_snippet"].lower()
    if any(ind in probe["final_text_snippet"] or ind in text_lower for ind in PARKED_INDICATORS):
        return "PARKED_OR_DEAD", False, "ドメイン販売・閉鎖・準備中ページの兆候"
    if probe["final_status"] not in (200, None):
        return "PARKED_OR_DEAD", False, f"最終ステータス{probe['final_status']}（200以外）"

    if same_registrable:
        return "SAME_REGISTRABLE_DOMAIN", True, "同一registrable domain内のsubdomain移動（identity検証前提で候補）"

    if probe["final_status"] == 200 and probe["final_text_snippet"]:
        return "LEGIT_DOMAIN_MIGRATION_CANDIDATE", True, "別domainだが200応答・本文取得済み、identity検証で確認要"

    return "OTHER", False, "判定不能"


def main() -> int:
    targets = load_targets()
    print(f"REDIRECT targets loaded: {len(targets)}")

    rows_out = []
    category_counts: dict[str, int] = {}
    needs_identity = []

    for i, row in enumerate(targets, start=1):
        clinic_id = int(row["clinic_id"])
        original_url = row["effective_official_hp_url"]
        probe = probe_chain(original_url)
        last_hop = probe["chain"][-1] if probe["chain"] else {}
        category, needs_identity_check, reason = classify(original_url, probe, row)
        category_counts[category] = category_counts.get(category, 0) + 1
        if needs_identity_check:
            needs_identity.append((clinic_id, probe["final_url"]))

        rows_out.append({
            "clinic_id": clinic_id,
            "clinic_name": row.get("clinic_name", ""),
            "original_url": original_url,
            "original_host": host(original_url),
            "redirect_status": probe["chain"][0]["status"] if probe["chain"] else "",
            "redirect_location": probe["chain"][0]["location"] if probe["chain"] else "",
            "destination_host": host(probe["final_url"]),
            "final_url": probe["final_url"],
            "final_status": probe["final_status"],
            "hop_count": len(probe["chain"]),
            "category": category,
            "safe_redirect_retry": "true" if needs_identity_check else "false",
            # SAME_HOST/WWW_VARIANT need no new rescue logic at all -- _same_site_host
            # already treats them as same-site, so the EXISTING fetch path already
            # follows them; these failed originally because of a redirect
            # loop/transient condition, not a cross-domain block. A plain requeue
            # (no code change) is the correct, minimal fix for these.
            "plain_retry_candidate": "true" if category in ("SAME_HOST", "WWW_VARIANT") else "false",
            "reason": reason,
            "last_error": probe["error"],
        })
        if i % 20 == 0:
            print(f"...{i}/{len(targets)} probed", flush=True)
        time.sleep(0.3)

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    fields = ["clinic_id", "clinic_name", "original_url", "original_host", "redirect_status",
               "redirect_location", "destination_host", "final_url", "final_status", "hop_count",
               "category", "safe_redirect_retry", "plain_retry_candidate", "reason", "last_error"]
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows_out)

    meta = {
        "total": len(targets), "category_counts": category_counts,
        "safe_redirect_retry_candidates": len(needs_identity),
        "safe_redirect_retry_clinic_ids": [cid for cid, _ in needs_identity],
    }
    OUT_JSON.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"\n=== category breakdown ===")
    for cat, count in sorted(category_counts.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {count}")
    print(f"\nLEGIT_DOMAIN_MIGRATION_CANDIDATE (needs identity verification): {len(needs_identity)}")
    print(f"\nWrote {OUT_CSV}\nWrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
