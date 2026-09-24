"""1医院単位で取得・判定。HP/EPARK/外部媒体は独立したバッチ。"""
import re
import unicodedata
from urllib.parse import urlsplit
from src.enrichment.hp_analysis import Page,identity,is_official_candidate,host,canonical_page
from src.enrichment.safe_web import SafeFetcher,WebError,crawl
from src.enrichment.search_provider import BudgetReached
from src.enrichment.epark_checker import canonical_epark_url
from src.scoring.research_scoring import analyze,media_signals,dedupe_signals,hot_status
from src.master.store import now


class Stopped(Exception):
    pass


def query_for(record,suffix=""):
    name = str(record.get("clinic_name","")).replace('"'," ").strip()
    phone = re.sub(r"[^0-9]","",str(record.get("phone", "")))
    return f'"{name}" "{phone}" {suffix}'.strip() if phone else f'"{name}" {record.get("address","")} {suffix}'.strip()


def hp_fallback_query(record):
    # 正式法人名・旧電話番号で公式HPが見つからないときだけ、医院名＋所在地で1回補完。
    name = unicodedata.normalize("NFKC",str(record.get("clinic_name", ""))).replace('"'," ").strip()
    name = re.sub(r"^(?:(?:社会|特定)?医療法人(?:社団|財団)?)[\s　]*[^\s　]*?会[\s　]*", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return query_for({**record,"clinic_name":name,"phone":""},"公式ホームページ")


def maps_url_candidates(url):
    """Maps URLを最優先し、古いパス時だけ同一ホストのルートを次候補にする。"""
    if not url or not is_official_candidate(url):
        return []
    out = [url]
    try:
        p = urlsplit(url)
        if (p.path or "/") not in {"", "/"} or p.query or p.fragment:
            root = f"{p.scheme}://{p.netloc}/"
            if root not in out:
                out.append(root)
    except ValueError:
        pass
    return out


def retained_media_signals(record):
    # HPとは独立に確認した外部媒体の結果を、HP再調査で消さない。
    return dedupe_signals([s for s in (record or {}).get("marketing_signals",[]) if isinstance(s,dict) and
        s.get("evidence_type") in {"検索結果の医院名・電話/住所一致","Caloo Plus明記・医院本人確認","制作実績・医院本人確認"}])


def empty_hp_result(status,record=None):
    # 再調査で本人確認に失敗した場合、旧HP由来の自動判定を現行結果に残さない。
    # 元CSV・手動修正・EPARKの状態・過去の調査履歴は別管理のまま保持する。
    signals = retained_media_signals(record)
    return {"hp_status":status,"hp_verified":False,"hp_url":"","hp_checked_at":now(),
            "hp_rank":"NO_HP" if status=="NOT_FOUND" else "UNKNOWN","hp_score":0,"hp_rank_reasons":[],
            "hp_match_score":0,"hp_match_reason":[],"hp_identity_pages":[],"hp_candidates":[],"crawl_errors":[],
            "treatment_categories":[],"treatment_evidence":[],"treatment_confidence":{},
            "hp_production_companies":[],"hp_production_evidence":[],
            "marketing_signals":signals,"marketing_signal_count":len(signals),"hot_status":hot_status(len(signals)),"research_status":status}


class Researcher:
    def __init__(self,search,fetcher=None,max_pages=20,should_stop=lambda:False):
        self.search = search
        self.fetcher = fetcher or SafeFetcher()
        self.max_pages = max_pages
        self.should_stop = should_stop

    def checkpoint(self):
        if self.should_stop():
            raise Stopped()

    def hp(self,record,force=False):
        self.checkpoint()
        results = []
        candidates = []
        maps_url = record.get("maps_website_url") if record.get("maps_presence_status") == "MAPS_MATCHED_WEBSITE" else ""
        existing = record.get("hp_url") or record.get("hp_candidate_url")
        if maps_url and is_official_candidate(maps_url):
            candidates.extend(maps_url_candidates(maps_url))
        elif existing and is_official_candidate(existing):
            candidates.append(existing)
        failures,reviews,seen = [],[],set()
        # Mapsのウェブサイト欄がある医院はHP発見目的の検索APIを消費しない。
        queries = iter([] if maps_url else [query_for(record),hp_fallback_query(record)])
        while True:
            for url in candidates:
                self.checkpoint()
                key = canonical_page(url)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    page = self.fetcher.fetch(url)
                    check = identity(record,page)
                    if not is_official_candidate(page.url):
                        reviews.append({"url":page.url,**check})
                        continue
                    support = []
                    if not check["verified"]:
                        # トップで住所・電話が不足する医院は同一HPの概要・アクセスを最大2ページ確認。
                        links = [a["url"] for a in page.links if host(a["url"])==host(page.url) and re.search(r"アクセス|医院概要|医院紹介|contact|access|about",a["text"]+a["url"],re.I)]
                        for link in list(dict.fromkeys(links))[:2]:
                            self.checkpoint()
                            try:
                                support.append(self.fetcher.fetch(link,allowed_host=host(page.url)))
                            except WebError:
                                pass
                        if support:
                            check = identity(record,Page(page.url,page.html+"\n"+"\n".join(p.html for p in support)))
                    if not check["verified"]:
                        reviews.append({"url":page.url,**check})
                        continue
                    pages,errors = crawl(page,self.fetcher,self.max_pages,self.should_stop)
                    self.checkpoint()
                    # 途中停止なら完了扱いにせず、検索キャッシュを使って再開する。
                    for p in support:
                        if p.url not in {x.url for x in pages}:
                            pages.append(p)
                    result = analyze(record,pages,results)
                    signals = dedupe_signals(result["marketing_signals"]+retained_media_signals(record))
                    result.update(marketing_signals=signals,marketing_signal_count=len(signals),hot_status=hot_status(len(signals)))
                    result.update(hp_url=page.url,hp_status="VERIFIED",hp_verified=True,hp_match_score=check["score"],
                                  hp_match_reason=check["reasons"],hp_checked_at=now(),hp_identity_pages=[page.url]+[p.url for p in support],
                                  crawl_errors=errors,research_status="SUCCESS",research_error="",hp_candidates=reviews)
                    return result,[p.evidence() for p in pages]
                except WebError as exc:
                    failures.append({"url":url,"reason":str(exc)})
            query = next(queries,None)
            if query is None:
                break
            self.checkpoint()
            batch = self.search.search(query,force=force)
            results.extend(batch)
            candidates = list(dict.fromkeys(r.get("url","") for r in batch if is_official_candidate(r.get("url",""))))[:5]
        # Google Mapsで医院本人確認済みのウェブサイトURLは、HP側が
        # 403/429/robots.txt/アクセス確認等で自動取得を拒否してもURL自体を失わない。
        # アクセス制限を回避する処理は行わず、内容解析だけ要確認として残す。
        access_restricted = re.compile(
            r"アクセス制限|アクセス確認画面|HTTP\s*(?:401|403|429)|"
            r"robots\.txtで取得が許可|取得間隔が長",
            re.I,
        )
        if maps_url and failures and not reviews and all(
            access_restricted.search(str(item.get("reason", ""))) for item in failures
        ):
            signals = retained_media_signals(record)
            result = empty_hp_result("VERIFIED", record)
            result.update(
                hp_status="VERIFIED",
                hp_verified=True,
                hp_url=maps_url,
                hp_rank="UNKNOWN",
                hp_score=0,
                hp_match_reason=[
                    "Google Mapsで医院本人確認済みのウェブサイトURLです。"
                    "サイト側のアクセス制限によりHP内容の自動解析は未完了です。"
                ],
                hp_candidates=[],
                crawl_errors=failures,
                hp_checked_at=now(),
                hp_content_status="ACCESS_RESTRICTED",
                hp_content_note="サイト側のアクセス制限のため自動解析せず、手動確認対象として保持します。",
                research_status="REVIEW",
                research_error="",
                marketing_signals=signals,
                marketing_signal_count=len(signals),
                hot_status=hot_status(len(signals)),
            )
            return result, []

        status = "REVIEW" if reviews else "ERROR" if failures else "NOT_FOUND"
        signals = dedupe_signals(media_signals(record,results)+retained_media_signals(record))
        return {**empty_hp_result(status,record),"hp_candidates":reviews,"crawl_errors":failures,
                "hp_match_reason":["検索した範囲で公式HPを発見できませんでした。" if status=="NOT_FOUND" else "候補ページの本人確認・取得を完了できませんでした。"],
                "marketing_signals":signals,"marketing_signal_count":len(signals),"hot_status":hot_status(len(signals)),"research_error":""},[]

    def epark(self,record,force=False):
        self.checkpoint()
        results = self.search.search(query_for(record,"site:epark.jp"),force=force)
        urls = list(dict.fromkeys(canonical_epark_url(r.get("url","")) for r in results))
        urls = [u for u in urls if u]
        if not urls:
            # 電話による検索で出なかった場合だけ住所で補完。
            self.checkpoint()
            name = str(record.get("clinic_name","")).replace('"',' ')
            results2 = self.search.search(f'"{name}" {record.get("address","")} site:epark.jp',force=force)
            results += results2
            urls = list(dict.fromkeys(canonical_epark_url(r.get("url","")) for r in results2))
            urls = [u for u in urls if u]
        verified,issues = [],[]
        for url in urls[:3]:
            self.checkpoint()
            try:
                page = self.fetcher.fetch(url)
                if not canonical_epark_url(page.url):
                    raise WebError("EPARK以外の移転先です。")
                check = identity(record,page,official=False)
                if check["verified"]:
                    verified.append((url,check))
                else:
                    issues.append({"url":url,"reason":"医院の本人確認不足"})
            except WebError as exc:
                # 取得制限時は検索結果だけで確定せず候補として保存。
                issues.append({"url":url,"reason":str(exc)})
        if len(verified)==1:
            url,check = verified[0]
            return {"epark_url":url,"epark_found":True,"epark_status":"VERIFIED","epark_match_score":check["score"],
                    "epark_match_reason":check["reasons"],"epark_checked_at":now(),"epark_contract":"UNKNOWN",
                    "epark_issues":issues,"research_status":"SUCCESS"},None
        status = "REVIEW" if urls else "NOT_FOUND"
        return {"epark_url":"","epark_found":None,"epark_status":status,"epark_candidates":urls,
                "epark_issues":issues,"epark_checked_at":now(),"epark_contract":"UNKNOWN","research_status":status},None

    def media(self,record,force=False):
        self.checkpoint()
        results = self.search.search(query_for(record,"掲載 記事 制作実績"),force=force)
        auto = media_signals(record,results)
        # 1回の一般検索で複数媒体を分類。媒体ごとに別検索しない。
        known = record.get("marketing_signals",[])
        known = [s for s in known if isinstance(s,dict) and s.get("evidence_type")!="検索結果の医院名・電話/住所一致"]
        return {"marketing_signals":dedupe_signals(known+auto),"media_checked_at":now(),"media_results":results,"research_status":"SUCCESS"},None

    def run(self,kind,record,force=False):
        if kind not in {"hp","epark","media"}:
            raise ValueError("調査の種類を選んでください。")
        return getattr(self,kind)(record,force)
