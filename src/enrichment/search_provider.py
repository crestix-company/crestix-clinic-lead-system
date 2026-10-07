"""Tavily basicだけを使う交換可能なアダプター。全試行を予約して上限を守る。"""
from typing import Protocol
import hashlib
import os
import requests
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
        # Stage4-D Gate2: persistent WRITE goes through the Repository (same SQL, same single
        # BEGIN IMMEDIATE transaction for the budget check + reservation -- see
        # src.repository.sqlite_write_adapter.SqliteSearchWriteRepository.check_cache_or_reserve).
        from src.repository.write_backend import write_repositories_for
        repo = write_repositories_for(self.store).search
        key = hashlib.sha256(("tavily-basic-v1:"+query).encode()).hexdigest()
        month = today_japan().strftime("%Y-%m")
        outcome,cached_result = repo.check_cache_or_reserve(
            key,self.job_id,month,self.monthly_limit,self.max_searches,self.used,force
        )
        if outcome=="cached":
            return cached_result
        # ネットワーク中にDBをロックしない。失敗も安全側で消費1回として残す。
        self.used += 1
        result = self.provider.search(query)
        repo.store_cache_result(key,query,result)
        return result

    def monthly_usage(self):
        from src.repository.write_backend import write_repositories_for
        return write_repositories_for(self.store).search.monthly_usage_count(today_japan().strftime("%Y-%m"))
