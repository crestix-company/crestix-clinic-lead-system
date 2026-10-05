"""実運用5件で報告された外部サイト採用・治療誤検出の回帰テスト。外部通信は行わない。"""
import json
import pytest
from src.enrichment.hp_analysis import Page,identity,is_official_candidate
from src.enrichment.researcher import Researcher
from src.enrichment.search_provider import CachedSearch,SearchError
from src.master.filters import Filters
from src.master.jobs import create_job,run_job,job_status,job_limit
from src.master.samples import sample_records
from src.master.store import ClinicStore
from src.scoring.research_scoring import treatments,hp_signals,rank_hp,signal


@pytest.fixture
def store(tmp_path):
    return ClinicStore(tmp_path/"clinics.db")


def clinic_page(record,url,content=""):
    return Page(url,f'<title>{record["clinic_name"]}</title><h1>{record["clinic_name"]}</h1>'
                f'<p>{record["phone"]} {record["address"]}</p>{content}')


class Provider:
    def __init__(self,batches=()):
        self.batches=list(batches)
        self.queries=[]

    def search(self,query):
        self.queries.append(query)
        return self.batches[len(self.queries)-1] if len(self.queries)<=len(self.batches) else []


class Fetcher:
    def __init__(self,pages):
        self.pages=pages
        self.calls=[]

    def fetch(self,url,allowed_host=None):
        self.calls.append(url)
        return self.pages[url]


@pytest.mark.parametrize("url",[
    "https://job-medley.com/facility/345406/",
    "https://www.senshin-daido-life.jp/search/hospital/1314122859",
    "https://fdoc.jp/clinic/detail/index/id/160861/",
    "https://gmo-clinic-map.com/hospital/ref/mt25r4wjl",
    "https://www.web-clover.net/cgi-bin/disp.cgi?itky22110=",
])
def test_external_listing_identity_does_not_mean_clinic_website(url):
    record=sample_records()[0]
    page=clinic_page(record,url,'<h2>内視鏡</h2><p>内視鏡検査。</p>'
        '<a href="https://youtube.com/@jobmedley">運営会社公式YouTube</a>'
        '<a href="https://tiktok.com/@jobmedley">運営会社公式TikTok</a>')
    assert identity(record,page,official=False)["verified"]
    assert not is_official_candidate(url)
    assert not identity(record,page)["verified"]
    assert treatments([page])["treatment_categories"]==[]
    assert hp_signals(record,[page])==[]
    assert rank_hp([page],{},[])["hp_rank"]=="NO_HP"


@pytest.mark.parametrize("url",[
    "https://www.kanja.jp/",
    "https://www.w-medicalnet.com/index/module/Medical/",
    "https://www.e-doctors-net.com/",
    "https://arakawa-med.or.jp/",
    "https://www.machida.tokyo.med.or.jp/",
    "https://www.musashino-med.or.jp/",
])
def test_directory_and_medical_association_sites_are_not_official(url):
    # Phase4B overnight audit (sales_target_reclassification TASK1/TASK2, 2026-10-05):
    # these ward/city 医師会 directories and clinic-search aggregators were being
    # accepted as evidence sources, producing false-positive treatment matches for
    # whichever unrelated clinic happened to be listed on the same aggregator page.
    assert not is_official_candidate(url)


def test_clinic_owned_menu_page_is_still_official():
    # Same audit: a text-pattern heuristic ("一覧" etc.) was tried and rejected
    # because it false-triggered on ordinary clinic-owned treatment-menu pages
    # like bequas-cl.com's own "施術一覧" page. The fix must stay domain-only.
    assert is_official_candidate("https://bequas-cl.com/menu/")


def test_ophthalmic_laser_is_not_dermatology():
    record=sample_records()[0]
    page=clinic_page(record,"https://eye.example/",
        '<a href="/glaucoma/">緑内障レーザー治療</a><h2>緑内障レーザー治療</h2><p>緑内障レーザー治療を行います。</p>'
        '<h2>レーザー治療</h2><p>眼科のレーザー治療について。</p>')
    result=treatments([page])
    assert "緑内障" in result["treatment_categories"]
    assert "皮膚科" not in result["treatment_categories"]
    skin=clinic_page(record,"https://skin.example/",'<a href="/spots/">シミ治療</a><h2>シミ治療</h2><p>シミ治療に対応します。</p>')
    assert "皮膚科" in treatments([skin])["treatment_categories"]


