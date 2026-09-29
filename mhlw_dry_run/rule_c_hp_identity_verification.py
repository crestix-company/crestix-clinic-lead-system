"""Rule C(UNIQUE_ADDRESS_MATCH)168件のみを対象にしたHP本人確認(Identity Verification)。

Phase 7(治療カテゴリResearch)ではない。目的はClinic MasterとMHLW(医療情報ネット)が
同一医院かどうかの確認のみ。治療カテゴリ・診療科はHPから取得しない(診療科の正はMHLWのまま)。

Production DBはREAD ONLY。書き込みは一切行わない。既存の本番fetch基盤
(src.enrichment.safe_web.SafeFetcher, src.enrichment.hp_analysis.identity/is_official_candidate)
をそのまま再利用する。
"""
import csv
import json
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
csv.field_size_limit(sys.maxsize)

from src.enrichment.safe_web import SafeFetcher, WebError  # noqa: E402
from src.enrichment.hp_analysis import is_official_candidate, identity, canonical_page, host  # noqa: E402
from src.normalizer.phone import normalize_phone  # noqa: E402
from difflib import SequenceMatcher  # noqa: E402
from mhlw_dry_run.rule_c_identity_rules import classify_identity, stale_or_move  # noqa: E402

BASE = Path(__file__).resolve().parent
DB_PATH = Path(__file__).resolve().parents[1] / "data" / "clinics.sqlite3"
FACILITY_FILES = [
    ("医科", "/Users/maekawahiroyuki/Downloads/02-1_clinic_facility_info_20260601.csv"),
    ("歯科", "/Users/maekawahiroyuki/Downloads/03-1_dental_facility_info_20260601.csv"),
]
OUT_CSV = BASE / "rule_c_hp_identity_audit.csv"
OUT_JSON = BASE / "rule_c_hp_identity_summary.json"


PHONE_RE = re.compile(r"(?<!\d)0\d[\d()\s\-－ー]{6,18}\d(?!\d)")


def log(*a):
    print(*a, file=sys.stderr)


# ---- 1. load Rule C 168 clinics from phase4_join_v2.csv ----
v2_rows = list(csv.DictReader(open(BASE / "phase4_join_v2.csv", encoding="utf-8", newline="")))
rule_c = [r for r in v2_rows if r["match_method"] == "UNIQUE_ADDRESS_MATCH"]
log(f"Rule C candidates: {len(rule_c)}")

# ---- 2. MHLW facility homepage lookup (only for the fids we need) ----
target_fids = {r["mhlw_facility_id"] for r in rule_c}
mhlw_hp = {}
mhlw_type = {}
for ftype, path in FACILITY_FILES:
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row["ID"] in target_fids:
                mhlw_hp[row["ID"]] = (row["案内用ホームページアドレス"] or "").strip()
                mhlw_type[row["ID"]] = ftype

mhlw_phone = {}
with open(BASE / "mhlw_department_master.csv", encoding="utf-8", newline="") as f:
    for row in csv.DictReader(f):
        fid = row["mhlw_facility_id"]
        if fid in target_fids and fid not in mhlw_phone:
            mhlw_phone[fid] = row.get("phone", "")

# ---- 3. legacy clinic HP/Maps fields (READ ONLY) ----
conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
conn.execute("PRAGMA query_only=ON")
conn.row_factory = sqlite3.Row
target_cids = {int(r["clinic_id"]) for r in rule_c}
legacy_hp = {}
for row in conn.execute("SELECT id, clinic_name, address, phone, medical_type, hp_url, maps_website_url, maps_presence_status, base_json FROM clinics"):
    if row["id"] not in target_cids:
        continue
    base = json.loads(row["base_json"]) if row["base_json"] else {}
    legacy_hp[str(row["id"])] = {
        "clinic_name": row["clinic_name"], "address": row["address"], "phone": row["phone"], "medical_type": row["medical_type"],
        "hp_url": row["hp_url"], "hp_candidate_url": base.get("hp_candidate_url", ""),
        "maps_website_url": row["maps_website_url"] if row["maps_presence_status"] == "MAPS_MATCHED_WEBSITE" else "",
    }
