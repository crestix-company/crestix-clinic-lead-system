"""Google AI Overview(SerpAPI経由)をcandidate discoveryとしてだけ使う、隔離されたAdapter。

重要な前提(ユーザー要件):
- AI Overviewが返るケースは少数派であり、返らないケースは正常系。
  「返らなかった」をエラーや検索失敗として扱わない(status=ABSENT。例外もNO_EMAILも出さない)。
- AI Overviewから見つかったメールは、他のメール取得経路とは完全に別のobject/statusで
  管理する(専用adapterとして隔離)。VERIFIED_EMAILには一切しない。
  VERIFIED判定はsrc.enrichment.email_research.candidate_verifierの責務。
- このAdapterが例外を投げることは無い(provider側のSearchErrorはここで吸収しstatus=ERRORにする)。
  呼び出し側(pipeline)の他のステータス判定を汚染しない。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.enrichment.email_research.email_extraction import _candidates_from_text, is_valid_email, normalize_email_text
from src.enrichment.email_research.search_candidate_adapter import is_third_party_domain
from src.enrichment.search_provider import SearchError

ADAPTER_VERSION = "ai-overview-adapter-0.1.0"

STATUS_PRESENT = "PRESENT"
STATUS_ABSENT = "ABSENT"  # 正常系。AI Overviewがそもそも無かった。
STATUS_ERROR = "ERROR"    # provider呼び出し自体が失敗した(隔離されたエラー)。


@dataclass(frozen=True)
class AIOverviewEmailCandidate:
    email: str
    snippet_text: str
    matched_query: str
    third_party_suspected: bool
    origin: str = "AI_OVERVIEW"


@dataclass
class AIOverviewResult:
    status: str
    matched_query: str = ""
    candidates: list[AIOverviewEmailCandidate] = field(default_factory=list)
    error: str = ""
    adapter_version: str = ADAPTER_VERSION


def _walk_text_fragments(node, out: list[str]) -> None:
    """ai_overviewペイロードの形は版によって揺れるため、文字列値だけを再帰的に集める。

    キー名を前提にせず全文字列ノードを対象にすることで、snippet/text/title等の
    フィールド名が変わっても抽出が壊れない(多少冗長に集めるが、メール抽出自体は
    正規表現マッチなので無関係な文字列が混ざっても誤検出にはならない)。
    """
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for value in node.values():
            _walk_text_fragments(value, out)
    elif isinstance(node, list):
        for item in node:
            _walk_text_fragments(item, out)


def extract_candidate_emails_from_ai_overview(payload: dict | None, matched_query: str) -> list[AIOverviewEmailCandidate]:
    """ai_overview辞書から、文字列として明示的に存在するメールだけを抽出する(推測生成はしない)。"""
    if not isinstance(payload, dict) or not payload:
        return []
    fragments: list[str] = []
    _walk_text_fragments(payload, fragments)
    combined = " ".join(fragments)

    candidates: list[AIOverviewEmailCandidate] = []
    seen: set[str] = set()
    for raw_email in _candidates_from_text(combined):
        normalized = normalize_email_text(raw_email)
        if not is_valid_email(normalized):
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        domain = normalized.rsplit("@", 1)[-1] if "@" in normalized else ""
        candidates.append(AIOverviewEmailCandidate(
            email=normalized, snippet_text=combined[:2000], matched_query=matched_query,
            third_party_suspected=is_third_party_domain(domain),
        ))
    return candidates


class AIOverviewAdapter:
    """search_raw(query)を持つproviderだけがAI Overviewを取得できる(無ければ常にABSENT)。"""

    def __init__(self, provider):
        self.provider = provider

    def run(self, query: str) -> AIOverviewResult:
        fetcher = getattr(self.provider, "search_raw", None)
        if fetcher is None:
            return AIOverviewResult(status=STATUS_ABSENT, matched_query=query)
        try:
            payload = fetcher(query)
        except SearchError as exc:
            return AIOverviewResult(status=STATUS_ERROR, matched_query=query, error=str(exc))

        ai_overview = payload.get("ai_overview") if isinstance(payload, dict) else None
        if not ai_overview:
            return AIOverviewResult(status=STATUS_ABSENT, matched_query=query)

        candidates = extract_candidate_emails_from_ai_overview(ai_overview, query)
        return AIOverviewResult(status=STATUS_PRESENT, matched_query=query, candidates=candidates)
