"""Phase 1 dry analysis (read-only, no code/behavior change) of the 32
plain_retry_candidate clinics' robots.txt fetch behavior. Confirms/quantifies
the root cause reported by the user: SafeFetcher.allowed() applies the same
same-site restriction to the robots.txt sub-fetch as to the page itself, so a
site whose robots.txt happens to redirect cross-host (even though the PAGE
itself is same-host and fetches fine) is permanently blocked.

Every hop goes through validate_url(url, resolve=True) -- full SSRF/public-IP/
port/userinfo/DNS-resolution checks, exactly as production code requires.
Probing stops (UNSAFE_DESTINATION) the moment any hop fails that check.
"""
from __future__ import annotations

import csv
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from src.enrichment.hp_analysis import NON_OFFICIAL, domain_is, host
from src.enrichment.safe_web import PinnedTransport, WebError, validate_url

MANIFEST = Path("data/manifests/phase7-fullrun-514bd9972b3bf20e.csv")
REDIRECT_CSV = Path("data/research_worker/phase7-fullrun-514bd9972b3bf20e/redirect_analysis.csv")
OUT_CSV = Path("data/research_worker/phase7-fullrun-514bd9972b3bf20e/robots_redirect_analysis.csv")
OUT_JSON = OUT_CSV.with_suffix(".meta.json")

MAX_PROBE_HOPS = 10  # analysis only -- observe beyond the eventual 5-hop implementation cap
REP_DIRECTIVE_RE = re.compile(r"^\s*(user-agent|allow|disallow|crawl-delay)\s*:", re.I | re.M)
ERROR_PAGE_HINTS = (
    "error.html", "404", "not found", "page not found", "ページが見つかりません",
    "このページは存在しません", "domain for sale", "parked", "coming soon", "準備中",
)


def load_targets() -> list[dict]:
    redirect_rows = list(csv.DictReader(REDIRECT_CSV.open(encoding="utf-8-sig")))
    target_ids = {int(r["clinic_id"]) for r in redirect_rows if r["plain_retry_candidate"] == "true"}
    manifest_rows = {int(r["clinic_id"]): r for r in csv.DictReader(MANIFEST.open(encoding="utf-8-sig"))}
    return [manifest_rows[cid] for cid in sorted(target_ids) if cid in manifest_rows]


def probe_robots_chain(clinic_url: str) -> dict:
    p = urlsplit(clinic_url)
    origin = f"{p.scheme}://{p.netloc}"
    start_url = origin + "/robots.txt"
    transport = PinnedTransport()
    hops = []
    current = start_url
    final_status = None
    final_headers = {}
    final_body = ""
    unsafe = False
    unsafe_reason = ""
    for _ in range(MAX_PROBE_HOPS):
        try:
            validate_url(current, resolve=True)
        except WebError as exc:
            unsafe = True
            unsafe_reason = str(exc)
            hops.append({"url": current, "host": host(current), "status": None, "error": str(exc)})
            break
        try:
            response = transport.get(current, timeout=10, max_bytes=2_000_000)
        except WebError as exc:
            hops.append({"url": current, "host": host(current), "status": None, "error": str(exc)})
            final_status = None
            break
        location = response.headers.get("location", "")
        hops.append({"url": current, "host": host(current), "status": response.status, "location": location})
        if response.status in {301, 302, 303, 307, 308} and location:
            current = urljoin(current, location)
            continue
        final_status = response.status
        final_headers = response.headers
        try:
            final_body = response.body.decode("utf-8", "ignore")
        except Exception:
            final_body = ""
        break
    else:
        pass  # exhausted MAX_PROBE_HOPS without resolving -> redirect loop

    return {
        "start_url": start_url, "hops": hops, "final_url": hops[-1]["url"] if hops else start_url,
        "final_status": final_status, "final_headers": final_headers, "final_body": final_body,
        "unsafe": unsafe, "unsafe_reason": unsafe_reason,
        "hop_count": len(hops),
        "resolved": final_status is not None or unsafe,
    }


