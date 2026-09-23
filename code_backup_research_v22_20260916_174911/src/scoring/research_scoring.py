"""治療、HP機能、集客施策を別々に評価。シグナルは種類ごとに常に1点。"""
from copy import deepcopy
from functools import lru_cache
from urllib.parse import urlparse
import re
from bs4 import BeautifulSoup
from src.enrichment.hp_analysis import identity, Page, host, domain_is, keyword_match, is_official_candidate
from src.enrichment.consultation_schedule import SIGNAL_NAME as MIDDAY_SIGNAL,midday_procedure
from src.utils.config import ROOT, read_config
from src.utils.date_utils import today_japan

SIGNAL_NAMES = ["Googleスポンサー広告確認済み","EPARK課金済み確認","Doctors File掲載","Medical DOC掲載","マイナビ記事掲載",
                "HP制作会社の制作実績","地域ドクターズ掲載","AIチャット導入","YouTube公式運用","TikTok公式運用","漫画コンテンツ",
                "治療専用LP","治療専門サイト","Caloo Plus","眼科Doc等専門媒体","その他有料施策・集客ツール",MIDDAY_SIGNAL]
MEDIA = {"doctorsfile.jp":"Doctors File掲載","medicaldoc.jp":"Medical DOC掲載","mynavi.jp":"マイナビ記事掲載",
         "mynavi-ms.jp":"マイナビ記事掲載","tokyo-doctors.com":"地域ドクターズ掲載","kanagawa-doctors.com":"地域ドクターズ掲載",
         "chiba-doctors.com":"地域ドクターズ掲載","ganka-doc.com":"眼科Doc等専門媒体"}


def signal(name, url, evidence_type, evidence, **kwargs):
    return {"name":name,"status":"CONFIRMED","evidence_url":url,"evidence_type":evidence_type,"evidence":evidence,**kwargs}


def dedupe_signals(signals):
    unique = {}
    for s in signals:
        if s.get("name") in SIGNAL_NAMES and s.get("status")=="CONFIRMED":
            unique.setdefault(s["name"],s)
    return [unique[n] for n in SIGNAL_NAMES if n in unique]


def hot_status(count):
    return "かなりアツい" if count>=3 else "アツい" if count==2 else "集客投資シグナルあり" if count==1 else "通常"


def treatments(pages, config=None):
    cfg = config or read_config(ROOT/"config/treatment_keywords.yml")
    pages = [p for p in pages if is_official_candidate(p.url)]
    evidence = []
    for category,keywords in cfg.items():
        for page in pages:
            for keyword in keywords:
                heading = keyword_match(keyword,page.title+" "+page.headings)
                body = keyword_match(keyword,page.main_text)
                if not heading and not body:
                    continue
                # 否定だけの行は治療提供の証拠にしない。
                sentences = re.split(r"[。\n]",page.main_text)
                affirmative = [s for s in sentences if keyword_match(keyword,s) and not re.search(r"行っていません|実施していません|対応していません|取り扱っていません|他院をご紹介|他院に紹介",s)]
                if body and not affirmative:
                    continue
                confidence = .9 if heading and body else .75 if body>=2 else .4
                evidence.append({"category":category,"url":page.url,"keyword":keyword,"confidence":confidence,
                                 "reason":"見出しと本文" if heading and body else "本文複数回" if body>=2 else "弱い言及・要確認"})
    categories = [k for k in cfg if any(e["category"]==k and e["confidence"]>=.7 for e in evidence)]
    return {"treatment_categories":categories,"treatment_evidence":evidence,
            "treatment_confidence":{k:max(e["confidence"] for e in evidence if e["category"]==k) for k in cfg if any(e["category"]==k for e in evidence)}}


