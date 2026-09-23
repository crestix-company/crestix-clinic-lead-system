"""Tavily basicだけを使う交換可能なアダプター。全試行を予約して上限を守る。"""
from typing import Protocol
import hashlib
import json
import os
import requests
from src.master.store import now,dumps
from src.utils.date_utils import today_japan


class SearchError(Exception):
    pass


class BudgetReached(SearchError):
    pass


class SearchProvider(Protocol):
    def search(self,query:str)->list[dict]: ...


class TavilySearchProvider:
    def __init__(self,api_key=None,session=None):
        self._api_key = api_key or os.getenv("TAVILY_API_KEY", "")
        self.session = session or requests.Session()

    def search(self,query):
        # Google Mapsで公式HP URLを取得済みの医院は検索APIを呼ばない。
        # キーは、実際に検索が必要になったときだけ必須にする。
        if not self._api_key:
            raise SearchError("Tavily APIキーを入力してください。Google MapsでHP取得済みの医院だけを調査する場合は検索APIを使用しません。")
        try:
            # SDKの暗黙retryを避け、課金試行とローカルカウントを1対1にする。
            response = self.session.post("https://api.tavily.com/search",
                headers={"Authorization":"Bearer "+self._api_key},
                json={"query":query,"topic":"general","search_depth":"basic","auto_parameters":False,
                      "max_results":10,"include_answer":False,"include_raw_content":False,"include_images":False},
                timeout=(10,30),allow_redirects=False)
            if response.status_code!=200:
                raise SearchError(f"Tavily検索エラー（HTTP {response.status_code}）。キー・残数・接続を確認してください。")
            data = response.json()
            if not isinstance(data,dict) or not isinstance(data.get("results"),list):
                raise SearchError("Tavilyの応答形式を確認できません。")
            # APIの自由な応答やエラー全文をDB/ログに保存しない。
            return [{k:str(r.get(k,"") or "")[:20000] for k in ["url","title","content"]} for r in data["results"][:10] if isinstance(r,dict)]
        except SearchError:
            raise
        except (requests.RequestException,ValueError,TypeError):
            raise SearchError("Tavilyに接続できませんでした。通信を確認して再開してください。") from None


class CachedSearch:
    def __init__(self,store,provider,job_id=None,monthly_limit=None,max_searches=100):
        self.store,self.provider,self.job_id = store,provider,job_id
        self.monthly_limit = int(monthly_limit if monthly_limit is not None else store.setting("monthly_limit",900))
        self.max_searches = int(max_searches)
        self.used = 0

    def search(self,query,force=False):
        key = hashlib.sha256(("tavily-basic-v1:"+query).encode()).hexdigest()
        month = today_japan().strftime("%Y-%m")
        with self.store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            cached = c.execute("SELECT result_json FROM search_cache WHERE query_key=?",(key,)).fetchone()
            same_job = self.job_id and c.execute("SELECT 1 FROM search_usage u JOIN search_cache s USING(query_key) WHERE u.job_id=? AND u.query_key=? AND s.searched_at>=u.attempted_at LIMIT 1",(self.job_id,key)).fetchone()
            if cached and (not force or same_job):
                return json.loads(cached[0])
            used = c.execute("SELECT count(*) FROM search_usage WHERE month=?",(month,)).fetchone()[0]
            reserve = self.store.setting("external_usage_reserve",0)
            monthly = min(self.monthly_limit,int(self.store.setting("monthly_limit",900)))
            if used+int(reserve)>=monthly:
                raise BudgetReached("月間検索上限です。残りの医院を保存して停止しました。")
            if self.job_id:
                job = c.execute("SELECT * FROM research_jobs WHERE id=?",(self.job_id,)).fetchone()
                if not job or job["search_count"]>=job["max_searches"]:
                    raise BudgetReached("今回の検索上限です。上限を変更すると続きから再開できます。")
                c.execute("UPDATE research_jobs SET search_count=search_count+1 WHERE id=?",(self.job_id,))
            elif self.used>=self.max_searches:
                raise BudgetReached("今回の検索上限です。")
            c.execute("INSERT INTO search_usage(month,job_id,query_key,attempted_at) VALUES(?,?,?,?)",(month,self.job_id,key,now()))
        # ネットワーク中にDBをロックしない。失敗も安全側で消費1回として残す。
        self.used += 1
        result = self.provider.search(query)
        with self.store.connect() as c:
            c.execute("INSERT OR REPLACE INTO search_cache VALUES(?,?,?,?)",(key,query,dumps(result),now()))
        return result

    def monthly_usage(self):
        with self.store.connect() as c:
            return c.execute("SELECT count(*) FROM search_usage WHERE month=?",(today_japan().strftime("%Y-%m"),)).fetchone()[0]
