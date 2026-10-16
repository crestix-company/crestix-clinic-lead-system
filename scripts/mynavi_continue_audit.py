#!/usr/bin/env python3
"""Continue Mynavi CSV article audits safely. CSV-only. No Google browser scraping.

Sources: latest updated CSV plus optional previously verified clinic URL CSV.
Optional URL discovery: Brave Search API via BRAVE_SEARCH_API_KEY environment variable.
Run only when target-site automated access is permitted. Never bypass CAPTCHA.
"""
import argparse
import asyncio
import csv
import os
import re
import time
import unicodedata
import urllib.parse
import urllib.request
import urllib.error
from collections import Counter
from pathlib import Path
from playwright.async_api import async_playwright
from mynavi_article_playwright_canary import inspect

COL_ID="clinic_id"
COL_NAME="クリニック名"
COL_ADDR="住所"
COL_STATUS="掲載判定（記入欄）"
COL_ARTICLE="実掲載記事URL（記入欄）"
COL_CLINIC="医院詳細URL（参考）"
URL_RE=re.compile(r"^https://clinic\\.mynavi\\.jp/clinic/[A-Za-z0-9]+/?$")
URL_FIND=re.compile(r"https?://clinic\\.mynavi\\.jp/clinic/[A-Za-z0-9]+/?")
REMOVE=re.compile(r"(?:医療法人社団|医療法人財団|医療法人|一般社団法人|公益社団法人)")
def normalize(s):
    return re.sub(r"[\\s\\-ー・,.、。()（）　]", "", unicodedata.normalize("NFKC",s or "").lower())
def simple_name(s):
    return REMOVE.sub("",s).strip()
def pref_city(address):
    p=re.match(r"^(北海道|東京都|大阪府|京都府|.{2,3}県)",address)
    if not p:return ("","")
    rest=address[p.end():]
    c=re.match(r"^(.{1,18}?[市区町村])",rest)
    return p.group(), (c.group(1) if c else "")
def load(path):
    with open(path,encoding="utf-8-sig",newline="") as f:
        reader=csv.DictReader(f)
        fields=reader.fieldnames
        rows=list(reader)
    for field in (COL_ID,COL_NAME,COL_ADDR,COL_STATUS,COL_ARTICLE,COL_CLINIC):
        if field not in fields: raise ValueError("missing column: "+field)
    return fields,rows
def read_known(paths):
    known={}
    for p in paths:
        if not p.exists(): continue
        _,rows=load(p)
        for row in rows:
            url=row[COL_CLINIC].strip()
            if URL_RE.fullmatch(url):
                known.setdefault(row[COL_ID],set()).add(url)
    return known
def search_api(name,address,key):
    pref,city=pref_city(address)
    query="site:clinic.mynavi.jp/clinic/ "+simple_name(name)+" "+pref+" "+city
    url="https://api.search.brave.com/res/v1/web/search?"+urllib.parse.urlencode({"q":query,"country":"jp","search_lang":"jp","count":10})
    req=urllib.request.Request(url,headers={"Accept":"application/json","X-Subscription-Token":key,"User-Agent":"Crestix-Mynavi-Canary/1.0"})
    import json
    with urllib.request.urlopen(req,timeout=20) as r:
        payload=json.load(r)
    if payload.get("error"): raise RuntimeError("Brave API: "+str(payload["error"]))
    return list(dict.fromkeys(
        match.group().rstrip("/")+"/"
        for entry in payload.get("web",{}).get("results",[])
        for match in URL_FIND.finditer(entry.get("url",""))
    ))[:5]
async def verify_candidate(page,url,name,address):
    try:
        response=await page.goto(url,wait_until="domcontentloaded",timeout=30000)
        if not response or response.status != 200:return False
        body=normalize((await page.locator("body").inner_text(timeout=15000))[:15000])
        heading=normalize(await page.title())
        pref,city=pref_city(address)
        # Strict checks: title name and prefecture+municipality in body.
        return bool(simple_name(name) and normalize(simple_name(name)) in heading and pref and city and normalize(pref) in body and normalize(city) in body)
    except Exception:
        return False
