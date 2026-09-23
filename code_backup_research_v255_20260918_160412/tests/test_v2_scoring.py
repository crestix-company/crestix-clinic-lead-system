import pytest
from src.enrichment.hp_analysis import Page,identity,is_official_candidate
from src.master.samples import sample_records
from src.scoring.research_scoring import treatments,hp_signals,media_signals,rank_hp,hot_status,finalize_result,signal
from src.enrichment.profiles import estimate_profile_age


def page(content="",url="https://demo.example/",title=None):
    r=sample_records()[0]
    return Page(url,f"<title>{title or r['clinic_name']}</title><h1>{r['clinic_name']}</h1><p>{r['phone']} {r['address']}</p>{content}")


@pytest.mark.parametrize("url",["https://doctorsfile.jp/h/123/","https://epark.jp/shopinfo/hpl123/","https://medicaldoc.jp/clinic/1","https://youtube.com/@clinic","https://caloo.jp/hospitals/detail/1","https://byoinnavi.jp/1"])
def test_non_official_excluded(url):
    assert not is_official_candidate(url)
    assert not identity(sample_records()[0],page(url=url))["verified"]


def test_hp_verification_and_wrong_name_phone():
    r=sample_records()[0]
    assert identity(r,page())["verified"]
    assert identity(r,page())["score"]>=90
    assert not identity(r,Page("https://demo.example/","<title>別の医院</title>03-0000-0001"))["verified"]
    assert not identity(r,Page("https://demo.example/",f"<title>{r['clinic_name']}</title>"))["verified"]


@pytest.mark.parametrize("category,keyword",[("内視鏡","大腸内視鏡"),("白内障","多焦点眼内レンズ"),("緑内障","SLT"),("ICL","ICL"),("糖尿病","糖尿病専門医"),("泌尿器科","尿路結石"),("循環器内科","心エコー"),("皮膚科","ニキビ治療")])
def test_treatment_categories_evidence(category,keyword):
    result=treatments([page(f"<h2>{keyword}</h2><p>{keyword}に対応しています。</p>")])
    assert category in result["treatment_categories"]
    assert any(e["keyword"]==keyword and e["url"]=="https://demo.example/" for e in result["treatment_evidence"])


def test_footer_english_substring_negation_are_not_high_confidence():
    result=treatments([Page("https://demo.example/","<h1>青空医院</h1><footer>白内障</footer><p>medical dedicated bed</p>")])
    assert result["treatment_categories"]==[]
    result=treatments([page("<h2>内視鏡</h2><p>内視鏡は行っていません。</p>")])
    assert "内視鏡" not in result["treatment_categories"]


@pytest.mark.parametrize("content,name",[
    ('<a href="https://youtube.com/@official-clinic">YouTube</a>',"YouTube公式運用"),
    ('<iframe src="https://www.youtube.com/embed/demo" title="当院紹介"></iframe>',"YouTube公式運用"),
    ('<a href="https://www.tiktok.com/@clinic">TikTok</a>',"TikTok公式運用"),
    ('<script>window.difyChatbotConfig={token:"demo"}</script>',"AIチャット導入"),
    ('<p>AI相談</p><iframe src="https://chat-widget.example/"></iframe>',"AIチャット導入"),
    ('<h2>漫画でわかる</h2><img src="manga.jpg">',"漫画コンテンツ"),
    ('<a href="https://endoscopy.example/">内視鏡専門サイト</a>',"治療専門サイト")])
def test_html_signal_detection(content,name):
    results=hp_signals(sample_records()[0],[page(content)])
    assert name in [s["name"] for s in results]
    assert all(s.get("evidence_url") and s.get("evidence_type") for s in results)
    if name=="AIチャット導入":
        assert results[0]["service_name"]


def test_lp_requires_identity_and_no_false_chat_or_video():
    r=sample_records()[0]
    assert "治療専用LP" in [s["name"] for s in hp_signals(r,[page(url="https://demo.example/lp/endoscopy/")])]
    wrong=Page("https://other.example/lp/test/","<h1>別の医院</h1>チャット相談")
    assert hp_signals(r,[wrong])==[]
    generic=page('<p>チャットについてのお知らせ</p><iframe src="https://youtube.com/embed/random" title="動画"></iframe>')
    assert hp_signals(r,[generic])==[]


