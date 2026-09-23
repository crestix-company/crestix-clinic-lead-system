import json
import socket
from pathlib import Path
from types import SimpleNamespace
import pytest
from src.master.store import ClinicStore
from src.master.filters import Filters
from src.master.samples import sample_records,SampleProvider,SampleFetcher
from src.master.jobs import create_job,run_job,job_status,pause_job,job_limit
from src.enrichment.safe_web import validate_url,SafeFetcher,WebResponse,WebError,crawl
from src.enrichment.search_provider import CachedSearch,TavilySearchProvider,SearchError,BudgetReached
from src.enrichment.researcher import Researcher
from src.enrichment.hp_analysis import Page


@pytest.fixture
def store(tmp_path):
    return ClinicStore(tmp_path/"clinics.db")


class Provider:
    def __init__(self,results=(),fail=False):
        self.calls=0;self.results=list(results);self.fail=fail
    def search(self,query):
        self.calls+=1
        if self.fail:
            raise SearchError("Tavily接続エラー")
        return self.results


def test_cache_persists_force_only_and_cap(store):
    p=Provider([{"url":"https://demo.example/","title":"医院","content":"根拠"}])
    s=CachedSearch(store,p,max_searches=2)
    assert s.search("医院名 電話")==p.results
    assert s.search("医院名 電話")==p.results and p.calls==1
    s=CachedSearch(ClinicStore(store.path),p,max_searches=2)
    s.search("医院名 電話")
    assert p.calls==1
    s.search("医院名 電話",force=True)
    assert p.calls==2
    store.set_setting("monthly_limit",2)
    with pytest.raises(BudgetReached):
        s.search("別の医院")
    assert p.calls==2


def test_failed_search_counts_and_reserve_and_month_boundary(store):
    p=Provider(fail=True);s=CachedSearch(store,p)
    store.set_setting("monthly_limit",2)
    with pytest.raises(SearchError):
        s.search("失敗")
    assert s.monthly_usage()==1
    store.set_setting("external_usage_reserve",1)
    with pytest.raises(BudgetReached):
        s.search("別医院")
    assert p.calls==1
    with store.connect() as c:
        c.execute("UPDATE search_usage SET month='2000-01'")
    assert s.monthly_usage()==0


def test_tavily_basic_api_payload_errors_and_no_key_in_db(store):
    class Session:
        def post(self,url,**kwargs):
            assert url=="https://api.tavily.com/search"
            assert kwargs["json"]["search_depth"]=="basic"
            assert kwargs["json"]["auto_parameters"] is False
            assert kwargs["allow_redirects"] is False
            return SimpleNamespace(status_code=200,json=lambda:{"results":[{"url":"https://example.com/","title":"確認","content":"医院"}],"api_key":"secret-value"})
    p=TavilySearchProvider("secret-value",Session())
    CachedSearch(store,p).search("公開医院名")
    assert b"secret-value" not in store.backup_bytes()


@pytest.mark.parametrize("status",[401,429,432,500,503])
def test_tavily_errors_no_retry_no_secret(status):
    class Session:
        calls=0
        def post(self,*a,**kw):
            self.calls+=1
            return SimpleNamespace(status_code=status,text="secret-value")
    session=Session()
    p=TavilySearchProvider("secret-value",session)
    with pytest.raises(SearchError) as exc:
        p.search("医院")
    assert "secret-value" not in str(exc.value) and session.calls==1


@pytest.mark.parametrize("url",["http://127.0.0.1/","http://localhost/","https://x.local/","http://10.1.2.3/","http://192.168.1.1/",
    "http://169.254.169.254/","http://[::1]/","http://[fc00::1]/","file:///etc/passwd","ftp://example.com/","http://u:p@example.com/","http://example.com:8080/","http://0.0.0.0/"])
def test_ssrf_url_block(url):
    with pytest.raises(WebError):
        validate_url(url,resolve=False)


def test_ssrf_dns_private_any_address_blocks(monkeypatch):
    monkeypatch.setattr(socket,"getaddrinfo",lambda *a,**k:[(2,1,6,"",("8.8.8.8",80)),(2,1,6,"",("127.0.0.1",80))])
    with pytest.raises(WebError):
        validate_url("http://public.example/")


class Transport:
    def __init__(self,routes):
        self.routes=routes;self.calls=[]
    def get(self,url,timeout,max_bytes):
        self.calls.append(url)
        response=self.routes[url]
        if isinstance(response,Exception):
            raise response
        return response


def response(text,status=200,**headers):
    return WebResponse(status,{"content-type":"text/html; charset=utf-8",**headers},text.encode())


def test_redirect_ssrf_robots_block_size_and_loop():
    transport=Transport({"https://clinic.example/robots.txt":response("",404),"https://clinic.example/":response("",302,location="http://127.0.0.1/")})
    fetch=SafeFetcher(transport,interval=0)
    with pytest.raises(WebError):
        fetch.fetch("https://clinic.example/")
    assert all("127.0.0.1" not in url for url in transport.calls)
    transport=Transport({"https://clinic.example/robots.txt":response("User-agent: *\nDisallow: /\n")})
    with pytest.raises(WebError,match="robots"):
        SafeFetcher(transport,interval=0).fetch("https://clinic.example/")
    assert len(transport.calls)==1
    transport=Transport({"https://clinic.example/robots.txt":response("",404),"https://clinic.example/":response("x"*101)})
    with pytest.raises(WebError,match="サイズ"):
        SafeFetcher(transport,max_bytes=100,interval=0).fetch("https://clinic.example/")