async def main(args):
    fields,rows=load(Path(args.input))
    known=read_known([Path(p) for p in args.reference])
    audit=[]
    key=os.getenv("BRAVE_SEARCH_API_KEY","").strip()
    processed=0
    api_calls=0
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=not args.headed)
        page=await browser.new_page(locale="ja-JP")
        for row in rows:
            if row[COL_STATUS].strip()!="未確認":continue
            if args.limit and processed>=args.limit:break
            processed+=1
            cid=row[COL_ID]
            original_url=row[COL_CLINIC].strip()
            urls=[original_url] if URL_RE.fullmatch(original_url) else sorted(known.get(cid,[]))
            source="existing URL" if original_url else "reference CSV"
            if not urls and key and api_calls < args.max_api_calls:
                api_calls+=1
                try:
                    urls=await asyncio.to_thread(search_api,row[COL_NAME],row[COL_ADDR],key)
                    source="Brave API"
                except urllib.error.HTTPError as e:
                    audit.append([cid,row[COL_NAME],"確認待ち",f"Brave HTTP {e.code}",""])
                    if e.code in (401,403,429):
                        print(f"Brave HTTP {e.code}; stopping to avoid wasted searches",flush=True)
                        break
                    continue
                except Exception as e:
                    audit.append([cid,row[COL_NAME],"確認待ち","検索APIエラー: "+type(e).__name__,""])
                    continue
            if not urls:
                audit.append([cid,row[COL_NAME],"未確認","URL候補なし（検索APIキー未設定または候補なし）",""])
                continue
            valid=[]
            for url in urls:
                if URL_RE.fullmatch(url) and await verify_candidate(page,url,row[COL_NAME],row[COL_ADDR]):
                    valid.append(url)
                await asyncio.sleep(args.delay)
            if len(valid)!=1:
                audit.append([cid,row[COL_NAME],"確認待ち","医院名・都道府県・市区町村照合不成立または複数候補", " | ".join(urls)])
                continue
            url=valid[0]
            result=await inspect(page,{COL_ID:cid,COL_NAME:row[COL_NAME],COL_ADDR:row[COL_ADDR],COL_CLINIC:url})
            status=result[COL_STATUS]
            # Search candidates: verify street address manually before concluding absence.
            if status=="掲載あり" and source in ("existing URL","reference CSV"):
                row[COL_STATUS]=status
                row[COL_ARTICLE]=result[COL_ARTICLE]
                row[COL_CLINIC]=url
                decision="更新"
            elif status=="掲載なしを確認" and source in ("existing URL","reference CSV"):
                row[COL_STATUS]=status
                row[COL_CLINIC]=url
                decision="更新"
            else:
                decision="確認待ち"
            audit.append([cid,row[COL_NAME],decision,source+" / "+result["確認理由"]+(" / 検索候補は住所番地の目視照合後に確定" if source=="Brave API" else ""),url])
            print(f"{processed}: {cid} {row[COL_NAME]} -> {decision} {status}",flush=True)
            await asyncio.sleep(args.delay)
        await browser.close()
    with open(args.output,"w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
    with open(args.audit,"w",encoding="utf-8-sig",newline="") as f:
        w=csv.writer(f);w.writerow(["clinic_id","クリニック名","処理結果","根拠・理由","医院詳細URL候補"]);w.writerows(audit)
    print("対象:",processed,"Brave検索回数:",api_calls,"処理内訳:",dict(Counter(r[2] for r in audit)))
    print("残り未確認:",sum(r[COL_STATUS].strip()=="未確認" for r in rows))
    print("出力:",args.output,"監査:",args.audit)
if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--input",required=True)
    ap.add_argument("--output",default="mynavi_328_updated_v2.csv")
    ap.add_argument("--audit",default="mynavi_328_audit_v2.csv")
    ap.add_argument("--reference",action="append",default=[],help="Prior CSV containing known clinic URLs; repeatable")
    ap.add_argument("--limit",type=int,default=10,help="0 = all unchecked")
    ap.add_argument("--delay",type=float,default=2.5)
    ap.add_argument("--headed",action="store_true")
    ap.add_argument("--max-api-calls",type=int,default=10,help="maximum Brave searches; protects remaining credits")
    asyncio.run(main(ap.parse_args()))