def test_own_clinic_sns_and_shared_web_host_still_work():
    record=sample_records()[0]
    page=clinic_page(record,"https://www5a.biglobe.ne.jp/~gannka/",
        '<a href="https://youtube.com/@clinic">当院のYouTube</a>'
        '<a href="https://tiktok.com/@clinic">当院のTikTok</a>')
    assert identity(record,page)["verified"]
    assert {s["name"] for s in hp_signals(record,[page])}=={"YouTube公式運用","TikTok公式運用"}


@pytest.mark.parametrize("bad_url",[
    "https://job-medley.com/facility/345406/",
    "https://www.senshin-daido-life.jp/search/hospital/1314122859",
])
def test_previous_wrong_hp_and_first_search_listing_are_skipped(store,bad_url):
    record={**sample_records()[0],"hp_url":bad_url}
    url="https://correct-clinic.example/"
    provider=Provider([[{"url":bad_url},{"url":url}]])
    fetcher=Fetcher({url:clinic_page(record,url)})
    result,_=Researcher(CachedSearch(store,provider),fetcher).hp(record,force=True)
    assert result["hp_url"]==url and result["hp_verified"]
    assert fetcher.calls==[url] and len(provider.queries)==1
    assert result["marketing_signals"]==[]


def test_external_only_results_use_one_name_address_fallback(store):
    record={**sample_records()[0],"clinic_name":"医療法人社団 佳翔会 武蔵小金井さくら眼科"}
    url="https://sakuraganka.jp/"
    provider=Provider([[{"url":"https://job-medley.com/facility/1/"}],[{"url":url}]])
    fetcher=Fetcher({url:clinic_page(record,url)})
    result,_=Researcher(CachedSearch(store,provider),fetcher).hp(record)
    assert result["hp_url"]==url and result["hp_status"]=="VERIFIED"
    assert len(provider.queries)==2
    assert '"武蔵小金井さくら眼科"' in provider.queries[1]
    assert "佳翔会" not in provider.queries[1]
    assert record["address"] in provider.queries[1]
    assert "公式ホームページ" in provider.queries[1]


def test_redirect_to_listing_is_not_crawled_or_scored(store):
    record=sample_records()[0]
    url="https://old-clinic.example/"
    listing="https://job-medley.com/facility/1/"
    provider=Provider([[{"url":url}],[]])
    fetcher=Fetcher({url:clinic_page(record,listing,'<a href="/about/">医院概要</a>')})
    result,pages=Researcher(CachedSearch(store,provider),fetcher).hp(record)
    assert result["hp_status"]=="REVIEW" and not result["hp_verified"]
    assert pages==[] and result["marketing_signals"]==[] and fetcher.calls==[url]


def seed_incorrect_result(store):
    record=sample_records()[0]
    store.import_master([record])
    cid=store.query(Filters(hp_only=False))[0]["id"]
    store.save_research(cid,{"hp_status":"VERIFIED","hp_url":"https://job-medley.com/facility/1/",
        "hp_rank":"A","hp_score":100,"hp_rank_reasons":[{"feature":"誤ったSNS","points":100}],
        "treatment_categories":["皮膚科"],"treatment_evidence":[{"category":"皮膚科"}],
        "treatment_confidence":{"皮膚科":.9},"hp_identity_pages":["https://job-medley.com/facility/1/"],
        "marketing_signals":[signal("YouTube公式運用","https://job-medley.com/facility/1/","公式HPのチャンネルリンク","求人サイト自身のSNS")],
        "epark_url":"https://epark.jp/shopinfo/hpl123/","epark_status":"VERIFIED","epark_contract":"FREE"},
        [{"url":"https://job-medley.com/facility/1/","text":"旧ページ"}])
    return cid


