#!/usr/bin/env python3
"""Verify existing site-search candidates against source clinic data. Read-only audit."""
import argparse, asyncio, json, re, unicodedata
from pathlib import Path
from urllib.parse import urlparse
from playwright.async_api import async_playwright

def norm(s):
    s=unicodedata.normalize("NFKC",s or "").lower()
    return re.sub(r"[\\s　,，、。()（）\\-]", "", s)

def core_name(s):
    return re.sub(r"医療法人(?:社団|財団)?|一般社団法人|公益社団法人", "", norm(s))

async def inspect(page, url, name, address):
    d={"url":url,"matched":False,"article_urls":[],"reason":""}
    u=urlparse(url)
    if u.scheme!="https" or u.hostname!="clinic.mynavi.jp" or not re.fullmatch(r"/clinic/[A-Za-z0-9]+/?",u.path):
        d["reason"]="URL形式不正"; return d
    try:
        res=await page.goto(url,wait_until="domcontentloaded",timeout=30000)
        if not res or res.status!=200:
            d["reason"]="ページHTTP "+str(res.status if res else "不明");return d
        title=await page.title()
        body=(await page.locator("body").inner_text(timeout=15000))[:20000]
        d["page_title"]=title
        d["page_excerpt"]=body[:300]
        name_ok=core_name(name) in core_name(title) and bool(core_name(name))
        # Do not infer an exact address match from only municipality tokens.
        address_ok=norm(address) in norm(body) and bool(norm(address))
        d["name_matched"]=name_ok
        d["full_address_matched"]=address_ok
        d["matched"]=name_ok and address_ok
        if not d["matched"]:
            d["reason"]="医院名または住所全文が一致しないため保留";return d
        headings=page.locator("h2,h3").filter(has_text="紹介記事")
        d["article_heading_count"]=await headings.count()
        if await headings.count():
            links=await headings.first.evaluate("""h => {
              for(let p=h.parentElement, i=0;i<5 && p;p=p.parentElement,i++){
                let a=[...new Set([...p.querySelectorAll('a[href*="/article/"]')].map(x=>new URL(x.getAttribute('href'),location.origin).href))];
                if(a.length>=1 && a.length<=6)return a;
                if(a.length>6)return [];
              }
              return [];
            }""")
            d["article_urls"]=[x for x in links if x.startswith("https://clinic.mynavi.jp/article/")]
        d["reason"]="紹介記事URL候補あり（記事本文と医院同一性の追加確認が必要）" if d["article_urls"] else "医院一致・記事リンク未検出（掲載なしとは判定しない）"
    except Exception as e:
        d["reason"]="取得エラー: "+type(e).__name__
    return d

async def run(args):
    source=json.loads(Path(args.input).read_text(encoding="utf-8"))
    rows=source.get("results",[])
    if len(rows)!=1: raise ValueError("安全のため1医院の監査JSONのみ受け付けます")
    row=rows[0]; candidates=row.get("candidate_urls",[])
    if len(candidates)>10: raise ValueError("候補が10件を超えています")
    report={"clinic_id":row["clinic_id"],"clinic_name":row["clinic_name"],"address":row["address"],"status":"確認待ち","candidates":[]}
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=not args.headed)
        page=await browser.new_page(locale="ja-JP")
        for i,url in enumerate(candidates,1):
            data=await inspect(page,url,row["clinic_name"],row["address"])
            report["candidates"].append(data)
            print(f"{i}/{len(candidates)} name={data.get('name_matched',False)} full_address={data.get('full_address_matched',False)} articles={len(data['article_urls'])} {data['reason']}",flush=True)
            await asyncio.sleep(args.delay)
        await browser.close()
    matches=[c for c in report["candidates"] if c["matched"]]
    report["matching_clinic_count"]=len(matches)
    report["reason"]="一致候補1件・記事URL候補を検討" if len(matches)==1 else "医院特定保留（住所表記差異または候補重複）"
    target=Path(args.output)
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print("監査:",target,"一致候補:",len(matches))

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--input",default="mynavi_ui_audit_1.json")
    p.add_argument("--output",default="mynavi_ui_identity_audit_1.json")
    p.add_argument("--delay",type=float,default=2.5)
    p.add_argument("--headed",action="store_true")
    asyncio.run(run(p.parse_args()))
