"""ネット接続もAPIキーも使わない、架空医院の一連のデモ。"""
from datetime import date
from pathlib import Path
import json
import html
from src.io.input_loader import load_table
from src.io.output_writer import csv_bytes
from src.master.filters import Filters
from src.master.jobs import create_job,run_job
from src.enrichment.hp_analysis import Page
from src.enrichment.safe_web import WebError


def sample_records():
    names = ["青空内視鏡クリニック","若葉眼科医院","日向皮膚科","月見内科"]
    year = date.today().year
    return [{"clinic_id":f"sample-{i+1}","clinic_name":name,"phone":f"03-0000-{i+1:04d}",
             "address":f"東京都千代田区架空町{i+1}-1-1","prefecture":"東京都","medical_type":"医科","facility_type":"診療所",
             "status":"現存","owner_name":f"見本 {i+1}郎","manager_name":f"見本 {i+1}郎","departments":"眼科" if i==1 else "消化器内科" if i==0 else "皮膚科" if i==2 else "内科",
             "designation_date":f"{year-3}-04-01","as_of":date.today().replace(day=1).isoformat(),"registration_reason":"新規","source_url":"架空サンプル"}
            for i,name in enumerate(names)]


class SampleProvider:
    def __init__(self):
        self.calls = 0

    def search(self,query):
        self.calls += 1
        for i,r in enumerate(sample_records()[:3]):
            if r["clinic_name"] in query:
                return [{"url":f"https://demo{i+1}.example/","title":r["clinic_name"],"content":r["phone"]+" "+r["address"]},
                        {"url":f"https://doctorsfile.jp/h/demo{i+1}/","title":r["clinic_name"],"content":r["phone"]+" "+r["address"]}]
        return []


class SampleFetcher:
    def fetch(self,url,allowed_host=None):
        for i,r in enumerate(sample_records()[:3]):
            if url.startswith(f"https://demo{i+1}.example/"):
                treatment = ["内視鏡","白内障手術","ニキビ治療"][i]
                h = f'''<html><head><title>{r['clinic_name']} | {treatment}</title><meta name="viewport" content="width=device-width"></head>
                <body><main><h1>{r['clinic_name']}</h1><p>{r['phone']} {r['address']}</p><h2>{treatment}</h2>
                <p>{treatment}の専門診療。{treatment}の料金について。</p>
                <h2>院長紹介</h2><p>院長 {r['manager_name']} 2005年 架空大学医学部卒業</p>
                <a href="/reserve/">Web予約</a><a href="tel:{r['phone']}">お問い合わせ</a>
                <a href="https://line.me/demo">LINE</a><a href="https://youtube.com/@demo{i+1}">公式YouTube</a>
                <img src="photo1.jpg"><img src="photo2.jpg"><img src="photo3.jpg">
                {'<script src="https://udify.app/embed.min.js"></script>' if i==0 else ''}</main></body></html>'''
                return Page(url,h)
        raise WebError("架空サンプル外のURLです。ネット接続は行っていません。")


def load_demo(store):
    if store.setting("sample_loaded",False) or store.count():
        return store.metrics()
    records = sample_records()
    headers = ["UUID","医院名","電話番号","住所","メモ",""]
    rows = [["DEMO-001",records[0]["clinic_name"],records[0]["phone"],records[0]["address"],"元データを保持",""]]
    table = load_table(csv_bytes(headers,rows),"demo.csv")
    store.import_comdesk(table)
    store.import_master(records)
    # サンプルは本番の課金カウンターを使わず、同じ取得/判定処理へfixtureを渡す。
    from src.enrichment.researcher import Researcher
    class OfflineSearch:
        def search(self,query,force=False):
            return SampleProvider().search(query)
    researcher = Researcher(OfflineSearch(),SampleFetcher(),max_pages=3)
    for record in store.query(limit=10):
        result,pages = researcher.hp(record)
        store.save_research(record["id"],result,pages)
    return store.metrics()
