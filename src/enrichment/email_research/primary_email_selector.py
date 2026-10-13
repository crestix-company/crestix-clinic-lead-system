"""1医院に複数のVERIFIED_EMAILが存在する場合に、営業リスト用途の代表メール
(primary_email)を原則1件だけ選ぶための選択ロジック。

設計方針(ユーザー要件に対応):
- Email Discovery(pipeline.py)やThree Gates(candidate_verifier.py v0.2.0)は一切変更しない。
  本モジュールは、既にVERIFIED_EMAILとして確定した候補群を入力として受け取り、
  「その中のどれを営業に使うか」だけを選ぶ、完全に独立した後段(sidecar)ステップ。
- 元candidateは一切削除・変更しない(呼び出し側がEmailCandidateInputの一覧を保持し続け、
  本モジュールはそのうち1件を選んだ結果(PrimarySelectionResult)だけを返す)。
- 以下は原則としてprimaryに選ばない(fail-closed):
    * status が VERIFIED_EMAIL ではない候補(SEARCH_CANDIDATE/REVIEW等。Three Gatesを
      通過していない候補を営業リストの代表メールにはしない)。
    * third_party_suspected=True の候補。
    * role が RECRUIT/RESERVATION/MEDICAL_COOPERATION/PERSONAL の候補。
    * role が UNKNOWN の候補(今回はConservative Promotionを継続し、所有権の強さに
      関わらず自動選出しない。用途が不明な代表メールを営業に出さないことを優先する)。
- 優先順位(tier、数字が小さいほど優先):
    0: GENERAL + OFFICIAL_HP
    1: OFFICE   + OFFICIAL_HP
    2: GENERAL + NAVII
    3: OFFICE   + NAVII
    4: GENERAL  + 検証済み検索ソース(SEARCH_QUERY/AI_OVERVIEW, status=VERIFIED_EMAIL)
    5: OFFICE   + 検証済み検索ソース
  同一tier内はrole_confidenceの高い候補を優先し、さらに同値ならemailの文字列順で
  決定的にtie-breakする。
"""
from __future__ import annotations

from dataclasses import dataclass

from src.enrichment.email_research.email_role_classifier import (
    ROLE_GENERAL,
    ROLE_MEDICAL_COOPERATION,
    ROLE_OFFICE,
    ROLE_PERSONAL,
    ROLE_RECRUIT,
    ROLE_RESERVATION,
    ROLE_UNKNOWN,
)

SELECTOR_VERSION = "primary-email-selector-0.1.0"

STATUS_VERIFIED_EMAIL = "VERIFIED_EMAIL"

SOURCE_OFFICIAL_HP = "OFFICIAL_HP"
SOURCE_NAVII = "NAVII"  # 予約枠(本フェーズでは未接続。実データが来ることは無い)。
_VERIFIED_SEARCH_SOURCES = frozenset({"SEARCH_QUERY", "AI_OVERVIEW"})

_NEVER_PRIMARY_ROLES = frozenset({ROLE_RECRUIT, ROLE_RESERVATION, ROLE_MEDICAL_COOPERATION, ROLE_PERSONAL})

_TIER_ORDER: tuple[tuple[str, str], ...] = (
    (ROLE_GENERAL, SOURCE_OFFICIAL_HP),
    (ROLE_OFFICE, SOURCE_OFFICIAL_HP),
    (ROLE_GENERAL, SOURCE_NAVII),
    (ROLE_OFFICE, SOURCE_NAVII),
)
_VERIFIED_SEARCH_TIER_ROLES: tuple[str, ...] = (ROLE_GENERAL, ROLE_OFFICE)


@dataclass(frozen=True)
class EmailCandidateInput:
    clinic_id: str
    clinic_name: str
    email: str
    status: str
    source_type: str
    source_url: str
    verified_on_official: bool
    third_party_suspected: bool
    email_role: str
    role_confidence: float
    sales_usable: str


@dataclass(frozen=True)
class PrimarySelectionResult:
    clinic_id: str
    clinic_name: str
    candidate_count: int
    primary_email: str
    email_role: str
    source_type: str
    source_url: str
    sales_usable: str
    primary_score: float
    selection_reason: str
    needs_review: bool
    selector_version: str = SELECTOR_VERSION


def _tier_rank(role: str, source_type: str) -> int | None:
    for idx, (tier_role, tier_source) in enumerate(_TIER_ORDER):
        if role == tier_role and source_type == tier_source:
            return idx
    if role in _VERIFIED_SEARCH_TIER_ROLES and source_type in _VERIFIED_SEARCH_SOURCES:
        return len(_TIER_ORDER) + _VERIFIED_SEARCH_TIER_ROLES.index(role)
    return None


def _is_eligible(candidate: EmailCandidateInput) -> bool:
    if candidate.status != STATUS_VERIFIED_EMAIL:
        return False
    if candidate.third_party_suspected:
        return False
    if candidate.email_role in _NEVER_PRIMARY_ROLES:
        return False
    if candidate.email_role == ROLE_UNKNOWN:
        return False
    return _tier_rank(candidate.email_role, candidate.source_type) is not None


def select_primary_email(candidates: list[EmailCandidateInput]) -> PrimarySelectionResult:
    """同一医院のcandidate群から、営業利用に最も適した1件をprimaryとして選ぶ。

    候補はここでは一切削除・変更されない(呼び出し側がcandidates自体を保持し続ける)。
    該当する候補が無ければ、primary_email=""・needs_review=Trueで返す
    (= 自動選択せずREVIEWへ回す)。
    """
    if not candidates:
        raise ValueError("candidatesが空です(呼び出し側で医院単位にグルーピングすること)")
    clinic_id, clinic_name = candidates[0].clinic_id, candidates[0].clinic_name

    eligible = [c for c in candidates if _is_eligible(c)]
    if not eligible:
        return PrimarySelectionResult(
            clinic_id=clinic_id, clinic_name=clinic_name, candidate_count=len(candidates),
            primary_email="", email_role="", source_type="", source_url="",
            sales_usable="REVIEW", primary_score=0.0,
            selection_reason="GENERAL/OFFICEの営業利用可能な候補(VERIFIED_EMAILかつ非third-party)"
                             "が無いため、自動選択せずREVIEWへ回す",
            needs_review=True,
        )

    ranked = sorted(
        eligible,
        key=lambda c: (_tier_rank(c.email_role, c.source_type), -c.role_confidence, c.email),
    )
    winner = ranked[0]
    tier = _tier_rank(winner.email_role, winner.source_type)
    primary_score = round(1.0 - tier * 0.1 + winner.role_confidence * 0.05, 4)
    return PrimarySelectionResult(
        clinic_id=clinic_id, clinic_name=clinic_name, candidate_count=len(candidates),
        primary_email=winner.email, email_role=winner.email_role, source_type=winner.source_type,
        source_url=winner.source_url, sales_usable=winner.sales_usable, primary_score=primary_score,
        selection_reason=(
            f"tier={tier}({winner.email_role}+{winner.source_type})を最優先候補として採用。"
            f"role_confidence={winner.role_confidence}"
            + (f"(同tier内{len(ranked)}件中、role_confidence最大の1件を選択)" if len(ranked) > 1 else "")
        ),
        needs_review=False,
    )
