"""Email Enrichment専用の検索プロバイダ切替(EMAIL_SEARCH_PROVIDER)。

既存 src.enrichment.search_provider の各クラスは変更しない。ここは「どのクラスを
使うか」を選ぶ薄いfactoryだけを追加する(既存エンジンの呼び出し経路には影響しない)。

EMAIL_SEARCH_PROVIDER=serpapi (デフォルト) | tavily
"""
from __future__ import annotations

import os

from src.enrichment.search_provider import SearchProvider, SerpApiSearchProvider, TavilySearchProvider

DEFAULT_PROVIDER_NAME = "serpapi"
_PROVIDER_CLASSES = {
    "serpapi": SerpApiSearchProvider,
    "tavily": TavilySearchProvider,
}


def get_email_search_provider(name: str | None = None, **kwargs) -> SearchProvider:
    """EMAIL_SEARCH_PROVIDER(未指定時はname引数、さらに未指定ならserpapi)でプロバイダを選ぶ。"""
    resolved = (name or os.getenv("EMAIL_SEARCH_PROVIDER", DEFAULT_PROVIDER_NAME) or DEFAULT_PROVIDER_NAME).strip().lower()
    cls = _PROVIDER_CLASSES.get(resolved)
    if cls is None:
        valid = ", ".join(sorted(_PROVIDER_CLASSES))
        raise ValueError(f"未知のEMAIL_SEARCH_PROVIDERです: {resolved!r}(有効な値: {valid})")
    return cls(**kwargs)