@pytest.mark.parametrize("fail",[False,True])
def test_rerun_clears_untrusted_old_analysis_and_keeps_sources_history_epark(store,fail):
    cid=seed_incorrect_result(store)
    with store.connect() as c:
        before_sources=[tuple(row) for row in c.execute("SELECT * FROM source_records")]
        previous=c.execute("SELECT result_json FROM research_results WHERE clinic_id=?",(cid,)).fetchone()[0]
    class Search(Provider):
        def search(self,query):
            if fail:
                raise SearchError("Tavily接続エラー")
            return super().search(query)
    jid=create_job(store,Filters(hp_only=False),force=True,max_searches=10)
    run_job(store,jid,Search(),Fetcher({}))
    current=store.get(cid)
    assert current["hp_status"]==("ERROR" if fail else "NOT_FOUND")
    assert current["hp_url"]=="" and current["hp_score"]==0
    assert current["hp_rank_reasons"]==[] and current["hp_identity_pages"]==[]
    assert current["treatment_categories"]==[] and current["treatment_evidence"]==[]
    assert current["treatment_confidence"]=={} and current["marketing_signal_count"]==0
    assert current["epark_url"]=="https://epark.jp/shopinfo/hpl123/"
    assert current["epark_contract"]=="FREE"
    with store.connect() as c:
        assert [tuple(row) for row in c.execute("SELECT * FROM source_records")]==before_sources
        assert c.execute("SELECT count(*) FROM hp_pages WHERE clinic_id=?",(cid,)).fetchone()[0]==0
        assert previous in [row[0] for row in c.execute("SELECT before_json FROM change_history WHERE clinic_id=?",(cid,))]


def test_manual_corrections_survive_rerun(store):
    cid=seed_incorrect_result(store)
    store.override(cid,"treatment_categories",["白内障"],note="目視確認済み")
    jid=create_job(store,Filters(hp_only=False),force=True,max_searches=10)
    run_job(store,jid,Provider(),Fetcher({}))
    assert store.get(cid)["treatment_categories"]==["白内障"]
    with store.connect() as c:
        auto=json.loads(c.execute("SELECT result_json FROM research_results WHERE clinic_id=?",(cid,)).fetchone()[0])
    assert auto["treatment_categories"]==[]


@pytest.mark.parametrize("outcome",["success","not_found","error"])
def test_independently_verified_media_survives_hp_rerun(store,outcome):
    cid=seed_incorrect_result(store)
    current=store.get(cid)
    store.save_research(cid,{"marketing_signals":current["marketing_signals"]+[
        signal("Doctors File掲載","https://doctorsfile.jp/h/1/","検索結果の医院名・電話/住所一致","独立した媒体調査で確認") ]})
    url="https://official.example/"
    class Search(Provider):
        def search(self,query):
            if outcome=="error":
                raise SearchError("Tavily接続エラー")
            return [{"url":url}] if outcome=="success" else []
    jid=create_job(store,Filters(hp_only=False),force=True,max_searches=10)
    run_job(store,jid,Search(),Fetcher({url:clinic_page(current,url)}))
    assert [s["name"] for s in store.get(cid)["marketing_signals"]]==["Doctors File掲載"]
    assert store.get(cid)["marketing_signal_count"]==1


def test_fallback_budget_pause_reuses_first_search_on_resume(store):
    store.import_master(sample_records()[:1])
    record=store.query(Filters(hp_only=False))[0]
    url="https://official.example/"
    provider=Provider([[],[{"url":url}]])
    fetcher=Fetcher({url:clinic_page(record,url)})
    jid=create_job(store,Filters(hp_only=False),force=True,max_searches=1)
    run_job(store,jid,provider,fetcher)
    assert job_status(store,jid)["status"]=="BUDGET" and len(provider.queries)==1
    job_limit(store,jid,2)
    run_job(store,jid,provider,fetcher)
    assert job_status(store,jid)["status"]=="COMPLETED" and len(provider.queries)==2
    assert store.get(record["id"])["hp_url"]==url