@pytest.mark.parametrize("url,name",[
    ("https://doctorsfile.jp/h/1/","Doctors File掲載"),("https://medicaldoc.jp/clinic/1/","Medical DOC掲載"),
    ("https://clinic.mynavi.jp/article/1/","マイナビ記事掲載"),("https://tokyo-doctors.com/clinic/1/","地域ドクターズ掲載"),
    ("https://kanagawa-doctors.com/clinic/1/","地域ドクターズ掲載"),("https://chiba-doctors.com/clinic/1/","地域ドクターズ掲載"),
    ("https://ganka-doc.com/clinic/1/","眼科Doc等専門媒体")])
def test_external_media_identity(url,name):
    r=sample_records()[0]
    result=media_signals(r,[{"url":url,"title":r["clinic_name"],"content":r["phone"]}])
    assert [s["name"] for s in result]==[name]
    assert media_signals(r,[{"url":url,"title":"別の医院","content":r["phone"]}])==[]


def test_caloo_plus_and_production_case_not_general_listing():
    r=sample_records()[0]
    base={"title":r["clinic_name"],"content":r["phone"]}
    assert media_signals(r,[{**base,"url":"https://caloo.jp/hospitals/1/"}])==[]
    result=media_signals(r,[{**base,"url":"https://caloo.jp/hospitals/1/","content":r["phone"]+" Caloo Plus"}])
    assert result[0]["name"]=="Caloo Plus"
    result=media_signals(r,[{**base,"url":"https://agency.example/works/1/","title":r["clinic_name"]+" ホームページ制作実績"}])
    assert result[0]["name"]=="HP制作会社の制作実績"


@pytest.mark.parametrize("n,label",[(0,"通常"),(1,"集客投資シグナルあり"),(2,"アツい"),(3,"かなりアツい"),(7,"かなりアツい")])
def test_signal_equal_weight_and_hot(n,label):
    from src.scoring.research_scoring import SIGNAL_NAMES
    signals=[signal(s,"https://source.example/","test","test") for s in SIGNAL_NAMES[2:2+n]]
    result=finalize_result({"marketing_signals":signals+signals})
    assert result["marketing_signal_count"]==n and result["hot_status"]==label


def test_epark_google_news_rules():
    r=sample_records()[0]
    p=page('<h2>お知らせ</h2><p>毎日更新しています。EPARK掲載・Google広告について</p><a href="https://epark.jp/shopinfo/hpl1/">公式HP</a>')
    assert hp_signals(r,[p])==[]
    x=finalize_result({"marketing_signals":[signal("Googleスポンサー広告確認済み","","",""),signal("EPARK課金済み確認","","","")]})
    assert x["marketing_signal_count"]==0
    assert finalize_result({"epark_contract":"PAID","google_ads_status":"CONFIRMED"})["marketing_signal_count"]==2


def test_hp_rank_features_are_separate_from_age():
    from src.master.samples import SampleFetcher
    p=SampleFetcher().fetch("https://demo1.example/")
    treatment=treatments([p]);signals=hp_signals(sample_records()[0],[p])
    strong=rank_hp([p],treatment,signals)
    assert strong["hp_rank"]=="A"
    assert rank_hp([Page("http://old.example/","医院")],{},[])["hp_rank"]=="D"
    assert rank_hp([],{},[])["hp_rank"]=="NO_HP"
    assert all("更新" not in reason["feature"] for reason in strong["hp_rank_reasons"])


@pytest.mark.parametrize("year",["2005年","平成17年","平成 17 年"])
def test_graduation_extracted_only_director_and_age(year):
    r=sample_records()[0]
    p=page(f"<h2>院長紹介</h2><p>院長 {r['manager_name']} {year} 架空大学医学部卒業</p>")
    result=estimate_profile_age(r,[p],current_year=2026)
    assert result["graduation_year"]==2005
    assert result["age_probability_under_59"]>.5
    wrong=estimate_profile_age({**r,"manager_name":"別人 院長"},[p],current_year=2026)
    assert wrong["age_probability_under_59"] is None


def test_age_conflict_and_unknown_not_guessed():
    r=sample_records()[0]
    assert estimate_profile_age(r,[])['age_probability_under_59'] is None
    result=estimate_profile_age({**r,"graduation_year":2005,"license_registration_year":1990},current_year=2026)
    assert result["age_estimation_confidence"]=="REVIEW" and result["age_probability_under_59"] is None
    result=estimate_profile_age({**r,"license_registration_year":1992},current_year=2026)
    assert .63<result["age_probability_under_59"]<.64
