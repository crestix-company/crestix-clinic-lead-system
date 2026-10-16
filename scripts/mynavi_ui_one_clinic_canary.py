#!/usr/bin/env python3
"""Replay the user's recorded Mynavi UI path for ONE clinic (canary only).

No Brave, no database access, no CSV mutation. Use only when site access is authorized.
Outputs a JSON report; absence of a heading is NOT proof of absence.
"""
import argparse
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

async def run(args):
    report={"clinic_name":args.name,"region":args.region,"prefecture":args.prefecture,
            "clinic_url":"","article_urls":[],"status":"確認待ち","reason":""}
    step="ブラウザ起動"
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=not args.headed)
        page=await browser.new_page(locale="ja-JP")
        try:
            step="トップページを開く"
            await page.goto("https://clinic.mynavi.jp/",wait_until="domcontentloaded",timeout=30000)
            step="エリア選択を開く"
            await page.get_by_text("エリアを選ぶ").first.click(timeout=15000)
            step="地域を選択"
            await page.get_by_text(args.region).nth(3).click(timeout=15000)
            step="都道府県を選択"
            await page.get_by_text(args.prefecture).nth(1).click(timeout=15000)
            step="エリア選択確定"
            await page.get_by_text("選択条件で決定").click(timeout=15000)
            search=page.get_by_role("main").get_by_role("textbox",name="キーワード検索")
            step="医院名入力"
            await search.fill(args.name,timeout=15000)
            step="検索実行"
            await page.get_by_role("main").get_by_role("button",name="検索する").click(timeout=15000)
            await page.wait_for_load_state("domcontentloaded",timeout=15000)
            step="検索結果の確認"
            details=page.get_by_role("link",name="詳細情報を見る")
            count=await details.count()
            report["detail_result_count"]=count
            if count!=1:
                report["reason"]=f"検索結果に詳細情報リンクが{count}件。目視で候補を選ぶ必要あり"
            else:
                step="医院詳細を開く"
                await details.click(timeout=15000)
                await page.wait_for_load_state("domcontentloaded",timeout=15000)
                report["clinic_url"]=page.url
                report["page_title"]=await page.title()
                step="紹介記事欄を確認"
                headings=page.locator("h2,h3").filter(has_text="紹介記事")
                report["article_heading_count"]=await headings.count()
                if await headings.count():
                    urls=await headings.first.evaluate("""h => {
                      let n=h.parentElement;
                      for (let i=0;i<5 && n;i++,n=n.parentElement) {
                        const found=[...new Set([...n.querySelectorAll('a[href*="/article/"]')].map(a => new URL(a.getAttribute('href'),location.origin).href))];
                        if(found.length && found.length<=6) return found;
                        if(found.length>6) return [];
                      }
                      return [];
                    }""")
                    report["article_urls"]=[u for u in urls if u.startswith("https://clinic.mynavi.jp/article/")]
                    report["reason"]="紹介記事候補を取得（医院名・住所と記事内掲載は別途照合）" if report["article_urls"] else "紹介記事見出しあり、記事URL未取得"
                else:
                    report["reason"]="紹介記事見出しなし。これだけでは掲載なしと断定できない"
        except PlaywrightTimeoutError:
            report["reason"]="操作タイムアウト: "+step
        except Exception as e:
            report["reason"]="操作エラー ("+step+"): "+type(e).__name__+" "+str(e)[:300]
        finally:
            report["last_step"]=step
            try:
                report["current_url"]=page.url
                await page.screenshot(path="mynavi_ui_debug.png",full_page=True,timeout=5000)
                report["screenshot"]="mynavi_ui_debug.png"
            except Exception:
                pass
            await browser.close()
    target=Path(args.output)
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))
    print("保存先:",target)

if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--name",default="高安歯科クリニック")
    ap.add_argument("--region",default="近畿")
    ap.add_argument("--prefecture",default="大阪府")
    ap.add_argument("--output",default="mynavi_ui_canary_1.json")
    ap.add_argument("--headed",action="store_true")
    asyncio.run(run(ap.parse_args()))
