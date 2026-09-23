"""治療、HP機能、集客施策を別々に評価。営業用途では本文の単語数より「医院が前面に出している情報」を優先する。"""
from copy import deepcopy
from urllib.parse import urlparse, urljoin
import re
from bs4 import BeautifulSoup
from src.enrichment.hp_analysis import identity, Page, host, domain_is, keyword_match, is_official_candidate
from src.enrichment.consultation_schedule import SIGNAL_NAME as MIDDAY_SIGNAL,midday_procedure
from src.utils.config import ROOT, read_config

SIGNAL_NAMES = [
    "Googleスポンサー広告確認済み","EPARK課金済み確認","Doctors File掲載","Medical DOC掲載","マイナビ記事掲載",
    "HP制作会社の制作実績","地域ドクターズ掲載","Caloo Plus","眼科Doc等専門媒体",
    "AIチャット導入","YouTube公式運用","TikTok公式運用","Instagram公式運用","LINE公式運用","オンライン診療",
    "漫画コンテンツ","治療専用LP","治療専門サイト","その他有料施策・集客ツール",MIDDAY_SIGNAL
]
MEDIA = {
    "doctorsfile.jp":"Doctors File掲載","medicaldoc.jp":"Medical DOC掲載","mynavi.jp":"マイナビ記事掲載",
    "mynavi-ms.jp":"マイナビ記事掲載","tokyo-doctors.com":"地域ドクターズ掲載","kanagawa-doctors.com":"地域ドクターズ掲載",
    "chiba-doctors.com":"地域ドクターズ掲載","ganka-doc.com":"眼科Doc等専門媒体"
}
ARTICLE_PATH = re.compile(r"/(?:blog|column|news|topics?|article|information)(?:/|$)",re.I)
INTRO_PAGE = re.compile(r"医院紹介|クリニック紹介|当院について|診療案内|診療内容|診療科|治療内容|about|clinic|medical|service|treatment",re.I)
NEGATIVE_TREATMENT_LABEL = re.compile(r"学会|ガイドライン|論文|採用|求人|ニュース|お知らせ|ブログ|コラム|他院|紹介状")


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


def _treatment_terms(category, keywords):
    # より具体的な長い語を先に返す（大腸内視鏡 > 内視鏡、糖尿病専門医 > 糖尿病）。
    terms=list(dict.fromkeys(list(keywords or [])+[category]))
    return sorted(terms,key=lambda x:(len(x),x),reverse=True)


def _category_term(category, text, terms):
    """カテゴリ固有の誤検出を抑えつつ、前面に出した治療名だけを採用する。"""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    term = _matching_term(text, terms)
    if not term:
        return None

    # 眼科の「糖尿病網膜症」は糖尿病内科を意味しない。
    if category == "糖尿病":
        if re.search(r"糖尿病網膜症|diabetic\s+retinopathy", text, re.I) and not re.search(
            r"糖尿病(?:内科|外来|専門|診療)|1型糖尿病|2型糖尿病|インスリン|CGM|リブレ|HbA1c", text, re.I
        ):
            return None

    # ED・男性更年期だけでは泌尿器科カテゴリに昇格させない。
    if category == "泌尿器科" and term in {"ED", "男性更年期"}:
        if not re.search(r"泌尿器科|尿路|前立腺|膀胱|腎|結石", text):
            return None

    return term


def _matching_term(text, terms):
    return next((term for term in terms if keyword_match(term,text)),None)


def _anchor_label(a):
    pieces = [a.get_text(" ",strip=True),a.get("title",""),a.get("aria-label","")]
    pieces += [img.get("alt","") for img in a.select("img[alt]")]
    return re.sub(r"\s+"," "," ".join(x for x in pieces if x)).strip()


def _intro_page(page, index):
    if index==0:
        return True
    p = urlparse(page.url)
    return bool(INTRO_PAGE.search(page.title+" "+page.headings+" "+p.path))


def _canonical_url(url):
    p=urlparse(url or "")
    path=re.sub(r"/+$","",p.path or "/") or "/"
    return (p.scheme.lower(),p.netloc.lower(),path)


def _primary_heading(page):
    soup=BeautifulSoup(page.html,"html.parser")
    node=soup.find("h1") or soup.find("h2")
    if node:
        return re.sub(r"\s+"," ",node.get_text(" ",strip=True)).strip()
    title=(page.title or "").strip()
    return re.split(r"\s*[｜|–—-]\s*",title,maxsplit=1)[0].strip()


