#!/usr/bin/env python3
"""D列が未確認の行だけを調査。元CSVは変更しない。DB接続なし。
Prerequisites: pip install playwright && python -m playwright install chromium
Usage: python scripts/mynavi_fill_unchecked.py --input original.csv --output completed.csv
Only run with permission for automated access to the target services.
"""
import argparse, asyncio, csv, re, unicodedata
from urllib.parse import quote, urljoin, urlsplit
from playwright.async_api import async_playwright
from mynavi_article_playwright_canary import inspect

CLINIC_RE = re.compile(r"https://clinic\.mynavi\.jp/clinic/[a-zA-Z0-9]+/?")
def norm(s):
    return re.sub(r"[\s\u3000\-ー・.,、。()（）]", "", unicodedata.normalize("NFKC", s).lower())
def title_name(name):
    return re.sub(r"^(医療法人(?:社団|財団)?|医療法人社団|医療法人財団|一般社団法人)\s*", "", name).strip()
async def discover(page, row):
    name, address = row[2].strip(),row[1].strip()
    prefecture = re.match(r"^(東京都|北海道|(?:京都|大阪)府|.{2,3}県)", address)
    area = prefecture.group(1) if prefecture else ""
    query = 'site:clinic.mynavi.jp/clinic/ "' + title_name(name) + '" "' + area + '"'
    # Public search listing; never click sponsored results or solve CAPTCHAs.
    search_url = "https://www.google.com/search?q=" + quote(query)
    try:
        response=await page.goto(search_url,wait_until="domcontentloaded",timeout=30000)
        if not response or response.status != 200:
            return "", "検索HTTPエラー"
        anchors=await page.locator('a[href]').evaluate_all("""els => els.map(x => x.href)""")
        urls=list(dict.fromkeys(
            m.group(0).rstrip("/")+"/" for a in anchors for m in [CLINIC_RE.search(a)] if m
        ))[:5]
        if not urls:
            return "", "検索結果から医院詳細URLを取得できず"
        confirmed=[]
        for url in urls:
            response=await page.goto(url,wait_until="domcontentloaded",timeout=30000)
            if not response or response.status != 200: continue
            title=await page.title()
            text=await page.locator("body").inner_text(timeout=15000)
            # Strict name and city/prefecture corroboration, not name alone.
            n=norm(title_name(name))
            if n and n in norm(title) and (area and norm(area) in norm(text)):
                confirmed.append(url)
        if len(confirmed)==1: return confirmed[0],"医院名・都道府県照合（住所詳細は要確認）"
        return "", ("複数候補" if len(confirmed)>1 else "医院名・所在地の照合不成立")
    except Exception as ex:
        return "", "検索エラー:"+type(ex).__name__

async def main(args):
    with open(args.input,encoding="utf-8-sig",newline="") as fp:
        allrows=list(csv.reader(fp))
    if len(allrows[0])<6: raise ValueError("6列以上のCSVが必要です")
    count=0
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=not args.headed)
        page=await browser.new_page(locale="ja-JP")
        for ix,row in enumerate(allrows[1:],start=2):
            if len(row)<6:row.extend([""]*(6-len(row)))
            if row[3].strip()!="未確認": continue
            if args.limit and count>=args.limit:break
            count+=1
            # Existing clinic URL must be respected. Never overwrite it on uncertainty.
            url=row[5].strip()
            match_note=""
            if not url:
                url,match_note=await discover(page,row)
            if url:
                inp={"clinic_id":row[0],"住所":row[1],"クリニック名":row[2],
                     "医院詳細URL（参考）":url}
                result=await inspect(page,inp)
                # Only trust negative results when the page really belongs to the clinic.
                if result["掲載判定（記入欄）"]=="掲載あり":
                    row[3]="掲載あり";row[4]=result["実掲載記事URL（記入欄）"];row[5]=url
                elif result["掲載判定（記入欄）"]=="掲載なしを確認":
                    # Discovery only checks name+prefecture; do not assert absence without full address check
                    if row[5]:
                        row[3]="掲載なしを確認"
                    else:
                        match_note+=" / 掲載なし候補（住所詳細未照合）"
                        row[5]=url
                else:row[5]=url
                note=match_note+" / "+result["確認理由"]
            else:note=match_note
            if len(row)>6 and note:row[6]=(row[6]+" | " if row[6] else "")+"自動調査: "+note
            print(f"{count}: {row[0]} {row[2]} => {row[3]} | {note}",flush=True)
            if count%10==0:save(args.output,allrows)
            await asyncio.sleep(max(args.delay,1.0))
        await browser.close()
    save(args.output,allrows)
    print("saved",args.output,"checked",count)
def save(path,rows):
    with open(path,"w",encoding="utf-8-sig",newline="") as f:
        csv.writer(f).writerows(rows)
if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--input",required=True)
    ap.add_argument("--output",required=True)
    ap.add_argument("--limit",type=int,default=0)
    ap.add_argument("--delay",type=float,default=3)
    ap.add_argument("--headed",action="store_true")
    asyncio.run(main(ap.parse_args()))
