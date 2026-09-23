from dataclasses import dataclass


@dataclass
class LeadDecision:
    status: str
    rank: str
    reason: str
    include: bool


def rank_lead(*, eligible, gate_reasons, recent, young, succession, epark,
              other_media=False, hp_weak=False, options=None):
    """最終対象・要確認・除外は排他的。要確認の出力への追加は最後に明示設定。"""
    options = options or {}
    include_review = bool(options.get("include_review_in_final", False))
    def review(reason):
        return LeadDecision("要確認", "C", reason, include_review)
    if eligible is False:
        return LeadDecision("除外", "除外", " / ".join(gate_reasons), False)
    if eligible is None:
        return review(" / ".join(gate_reasons) or "医科・診療所・現在営業中の確認不足")
    if (young is False and not succession.candidate and recent is False
            and options.get("require_young", True) and options.get("require_recent", True)):
        return LeadDecision("除外", "除外", "59歳以下確率が50%未満・継承候補なし・指定から10年超", False)
    age_ok = young is True or (young is False and not options.get("require_young", True))
    recent_ok = recent is True or (recent is False and not options.get("require_recent", True))
    owner_ok = succession.owner_manager_equal is True or (
        succession.owner_manager_equal is False and not options.get("require_owner_manager", True))
    inheritance = succession.candidate and options.get("include_succession", True)
    # 不足した年齢はチェックを外しても自動で合格にしない。
    if age_ok and recent_ok and inheritance and (epark == "課金済み" or other_media):
        return LeadDecision("営業対象", "S", "若手・指定10年以内・継承候補・媒体投資の根拠あり", True)
    if age_ok and recent_ok and owner_ok:
        return LeadDecision("営業対象", "A", "通常院長型の指定期間・年齢・決裁条件を充足", True)
    if age_ok and (inheritance or epark == "課金済み"):
        rank = "B" if recent is True and (epark == "無課金" or hp_weak) else "C"
        return LeadDecision("営業対象", rank, "継承候補かつ若手" if inheritance else "若手かつEPARK課金確認済み", True)
    if succession.change_reason and young is True and options.get("include_succession", True):
        return LeadDecision("営業対象（要確認）", "C", "変更/継承記録と若手判定あり。実務決裁権を要確認（パターン4）", True)
    missing = []
    if young is None:
        missing.append("医籍登録年または本人特定が不足")
    if recent is None:
        missing.append("指定年月日が未取得・不正・未来")
    if succession.owner_manager_equal is None:
        missing.append("開設者/管理者の氏名が不足")
    if young is False:
        missing.append("年齢基準未達だが一律除外条件は未充足")
    return review(" / ".join(missing) or "自動採用条件に未該当。継承・決裁権・媒体投資の根拠を確認")