def treatments(pages, record=None, config=None):
    """営業用の治療カテゴリ。

    優先順位:
    1) 医院名
    2) HOMEの実際の治療メニュー/タブ
    3) 医院紹介・診療案内の実際の治療メニュー/タブ
    4) 2/3から直接リンクされた独立治療ページの主見出し

    共通SEOタイトル、サイトマップ、本文に単語があるだけのページはカテゴリ化しない。
    """
    cfg = config or read_config(ROOT/"config/treatment_keywords.yml")
    pages = [p for p in pages if is_official_candidate(p.url)]
    evidence = []
    seen = set()
    linked = {}

    def add(category,url,keyword,confidence,reason,source,hot=False,label=""):
        key=(category,source,_canonical_url(url),keyword,label)
        if key in seen:
            return
        seen.add(key)
        evidence.append({"category":category,"url":url,"keyword":keyword,"confidence":confidence,
                         "reason":reason,"source":source,"hot":bool(hot),"label":label})

    clinic_name = str((record or {}).get("clinic_name", ""))
    home_url = pages[0].url if pages else ""
    if clinic_name:
        for category,keywords in cfg.items():
            term=_category_term(category,clinic_name,_treatment_terms(category,keywords))
            if term:
                add(category,home_url,term,1.0,"医院名に治療・診療名を含む（最優先）","CLINIC_NAME",True,clinic_name)

    # HOME / 医院紹介 / 診療案内の実リンクだけを見る。
    for index,page in enumerate(pages):
        if not _intro_page(page,index):
            continue
        soup=BeautifulSoup(page.html,"html.parser")
        for a in soup.select("a[href]"):
            if a.find_parent("footer") is not None:
                continue
            label=_anchor_label(a)
            if not label or len(label)>100 or NEGATIVE_TREATMENT_LABEL.search(label):
                continue
            href=urljoin(page.url,a.get("href",""))
            # 治療カテゴリは公式サイト内部の導線に限定。
            if host(href) and host(href)!=host(page.url):
                continue
            for category,keywords in cfg.items():
                term=_category_term(category,label,_treatment_terms(category,keywords))
                if not term:
                    continue
                source="HOME_MENU" if index==0 else "INTRO_MENU"
                reason="HOMEの治療メニュー・タブ" if index==0 else "医院紹介・診療案内の治療メニュー・タブ"
                add(category,href,term,.98,reason,source,False,label)
                linked.setdefault(_canonical_url(href),set()).add(category)

    # 独立ページは、上記メニューから直接リンクされたページだけ補強根拠にする。
    for page in pages:
        key=_canonical_url(page.url)
        categories=linked.get(key,set())
        if not categories or ARTICLE_PATH.search(urlparse(page.url).path):
            continue
        heading=_primary_heading(page)
        if not heading or NEGATIVE_TREATMENT_LABEL.search(heading):
            continue
        for category in categories:
            keywords=cfg.get(category,[])
            term=_category_term(category,heading,_treatment_terms(category,keywords))
            if not term:
                continue
            negative=any(
                keyword_match(term,sentence) and re.search(r"行っていません|実施していません|対応していません|取り扱っていません|他院をご紹介|他院に紹介",sentence)
                for sentence in re.split(r"[。\n]",page.main_text)
            )
            if negative:
                continue
            add(category,page.url,term,.96,"治療メニューから直接リンクされた独立ページの主見出し",
                "DEDICATED_PAGE",False,heading[:160])

    categories=[category for category in cfg if any(e["category"]==category and e["confidence"]>=.95 for e in evidence)]
    confidence={category:max(e["confidence"] for e in evidence if e["category"]==category) for category in categories}
    name_hot=[category for category in categories if any(e["category"]==category and e["source"]=="CLINIC_NAME" for e in evidence)]
    return {"treatment_categories":categories,"treatment_evidence":evidence,"treatment_confidence":confidence,
            "treatment_name_hot":bool(name_hot),"treatment_name_hot_categories":name_hot}

def _link_area(a):
    if a.find_parent("footer") is not None:
        return "公式HPトップページ下部（フッター）"
    if a.find_parent("aside") is not None:
        return "公式HP左右サイド"
    node=a
    for _ in range(4):
        if node is None:
            break
        marker=" ".join([" ".join(node.get("class",[])),node.get("id","") if hasattr(node,"get") else "",node.get("style","") if hasattr(node,"get") else ""])
        if re.search(r"side|sidebar|fixed|float|banner|sticky|bnr",marker,re.I):
            return "公式HP左右/固定バナー"
        node=node.parent
    return "公式HPリンク"


def _link_context(a):
    parent=a.parent
    text=parent.get_text(" ",strip=True) if parent else ""
    return re.sub(r"\s+"," ",text)[:240]


