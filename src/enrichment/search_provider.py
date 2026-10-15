"""Tavily basicだけを使う交換可能なアダプター。全試行を予約して上限を守る。"""
from typing import Protocol
import hashlib
import os
import time
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

class SerpApiSearchProvider:
    """SerpAPI(Google Search engine)だけを使う交換可能なアダプター。

    TavilySearchProviderと同じ Protocol(SearchProvider.search(query)->list[dict]) を満たす。
    既存のTavilySearchProvider/CachedSearchは変更しない(並存させるだけ)。
    """
    ENDPOINT = "https://serpapi.com/search.json"

    # Hardening Phase: Request Metering。既存artifactのsearch_cache.jsonが記録している「486」は
    # 成功response数(=キャッシュヒットも含めた論理的な検索結果数)であり、総HTTP attempt数ではない。
    # この区別は変更しない(既存artifactの意味を変更しない)。ここで追加するのは、以後の実行で
    # attempted/success/failed/http_4xx/http_5xx/timeoutを内部で数えられるようにするための
    # 薄いmetering機能のみ(デフォルトでは何も出力/送信しない。呼び出し側がprovider.metricsを読む)。
    METRIC_KEYS = (
        "query_count", "attempted", "success", "failed", "http_4xx", "http_5xx", "timeout",
        "connect_timeout", "read_timeout", "network_error", "terminal_timeout",
        "retry_count", "retry_recovery", "retry_timeout", "retry_network", "retry_5xx",
        "rate_limit", "retry_429",
    )

    def __init__(self, api_key=None, session=None, hl="ja", gl="jp", num=10, *,
                 connect_timeout=10, read_timeout=60, max_retries=2,
                 backoff_base_seconds=0.5, sleep_fn=time.sleep,
                 retry_rate_limit=False):
        # None means "use normal environment configuration".  An explicitly
        # supplied empty string means "disabled" and must never fall back to an
        # ambient key, because that could turn an offline/test invocation live.
        self._api_key = os.getenv("SERPAPI_API_KEY", "") if api_key is None else api_key
        self.session = session or requests.Session()
        self.hl, self.gl, self.num = hl, gl, num
        self.connect_timeout, self.read_timeout = connect_timeout, read_timeout
        self.max_retries = max(0, int(max_retries))
        self.backoff_base_seconds = max(0.0, float(backoff_base_seconds))
        self.sleep_fn = sleep_fn
        self.retry_rate_limit = bool(retry_rate_limit)
        self.metrics = {key: 0 for key in self.METRIC_KEYS}

    def _record(self, key):
        self.metrics[key] = self.metrics.get(key, 0) + 1

    def _request(self, params):
        if not self._api_key:
            raise SearchError("SerpAPIキーを入力してください(SERPAPI_API_KEY)。")
        self._record("query_count")
        retries = 0
        retries_5xx = 0

        def retry(reason):
            nonlocal retries
            if retries >= self.max_retries:
                return False
            self._record("retry_count")
            self._record(f"retry_{reason}")
            delay = self.backoff_base_seconds * (2 ** retries)
            retries += 1
            if delay:
                self.sleep_fn(delay)
            return True

        while True:
            self._record("attempted")
            try:
                response = self.session.get(
                    self.ENDPOINT,
                    params={"api_key": self._api_key, "engine": "google", "hl": self.hl,
                            "gl": self.gl, "num": self.num, **params},
                    timeout=(self.connect_timeout, self.read_timeout),
                )
            except requests.ConnectTimeout:
                self._record("timeout"); self._record("connect_timeout")
                if retry("timeout"):
                    continue
                self._record("terminal_timeout"); self._record("failed")
                raise SearchError("SerpAPI接続タイムアウト。") from None
            except requests.ReadTimeout:
                self._record("timeout"); self._record("read_timeout")
                if retry("timeout"):
                    continue
                self._record("terminal_timeout"); self._record("failed")
                raise SearchError("SerpAPI読込タイムアウト。") from None
            except requests.Timeout:
                self._record("timeout"); self._record("read_timeout")
                if retry("timeout"):
                    continue
                self._record("terminal_timeout"); self._record("failed")
                raise SearchError("SerpAPIタイムアウト。") from None
            except requests.RequestException:
                self._record("network_error")
                if retry("network"):
                    continue
                self._record("failed")
                raise SearchError("SerpAPIネットワークエラー。") from None

            if response.status_code != 200:
                if 400 <= response.status_code < 500:
                    self._record("http_4xx")
                    if response.status_code == 429:
                        self._record("rate_limit")
                        headers = getattr(response, "headers", {}) or {}
                        raw_retry_after = str(headers.get("Retry-After", headers.get("retry-after", ""))).strip()
                        try:
                            retry_after = int(raw_retry_after)
                        except (TypeError, ValueError):
                            retry_after = None
                        if (self.retry_rate_limit and retry_after is not None
                                and 0 <= retry_after <= 30
                                and retries < self.max_retries):
                            self._record("retry_count")
                            self._record("retry_429")
                            retries += 1
                            if retry_after:
                                self.sleep_fn(retry_after)
                            continue
                elif 500 <= response.status_code < 600:
                    self._record("http_5xx")
                    if retries_5xx < 1 and retries < self.max_retries:
                        retries_5xx += 1
                        if retry("5xx"):
                            continue
                self._record("failed")
                raise SearchError(f"SerpAPI検索エラー（HTTP {response.status_code}）。キー・残数・接続を確認してください。")
            try:
                data = response.json()
            except (ValueError, TypeError):
                self._record("failed")
                raise SearchError("SerpAPIの応答形式を確認できません。") from None
            if not isinstance(data, dict):
                self._record("failed"); raise SearchError("SerpAPIの応答形式を確認できません。")
            if isinstance(data.get("error"), str):
                self._record("failed"); raise SearchError(f"SerpAPIエラー: {data['error'][:200]}")
            if retries > 0:
                self._record("retry_recovery")
            self._record("success")
            return data

    def search(self, query):
        """SearchProvider Protocol準拠(url/title/content)。organic_resultsのみを対象にする。"""
        data = self._request({"q": query})
        organic = data.get("organic_results")
        if not isinstance(organic, list):
            return []
        # APIの自由な応答やエラー全文をDB/ログに保存しない。
        return [
            {"url": str(r.get("link", "") or "")[:20000],
             "title": str(r.get("title", "") or "")[:20000],
             "content": str(r.get("snippet", "") or "")[:20000]}
            for r in organic[:self.num] if isinstance(r, dict)
        ]

    def search_raw(self, query):
        """organic_results以外(ai_overview等)も必要な呼び出し元向けの、生応答に近い辞書。

        応答の構造を保ったまま返すが、不要に大きい/用途不明なキーはそのまま呼び出し元の
        責務とする(ここでは切り落とさない)。DB保存は呼び出し元が行わないこと。
        """
        return self._request({"q": query})


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