def hp_signals(record,pages):
    found = []
    for page in pages:
        if not is_official_candidate(page.url):
            continue
        soup = BeautifulSoup(page.html,"html.parser")
        midday = midday_procedure(page.html)
        if midday:
            found.append(signal(MIDDAY_SIGNAL,page.url,midday["evidence_type"],midday["evidence"],
                **{k:v for k,v in midday.items() if k not in {"evidence_type","evidence"}}))
        for a in page.links:
            url,label = a["url"],a["text"]
            if (domain_is(url,"youtube.com") and re.search(r"/(?:@|channel/|c/|user/)",urlparse(url).path)):
                found.append(signal("YouTube公式運用",page.url,"公式HPのチャンネルリンク",url))
            if domain_is(url,"tiktok.com") and "/@" in urlparse(url).path:
                found.append(signal("TikTok公式運用",page.url,"公式HPのアカウントリンク",url))
            if re.search(r"専門サイト|専門ホームページ",label) and host(url) and host(url)!=host(page.url):
                found.append(signal("治療専門サイト",page.url,"公式HPからの専用サイトリンク",url))
            if re.search(r"漫画|マンガ|コミック",label) and re.search(r"manga|comic|漫画|マンガ",url,re.I):
                found.append(signal("漫画コンテンツ",page.url,"専用コンテンツリンク",url))
        for iframe in soup.select("iframe[src]"):
            url = iframe.get("src", "")
            if (domain_is(url,"youtube.com") or domain_is(url,"youtube-nocookie.com")) and re.search(r"当院|医院紹介|院長|クリニック紹介|公式",iframe.get("title", "")):
                found.append(signal("YouTube公式運用",page.url,"医院紹介の動画埋込",url))
        if re.search(r"漫画でわかる|マンガでわかる|コミック",page.headings) and (soup.select("img") or len(page.main_text)>100):
            found.append(signal("漫画コンテンツ",page.url,"専用見出し・コンテンツ","漫画専用コンテンツ"))
        if re.search(r"/(?:lp|landing|campaign)(?:/|[-_.]|$)",urlparse(page.url).path,re.I) and identity(record,page)["verified"]:
            found.append(signal("治療専用LP",page.url,"LP本人確認","医院名と電話/住所を確認"))
        embeds = " ".join(str(t) for t in soup.select("script,iframe,[data-chatbot-id],[data-ai-chat],[data-chat-widget]"))
        if re.search(r"(?:udify\.app|dify\.ai|difyChatbotConfig|data-ai-chat|data-chatbot-id)",embeds,re.I):
            service = "Dify" if re.search(r"dify|udify",embeds,re.I) else "AIチャット（サービス名未特定）"
            found.append(signal("AIチャット導入",page.url,"script/iframe/data属性","AIチャット埋込を確認",service_name=service))
        elif re.search(r"AIチャット|AI相談|24時間.*AI",page.text,re.I) and re.search(r"chatbot|chat-widget|chatplus|chatform|chatbase",embeds,re.I):
            found.append(signal("AIチャット導入",page.url,"AI記載とチャット埋込","AIの明記とウィジェットを確認",service_name="AIチャット（サービス名未特定）"))
    return dedupe_signals(found)


def media_signals(record, results):
    found = []
    for r in results:
        url,title,content = r.get("url",""),r.get("title",""),r.get("content","")
        import html
        page = Page(url,f"<title>{html.escape(title)}</title><h1>{html.escape(title)}</h1><main>{html.escape(content)}</main>")
        if not identity(record,page,official=False)["verified"]:
            continue
        name = next((s for domain,s in MEDIA.items() if domain_is(url,domain)),None)
        if name:
            found.append(signal(name,url,"検索結果の医院名・電話/住所一致",content[:500]))
        if (domain_is(url,"caloo.jp") or domain_is(url,"caloo.com")) and re.search(r"caloo\s*plus|カループラス",title+" "+content,re.I):
            found.append(signal("Caloo Plus",url,"Caloo Plus明記・医院本人確認",content[:500]))
        if re.search(r"制作実績|導入事例",title) and re.search(r"ホームページ|Webサイト|WEBサイト|HP制作",title+content):
            found.append(signal("HP制作会社の制作実績",url,"制作実績・医院本人確認",content[:500]))
    return dedupe_signals(found)