conn.close()
log(f"legacy HP fields loaded for {len(legacy_hp)} clinics")


def with_scheme(url):
    """MHLW原本にはスキーム省略のURL('example.co.jp'等)が約1割存在する。https://を補う。"""
    url = (url or "").strip()
    if url and "://" not in url:
        return "https://" + url
    return url


def candidate_urls(cid, fid):
    L = legacy_hp[cid]
    ordered = [with_scheme(L["maps_website_url"]), with_scheme(L["hp_url"] or L["hp_candidate_url"]), with_scheme(mhlw_hp.get(fid, ""))]
    return [u for u in ordered if u and is_official_candidate(u)]


def extract_hp_name(page):
    return (page.title or "").strip()[:120]


def extract_hp_address(text):
    m = re.search(r"(東京都|北海道|(?:京都|大阪)府|.{2,3}県)[^\s、。]{5,40}", text)
    return m.group(0)[:60] if m else ""


def extract_hp_phone(page, text):
    soup_phones = []
    try:
        from src.enrichment.hp_analysis import page_soup
        soup = page_soup(page)
        soup_phones = [a.get("href", "")[4:] for a in soup.select('a[href^="tel:"]')]
    except Exception:
        pass
    for raw in soup_phones:
        n = normalize_phone(raw)
        if n:
            return n
    m = PHONE_RE.search(text)
    return normalize_phone(m.group(0)) if m else ""


def get_canonical(page):
    try:
        from src.enrichment.hp_analysis import page_soup
        soup = page_soup(page)
        link = soup.find("link", rel="canonical")
        if link and link.get("href"):
            from urllib.parse import urljoin
            return urljoin(page.url, link["href"])
    except Exception:
        pass
    return canonical_page(page.url)


fetcher = SafeFetcher()
rows_out = []
status_counter = {}