def test_crawl_domain_limit_duplicates_failure_continues():
    first=Page("https://clinic.example/",'<a href="/treat/">治療</a><a href="/treat/#x">治療</a><a href="/bad/">検査</a><a href="https://other.example/">外部</a><a href="/x.pdf">資料</a>')
    class Fetcher:
        calls=[]
        def fetch(self,url,allowed_host=None):
            self.calls.append(url)
            assert allowed_host=="clinic.example"
            if "/bad/" in url:
                raise WebError("通信エラー")
            return Page(url,'<a href="/">top</a><a href="/more/">治療</a>')
    f=Fetcher();pages,errors=crawl(first,f,max_pages=3)
    assert len(f.calls)==2
    assert f.calls.count("https://clinic.example/treat/")==1
    assert len(errors)==1 and len(pages)==2


def test_403_blocks_host_further_fetch():
    t=Transport({"https://clinic.example/robots.txt":response("",404),"https://clinic.example/":response("",403)})
    f=SafeFetcher(t,interval=0)
    with pytest.raises(WebError):f.fetch("https://clinic.example/")
    with pytest.raises(WebError):f.fetch("https://clinic.example/second/")
    assert len(t.calls)==2


def test_hp_not_found_and_error_not_equated(store):
    researcher=Researcher(CachedSearch(store,Provider()),SampleFetcher())
    result,_=researcher.hp(sample_records()[0])
    assert result["hp_status"]=="NOT_FOUND" and result["hp_rank"]=="NO_HP"
    record=sample_records()[0]
    result,_=Researcher(CachedSearch(store,Provider([{"url":"https://broken.example/","title":record["clinic_name"],"content":""}])) ,SampleFetcher()).hp(record,force=True)
    assert result["hp_status"]=="ERROR"


@pytest.mark.parametrize("has_hp_link",[True,False])
def test_epark_route_identity_never_infers_contract(store,has_hp_link):
    r=sample_records()[0];url="https://epark.jp/shopinfo/hpl123/"
    search=CachedSearch(store,Provider([{"url":url,"title":r["clinic_name"],"content":r["phone"]}]))
    class Fetch:
        def fetch(self,url):
            return Page(url,f'<title>{r["clinic_name"]}</title><p>{r["phone"]} {r["address"]}</p>'+('<a href="https://clinic.example/">HP</a>' if has_hp_link else ''))
    result,_=Researcher(search,Fetch()).epark(r)
    assert result["epark_found"] is True and result["epark_contract"]=="UNKNOWN"
    assert result["epark_url"]==url
    assert search.used==1


def test_epark_no_results_fallback_two_queries(store):
    search=CachedSearch(store,Provider())
    result,_=Researcher(search,SampleFetcher()).epark(sample_records()[0])
    assert result["epark_status"]=="NOT_FOUND"
    assert search.used==2


def test_job_budget_pause_resume_done_not_researched(store):
    store.import_master(sample_records())
    jid=create_job(store,Filters(active_only=False,hp_only=False),limit=4,max_searches=1,max_pages=2)
    p=SampleProvider()
    run_job(store,jid,p,SampleFetcher())
    job=job_status(store,jid)
    assert job["status"]=="BUDGET" and job["counts"]["DONE"]==1 and p.calls==1
    job_limit(store,jid,10)
    run_job(store,jid,p,SampleFetcher())
    job=job_status(store,jid)
    assert job["status"]=="COMPLETED" and job["counts"]["DONE"]==4 and p.calls==5
    assert job["results"]=={"SUCCESS":3,"NOT_FOUND":1}
    run_job(store,jid,p,SampleFetcher())
    assert p.calls==5
    with pytest.raises(ValueError,match="未調査"):
        create_job(store,Filters(active_only=False,hp_only=False))


def test_network_error_one_clinic_does_not_stop_batch(store):
    store.import_master(sample_records()[:2])
    class FailFirst(SampleProvider):
        def search(self,query):
            if "青空" in query:
                self.calls+=1
                raise SearchError("通信エラー")
            return super().search(query)
    jid=create_job(store,Filters(hp_only=False),max_searches=10)
    run_job(store,jid,FailFirst(),SampleFetcher())
    assert job_status(store,jid)["results"]=={"ERROR":1,"SUCCESS":1}
    assert job_status(store,jid)["status"]=="COMPLETED"


@pytest.mark.parametrize("force",[False,True])
def test_pause_during_request_replays_cache_but_not_api(store,force):
    store.import_master(sample_records()[:1])
    jid=create_job(store,Filters(hp_only=False),max_searches=10,force=force)
    class Pause(SampleProvider):
        def search(self,q):
            result=super().search(q)
            pause_job(store,jid)
            return result
    p=Pause()
    run_job(store,jid,p,SampleFetcher())
    assert job_status(store,jid)["status"]=="PAUSED"
    run_job(store,jid,p,SampleFetcher())
    assert job_status(store,jid)["status"]=="COMPLETED"
    assert p.calls==1


def test_crash_recovery_returns_running_item_to_pending(store):
    store.import_master(sample_records()[:1])
    jid=create_job(store,Filters(hp_only=False))
    with store.connect() as c:
        c.execute("UPDATE research_job_items SET state='RUNNING' WHERE job_id=?",(jid,))
        c.execute("UPDATE research_jobs SET status='RUNNING' WHERE id=?",(jid,))
    run_job(store,jid,SampleProvider(),SampleFetcher())
    assert job_status(store,jid)["status"]=="COMPLETED"