def rank_hp(pages, treatment, signals, config=None):
    cfg = config or read_config(ROOT/"config/hp_ranking.yml")
    pages = [p for p in pages if is_official_candidate(p.url)]
    if not pages:
        return {"hp_rank":"NO_HP","hp_score":0,"hp_rank_reasons":[]}
    html = "\n".join(p.html for p in pages)
    text = "\n".join(p.main_text for p in pages)
    soup = BeautifulSoup(html,"html.parser")
    links = [a for p in pages for a in p.links]
    treatment_urls = {e["url"] for e in treatment.get("treatment_evidence",[]) if e["confidence"]>=.7}
    features = {
        "HTTPS":pages[0].url.startswith("https://"), "スマホviewport":bool(soup.select('meta[name="viewport"]')),
        "Web予約導線":any(re.search(r"予約|受付|reserve|reservation|booking",a["text"]+a["url"],re.I) for a in links),
        "LINE導線":any(domain_is(a["url"],"line.me") or domain_is(a["url"],"lin.ee") for a in links),
        "治療専用ページ":bool(treatment_urls), "院長プロフィール":bool(re.search(r"院長紹介|院長プロフィール|院長挨拶|院長ご挨拶",text)),
        "写真":len(soup.select("img"))>=3, "料金情報":bool(re.search(r"料金|費用|価格|円[（(税込]",text)),
        "問合せCTA":any(re.search(r"お問い合わせ|問合せ|tel:",a["text"]+a["url"]) for a in links),
        "SNS導線":any(any(domain_is(a["url"],d) for d in ["instagram.com","youtube.com","tiktok.com","facebook.com","x.com"]) for a in links),
        "独自LP・専門サイト":any(s["name"] in {"治療専用LP","治療専門サイト"} for s in signals)}
    reasons = [{"feature":k,"points":int(cfg["weights"].get(k,0))} for k,v in features.items() if v]
    score = sum(r["points"] for r in reasons)
    rank = next((r for r in ["A","B","C","D"] if score>=cfg["thresholds"][r]),"D")
    return {"hp_rank":rank,"hp_score":score,"hp_rank_reasons":reasons,"hp_rank_version":cfg["version"]}


def analyze(record,pages,results=()):
    from src.enrichment.profiles import estimate_profile_age
    treatment = treatments(pages)
    signals = dedupe_signals(hp_signals(record,pages)+media_signals(record,results))
    return {**treatment, **rank_hp(pages,treatment,signals), **estimate_profile_age(record,pages),
            "marketing_signals":signals, "marketing_signal_count":len(signals),"hot_status":hot_status(len(signals))}


def finalize_result(data):
    data = deepcopy(data)
    signals = data.get("marketing_signals",[])
    if signals and isinstance(signals[0],str):
        signals = [signal(n,"","手動確認","手動で選択したシグナル") for n in signals]
    # 契約/広告の専用状態を唯一の正とし、一般シグナルチェックでの裏口を作らない。
    signals = [s for s in signals if s.get("name") not in {"Googleスポンサー広告確認済み","EPARK課金済み確認"}]
    if data.get("epark_contract")=="PAID":
        signals.append(signal("EPARK課金済み確認",data.get("epark_url",""),"契約状態の確認","課金済み確認"))
    if data.get("google_ads_status")=="CONFIRMED":
        signals.append(signal("Googleスポンサー広告確認済み",data.get("hp_url",""),"手動広告確認","Googleスポンサー広告確認済み"))
    signals = dedupe_signals(signals)
    data.update(marketing_signals=signals,marketing_signal_count=len(signals),hot_status=hot_status(len(signals)))
    data.setdefault("hp_status","UNRESEARCHED")
    if data["hp_status"]=="NOT_FOUND" and "hp_rank" not in data.get("manual_fields",[]):
        data["hp_rank"] = "NO_HP"
    if data["hp_status"]=="VERIFIED" and not data.get("hp_url"):
        data["hp_status"] = "REVIEW"
    data["hp_verified"] = data["hp_status"]=="VERIFIED"
    data.setdefault("epark_contract","UNKNOWN")
    data.setdefault("google_ads_status","UNKNOWN")
    return data