def hp_signals(record,pages):
    found=[]
    for index,page in enumerate(pages):
        if not is_official_candidate(page.url):
            continue
        soup=BeautifulSoup(page.html,"html.parser")
        midday=midday_procedure(page.html)
        if midday:
            found.append(signal(MIDDAY_SIGNAL,page.url,midday["evidence_type"],midday["evidence"],
                **{k:v for k,v in midday.items() if k not in {"evidence_type","evidence"}}))

        # 集客導線はトップページの下部・左右バナーを優先。画像バナーのaltも読む。
        for a in soup.select("a[href]"):
            url=urljoin(page.url,a.get("href",""))
            label=_anchor_label(a)
            context=(label+" "+_link_context(a)).strip()
            area=_link_area(a) if index==0 else "公式HP内リンク"
            direct=next((name for domain,name in MEDIA.items() if domain_is(url,domain)),None)
            if direct:
                found.append(signal(direct,page.url,area,f"{context[:160]} → {url}"))
            if (domain_is(url,"caloo.jp") or domain_is(url,"caloo.com")) and re.search(r"caloo\s*plus|カループラス",context,re.I):
                found.append(signal("Caloo Plus",page.url,area,f"{context[:160]} → {url}"))
            if domain_is(url,"youtube.com") and re.search(r"/(?:@|channel/|c/|user/)",urlparse(url).path):
                found.append(signal("YouTube公式運用",page.url,area,url))
            if domain_is(url,"tiktok.com") and "/@" in urlparse(url).path:
                found.append(signal("TikTok公式運用",page.url,area,url))
            if domain_is(url,"instagram.com") and re.search(r"^/(?!p/|reel/|explore/)[^/]+/?$",urlparse(url).path):
                found.append(signal("Instagram公式運用",page.url,area,url))
            if domain_is(url,"lin.ee") or (domain_is(url,"line.me") and re.search(r"/R/ti/p|/ti/p|/oaMessage|/chat",urlparse(url).path,re.I)):
                found.append(signal("LINE公式運用",page.url,area,url))
            if re.search(r"オンライン診療|オンライン外来|遠隔診療|telemedicine",context,re.I):
                found.append(signal("オンライン診療",page.url,area,f"{context[:160]} → {url}"))
            if re.search(r"専門サイト|専門ホームページ|専門ページ",context) and host(url) and host(url)!=host(page.url):
                found.append(signal("治療専門サイト",page.url,area,url))
            if re.search(r"漫画|マンガ|コミック",context) and re.search(r"manga|comic|漫画|マンガ",url+context,re.I):
                found.append(signal("漫画コンテンツ",page.url,area,url))
            if host(url) and host(url)!=host(page.url) and re.search(r"ホームページ制作|HP制作|Web制作|WEB制作|サイト制作|designed\s+by|powered\s+by",context,re.I):
                found.append(signal("HP制作会社の制作実績",page.url,area,f"{context[:160]} → {url}"))

        for iframe in soup.select("iframe[src]"):
            url=iframe.get("src","")
            if (domain_is(url,"youtube.com") or domain_is(url,"youtube-nocookie.com")) and re.search(r"当院|医院紹介|院長|クリニック紹介|公式",iframe.get("title","")):
                found.append(signal("YouTube公式運用",page.url,"医院紹介の動画埋込",url))
        if re.search(r"漫画でわかる|マンガでわかる|コミック",page.headings) and (soup.select("img") or len(page.main_text)>100):
            found.append(signal("漫画コンテンツ",page.url,"専用見出し・コンテンツ","漫画専用コンテンツ"))
        if re.search(r"/(?:lp|landing|campaign)(?:/|[-_.]|$)",urlparse(page.url).path,re.I) and identity(record,page)["verified"]:
            found.append(signal("治療専用LP",page.url,"LP本人確認","医院名と電話/住所を確認"))
        embeds=" ".join(str(t) for t in soup.select("script,iframe,[data-chatbot-id],[data-ai-chat],[data-chat-widget]"))
        if re.search(r"(?:udify\.app|dify\.ai|difyChatbotConfig|data-ai-chat|data-chatbot-id|chatplus|chatbase|chatform)",embeds,re.I):
            service="Dify" if re.search(r"dify|udify",embeds,re.I) else "AIチャット（サービス名未特定）"
            found.append(signal("AIチャット導入",page.url,"script/iframe/data属性","チャットウィジェット埋込を確認",service_name=service))
        elif re.search(r"AIチャット|AI相談|24時間.*AI",page.text,re.I) and re.search(r"chatbot|chat-widget",embeds,re.I):
            found.append(signal("AIチャット導入",page.url,"AI記載とチャット埋込","AIの明記とウィジェットを確認",service_name="AIチャット（サービス名未特定）"))
    return dedupe_signals(found)


