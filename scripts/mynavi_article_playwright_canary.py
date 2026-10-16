#!/usr/bin/env python3
"""Mynavi article presence canary. CSV-only: never writes to the CRM/production DB.

Usage:
  python -m pip install playwright
  python -m playwright install chromium
  python scripts/mynavi_article_playwright_canary.py --input input.csv --output result.csv --headed

Input columns: clinic_id,住所,クリニック名,医院詳細URL（参考）
Also accepts id/address/clinic_name/clinic_url, and similar headers.
Only confirmed clinic URLs are checked. Missing clinic URLs remain 確認待ち.
Run only when automated access is authorized by the website operator.
"""
import argparse
import asyncio
import csv
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

ARTICLE = re.compile(r"^https://clinic\.mynavi\.jp/article/[^?#]+/?$")
CLINIC = re.compile(r"^https://clinic\.mynavi\.jp/clinic/[^?#]+/?$")

def cell(row, *keys):
    for key in keys:
        if row.get(key):
            return str(row[key]).strip()
    return ""

async def inspect(page, row):
    cid = cell(row, "clinic_id", "id")
    name = cell(row, "クリニック名", "clinic_name")
    address = cell(row, "住所", "address")
    clinic_url = cell(row, "医院詳細URL（参考）", "医院詳細URL", "clinic_url")
    out = {"clinic_id": cid, "住所": address, "クリニック名": name,
           "掲載判定（記入欄）": "確認待ち", "実掲載記事URL（記入欄）": "",
           "医院詳細URL（参考）": clinic_url, "確認理由": "", "確認日時": datetime.now().astimezone().isoformat(timespec="seconds")}
    if not CLINIC.fullmatch(clinic_url):
        out["確認理由"] = "医院詳細URL未特定またはURL形式不正"
        return out
    try:
        response = await page.goto(clinic_url, wait_until="domcontentloaded", timeout=30000)
        if response is None or response.status != 200:
            out["確認理由"] = f"HTTP {response.status if response else 'no response'}"
            return out
        # Do not mistake the navigation anchor '紹介記事' for an actual article.
        headings = page.locator("h2,h3").filter(has_text=re.compile("紹介記事"))
        section = None
        for i in range(await headings.count()):
            candidate = headings.nth(i)
            title = (await candidate.inner_text()).strip()
            if "紹介記事" in title and ("「" in title or name in title):
                section = candidate
                break
        if section is None:
            body = await page.locator("body").inner_text()
            if len(body) < 150:
                out["確認理由"] = "ページ本文不足"
            elif "アクセス" in body and "診療時間" in body:
                out["掲載判定（記入欄）"] = "掲載なしを確認"
                out["確認理由"] = "紹介記事見出しなし"
            else:
                out["確認理由"] = "構造未確認"
            return out
        # Links can live in nested wrappers, not immediate heading siblings.
        # Traverse document order between this section heading and the next
        # heading of equal or higher rank, excluding unrelated recommendations.
        # The article cards are nested beside the header, not necessarily its
        # direct siblings. Choose the nearest bounded container; never scan
        # unrelated page-wide recommendations.
        urls = await section.evaluate("""h => {
          let node = h.parentElement;
          for (let depth = 0; depth < 5 && node; depth++, node = node.parentElement) {
            const links = [...node.querySelectorAll('a[href*="/article/"]')]
              .map(a => new URL(a.getAttribute('href'), location.origin).href);
            const unique = [...new Set(links)];
            if (unique.length >= 1 && unique.length <= 6) return unique;
            if (unique.length > 6) return [];
          }
          return [];
        }""")
        seen = list(dict.fromkeys(u.split("?")[0] for u in urls if ARTICLE.fullmatch(u.split("?")[0])))
        if seen:
            out["掲載判定（記入欄）"] = "掲載あり"
            out["実掲載記事URL（記入欄）"] = " | ".join(seen)
            out["確認理由"] = f"紹介記事見出し内に{len(seen)}記事"
        else:
            out["確認理由"] = "紹介記事見出しあり・記事URL未取得（要目視）"
    except PlaywrightTimeoutError:
        out["確認理由"] = "タイムアウト"
    except Exception as exc:
        out["確認理由"] = "取得エラー: " + type(exc).__name__
    return out

async def run(args):
    with open(args.input, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))[:args.limit]
    outputs = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not args.headed)
        page = await browser.new_page(locale="ja-JP")
        for i, row in enumerate(rows, 1):
            result = await inspect(page, row)
            outputs.append(result)
            print(f"{i}/{len(rows)} {result['clinic_id']} {result['掲載判定（記入欄）']} {result['確認理由']}", flush=True)
            if i < len(rows):
                await asyncio.sleep(args.delay)
        await browser.close()
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(outputs[0].keys()) if outputs else ["clinic_id"])
        writer.writeheader()
        writer.writerows(outputs)
    print(f"saved: {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", default="mynavi_canary_result.csv")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--delay", type=float, default=3.0, help="seconds between checks")
    parser.add_argument("--headed", action="store_true", help="show browser window")
    asyncio.run(run(parser.parse_args()))