for i, r in enumerate(rule_c, 1):
    started = time.monotonic()
    cid, fid = r["clinic_id"], r["mhlw_facility_id"]
    L = legacy_hp[cid]
    cm_record = {"clinic_name": L["clinic_name"], "address": L["address"], "phone": L["phone"]}
    mh_record = {"clinic_name": r["mhlw_clinic_name_raw"], "address": r["mhlw_address_raw"], "phone": mhlw_phone.get(fid, "")}
    urls = candidate_urls(cid, fid)
    sim = round(SequenceMatcher(None, r["comparison_clinic_name"], r["comparison_mhlw_name"]).ratio(), 3)
    common = {
        "clinic_id": cid, "mhlw_facility_id": fid, "clinic_name": L["clinic_name"],
        "mhlw_clinic_name": r["mhlw_clinic_name_raw"], "clinic_address": L["address"],
        "mhlw_address": r["mhlw_address_raw"], "clinic_phone": L["phone"],
        "mhlw_phone": mh_record["phone"], "maps_website_url": L["maps_website_url"],
        "hp_url": L["hp_url"] or L["hp_candidate_url"], "mhlw_homepage_url": mhlw_hp.get(fid, ""),
        "name_similarity": sim,
    }

    log(f"[{i}/{len(rule_c)}] clinic_id={cid} urls={len(urls)}")

    if not urls:
        rows_out.append({**common, "selected_identity_url": "", "hp_name": "", "hp_address": "", "hp_phone": "",
            "page_title": "", "canonical_url": "", "final_url": "", "final_domain": "", "address_match": False,
            "phone_match": False, "identity_status": "HP_UNAVAILABLE", "review_reason": "NO_CANDIDATE_URL",
            "fetch_status": "NO_URL", "processing_seconds": round(time.monotonic() - started, 3), "error": ""})
        status_counter["HP_UNAVAILABLE"] = status_counter.get("HP_UNAVAILABLE", 0) + 1
        continue

    page, used_url, fetch_error = None, "", ""
    for url in urls:
        try:
            page = fetcher.fetch(url)
            used_url = url
            break
        except WebError as exc:
            fetch_error = str(exc)
            continue

    if page is None:
        rows_out.append({**common, "selected_identity_url": urls[0], "hp_name": "", "hp_address": "", "hp_phone": "",
            "page_title": "", "canonical_url": "", "final_url": "", "final_domain": "", "address_match": False,
            "phone_match": False, "identity_status": "HP_UNAVAILABLE", "review_reason": "FETCH_FAILED",
            "fetch_status": "ERROR", "processing_seconds": round(time.monotonic() - started, 3), "error": fetch_error})
        status_counter["HP_UNAVAILABLE"] = status_counter.get("HP_UNAVAILABLE", 0) + 1
        continue

    text = unicodedata.normalize("NFKC", page.text)
    hp_name = extract_hp_name(page)
    hp_address = extract_hp_address(text)
    hp_phone = extract_hp_phone(page, text)
    final_url = page.url
    canon = get_canonical(page)
    domain = host(final_url)

    # 文字数の少なさだけではSTALE判定しない: JS描画中心の実在サイトは静的HTML取得だと
    # 本文がほぼ空になるため(SafeFetcherはJSを実行しない)、それを「閉鎖/パーキング」と
    # 誤判定してしまう(実データで確認・修正済み)。明示的なSTALE文言があるページのみSTALE扱いする。
    stale_kind = stale_or_move(page.text)

    cm_id = identity(cm_record, page)
    mh_id = identity(mh_record, page)
    name_ok_cm, addr_ok_cm, phone_ok_cm = cm_id["name_match"], cm_id["address_match"], cm_id["phone_match"]
    name_ok_mh, addr_ok_mh, phone_ok_mh = mh_id["name_match"], mh_id["address_match"], mh_id["phone_match"]

    # name_ok判定はpage.title+headingsのみを使う(identity()の仕様)ため、JS描画中心のサイトで
    # 本文(main_text/addr_ok/phone_ok)がほぼ空でも比較的頑健。Rule C対象は定義上raw名称が
    # 両側で異なるため、page上でその「異なる2つの名称」が両方とも見つかること自体が強い証拠になる。
    status, reason, name_changed = classify_identity(
        same_medical_type=L["medical_type"] == mhlw_type.get(fid), stale_kind=stale_kind,
        cm_name=name_ok_cm, mhlw_name=name_ok_mh, cm_address=addr_ok_cm, mhlw_address=addr_ok_mh,
        cm_phone=phone_ok_cm, mhlw_phone=phone_ok_mh)

    rows_out.append({**common, "selected_identity_url": used_url, "hp_name": hp_name, "hp_address": hp_address,
        "hp_phone": hp_phone, "page_title": page.title, "canonical_url": canon, "final_url": final_url,
        "final_domain": domain, "address_match": addr_ok_cm or addr_ok_mh, "phone_match": phone_ok_cm or phone_ok_mh,
        "identity_status": status, "review_reason": reason, "name_changed": name_changed, "fetch_status": "FETCHED",
        "processing_seconds": round(time.monotonic() - started, 3), "error": ""})
    status_counter[status] = status_counter.get(status, 0) + 1

fieldnames = ["clinic_id", "mhlw_facility_id", "clinic_name", "mhlw_clinic_name", "clinic_address", "mhlw_address",
              "clinic_phone", "mhlw_phone", "maps_website_url", "hp_url", "mhlw_homepage_url", "selected_identity_url",
              "hp_name", "hp_address", "hp_phone", "page_title", "canonical_url", "final_url", "final_domain",
              "name_similarity", "address_match", "phone_match", "identity_status", "review_reason", "name_changed",
              "fetch_status", "processing_seconds", "error"]
with open(OUT_CSV, "w", encoding="utf-8", newline="") as out:
    w = csv.DictWriter(out, fieldnames=fieldnames)
    w.writeheader()
    for row in rows_out:
        w.writerow(row)

summary = {"total": len(rule_c), "status_breakdown": status_counter,
           "promotable": status_counter.get("HP_IDENTITY_CONFIRMED", 0) + status_counter.get("HP_RENAME_CONFIRMED", 0)}
with open(OUT_JSON, "w", encoding="utf-8") as out:
    json.dump(summary, out, ensure_ascii=False, indent=2)
log(json.dumps(summary, ensure_ascii=False, indent=2))