def media_signals(record,results):
    found=[]
    for r in results:
        url,title,content=r.get("url",""),r.get("title",""),r.get("content","")
        import html
        page=Page(url,f"<title>{html.escape(title)}</title><h1>{html.escape(title)}</h1><main>{html.escape(content)}</main>")
        if not identity(record,page,official=False)["verified"]:
            continue
        name=next((s for domain,s in MEDIA.items() if domain_is(url,domain)),None)
        if name:
            found.append(signal(name,url,"検索結果の医院名・電話/住所一致",content[:500]))
        if (domain_is(url,"caloo.jp") or domain_is(url,"caloo.com")) and re.search(r"caloo\s*plus|カループラス",title+" "+content,re.I):
            found.append(signal("Caloo Plus",url,"Caloo Plus明記・医院本人確認",content[:500]))
        if re.search(r"制作実績|導入事例",title) and re.search(r"ホームページ|Webサイト|WEBサイト|HP制作",title+content):
            found.append(signal("HP制作会社の制作実績",url,"制作実績・医院本人確認",content[:500]))
    return dedupe_signals(found)


def rank_hp(pages,treatment,signals,config=None):
    cfg=config or read_config(ROOT/"config/hp_ranking.yml")
    pages=[p for p in pages if is_official_candidate(p.url)]
    if not pages:
        return {"hp_rank":"NO_HP","hp_score":0,"hp_rank_reasons":[]}
    html="\n".join(p.html for p in pages)
    text="\n".join(p.main_text for p in pages)
    soup=BeautifulSoup(html,"html.parser")
    links=[a for p in pages for a in p.links]
    treatment_urls={e["url"] for e in treatment.get("treatment_evidence",[]) if e["confidence"]>=.9 and e.get("source")!="CLINIC_NAME"}
    features={
        "HTTPS":pages[0].url.startswith("https://"),"スマホviewport":bool(soup.select('meta[name="viewport"]')),
        "Web予約導線":any(re.search(r"予約|受付|reserve|reservation|booking",a["text"]+a["url"],re.I) for a in links),
        "LINE導線":any(domain_is(a["url"],"line.me") or domain_is(a["url"],"lin.ee") for a in links),
        "治療専用ページ":bool(treatment_urls),"院長プロフィール":bool(re.search(r"院長紹介|院長プロフィール|院長挨拶|院長ご挨拶",text)),
        "写真":len(soup.select("img"))>=3,"料金情報":bool(re.search(r"料金|費用|価格|円[（(税込]",text)),
        "問合せCTA":any(re.search(r"お問い合わせ|問合せ|tel:",a["text"]+a["url"]) for a in links),
        "SNS導線":any(any(domain_is(a["url"],d) for d in ["instagram.com","youtube.com","tiktok.com","facebook.com","x.com"]) for a in links),
        "独自LP・専門サイト":any(s["name"] in {"治療専用LP","治療専門サイト"} for s in signals)}
    reasons=[{"feature":k,"points":int(cfg["weights"].get(k,0))} for k,v in features.items() if v]
    score=sum(r["points"] for r in reasons)
    rank=next((r for r in ["A","B","C","D"] if score>=cfg["thresholds"][r]),"D")
    return {"hp_rank":rank,"hp_score":score,"hp_rank_reasons":reasons,"hp_rank_version":cfg["version"]}


def analyze(record,pages,results=()):
    from src.enrichment.profiles import estimate_profile_age
    treatment=treatments(pages,record=record)
    signals=dedupe_signals(hp_signals(record,pages)+media_signals(record,results))
    return {**treatment,**rank_hp(pages,treatment,signals),**estimate_profile_age(record,pages),
            "marketing_signals":signals,"marketing_signal_count":len(signals),"hot_status":hot_status(len(signals))}


def finalize_result(data):
    data=deepcopy(data)
    signals=data.get("marketing_signals",[])
    if signals and isinstance(signals[0],str):
        signals=[signal(n,"","手動確認","手動で選択したシグナル") for n in signals]
    signals=[s for s in signals if s.get("name") not in {"Googleスポンサー広告確認済み","EPARK課金済み確認"}]
    if data.get("epark_contract")=="PAID":
        signals.append(signal("EPARK課金済み確認",data.get("epark_url",""),"契約状態の確認","課金済み確認"))
    if data.get("google_ads_status")=="CONFIRMED":
        signals.append(signal("Googleスポンサー広告確認済み",data.get("hp_url",""),"手動広告確認","Googleスポンサー広告確認済み"))
    signals=dedupe_signals(signals)
    data.update(marketing_signals=signals,marketing_signal_count=len(signals),hot_status=hot_status(len(signals)))
    data.setdefault("hp_status","UNRESEARCHED")
    if data["hp_status"]=="NOT_FOUND" and "hp_rank" not in data.get("manual_fields",[]):
        data["hp_rank"]="NO_HP"
    if data["hp_status"]=="VERIFIED" and not data.get("hp_url"):
        data["hp_status"]="REVIEW"
    data["hp_verified"]=data["hp_status"]=="VERIFIED"
    data.setdefault("epark_contract","UNKNOWN")
    data.setdefault("google_ads_status","UNKNOWN")
    return data