def classify(clinic_url: str, probe: dict) -> tuple[str, str]:
    if probe["unsafe"]:
        return "UNSAFE_DESTINATION", probe["unsafe_reason"]
    if probe["hop_count"] >= MAX_PROBE_HOPS and probe["final_status"] is None:
        return "REDIRECT_LOOP", f"{MAX_PROBE_HOPS}hop以内に解決せず"
    if probe["final_status"] is None:
        return "OTHER", "取得失敗（接続エラー等）"
    status = probe["final_status"]
    if status == 429:
        return "OTHER", "429はRFC 9309上404と同一視しない（別扱い要）"
    if 500 <= status <= 599:
        return "ROBOTS_5XX", f"最終ステータス{status}"
    if status in (404, 410):
        return "ROBOTS_4XX", f"最終ステータス{status}"
    if 400 <= status <= 499:
        return "ROBOTS_4XX", f"最終ステータス{status}"
    if status != 200:
        return "OTHER", f"最終ステータス{status}"

    content_type = probe["final_headers"].get("content-type", "").lower()
    body = probe["final_body"]
    body_lower = body.lower()
    is_html = "html" in content_type or bool(re.search(r"<html|<!doctype html", body_lower))
    has_rep = bool(REP_DIRECTIVE_RE.search(body))
    looks_like_error_page = any(hint in body_lower for hint in ERROR_PAGE_HINTS) or "error.html" in probe["final_url"].lower()

    if is_html and (looks_like_error_page or not has_rep):
        return "HTML_ERROR_PAGE", f"2xxだがHTML（content-type={content_type!r}, REP directive={'あり' if has_rep else 'なし'}）"
    if has_rep:
        return "VALID_ROBOTS", "REP directiveを検出"
    if not body.strip():
        return "EMPTY_ROBOTS", "空ボディ"
    return "EMPTY_ROBOTS", "2xxだが有効なruleなし"


def main() -> int:
    targets = load_targets()
    print(f"plain_retry_candidate targets loaded: {len(targets)}")
    if len(targets) != 32:
        print(f"WARNING: expected 32, got {len(targets)}")

    rows_out = []
    category_counts: dict[str, int] = {}

    for i, row in enumerate(targets, start=1):
        clinic_id = int(row["clinic_id"])
        clinic_url = row["effective_official_hp_url"]
        probe = probe_robots_chain(clinic_url)
        category, reason = classify(clinic_url, probe)
        category_counts[category] = category_counts.get(category, 0) + 1

        final_host = host(probe["final_url"])
        unsafe_ip_hop = any(
            "ローカルネットワーク" in (h.get("error") or "") or "公開IP" in (h.get("error") or "")
            for h in probe["hops"]
        )
        rows_out.append({
            "clinic_id": clinic_id,
            "clinic_name": row.get("clinic_name", ""),
            "initial_clinic_url": clinic_url,
            "initial_robots_url": probe["start_url"],
            "hop_count": probe["hop_count"],
            "hops_json": json.dumps(probe["hops"], ensure_ascii=False),
            "final_url": probe["final_url"],
            "final_host": final_host,
            "final_status": probe["final_status"],
            "final_content_type": probe["final_headers"].get("content-type", ""),
            "final_body_head": probe["final_body"][:500].replace("\n", " ").replace("\r", ""),
            "has_rep_directive": REP_DIRECTIVE_RE.search(probe["final_body"]) is not None,
            "has_user_agent": bool(re.search(r"^\s*user-agent\s*:", probe["final_body"], re.I | re.M)),
            "has_disallow": bool(re.search(r"^\s*disallow\s*:", probe["final_body"], re.I | re.M)),
            "has_allow": bool(re.search(r"^\s*allow\s*:", probe["final_body"], re.I | re.M)),
            "has_crawl_delay": bool(re.search(r"^\s*crawl-delay\s*:", probe["final_body"], re.I | re.M)),
            "unsafe_destination": probe["unsafe"] or unsafe_ip_hop,
            "non_official_domain": any(domain_is(probe["final_url"], d) for d in NON_OFFICIAL),
            "category": category,
            "reason": reason,
        })
        if i % 10 == 0:
            print(f"...{i}/{len(targets)} probed", flush=True)
        time.sleep(0.3)

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    fields = ["clinic_id", "clinic_name", "initial_clinic_url", "initial_robots_url", "hop_count",
              "hops_json", "final_url", "final_host", "final_status", "final_content_type",
              "final_body_head", "has_rep_directive", "has_user_agent", "has_disallow", "has_allow",
              "has_crawl_delay", "unsafe_destination", "non_official_domain", "category", "reason"]
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows_out)

    meta = {"total": len(targets), "category_counts": category_counts}
    OUT_JSON.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("\n=== robots redirect category breakdown ===")
    for cat, count in sorted(category_counts.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {count}")
    print(f"\nWrote {OUT_CSV}\nWrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
