"""HPランクGround Truthのappend-only拡張（既存300件 + 新規レビュー分）。

既存の人間評価300件は絶対に変更しない。新規レビュー分は追記だけとし、
medical_key（安定ID）で既存と重複する場合は自動で上書き・統合せず
"conflict"として分離する（同じ医院の二重Ground Truthを防止）。
"""
import hashlib
from dataclasses import dataclass, field


@dataclass
class GroundTruthRow:
    medical_key: str
    clinic_name: str
    website_url: str
    human_rank: str
    source: str  # 例: "initial_300" / "phase2.2_active_learning"
    human_memo: str = ""
    reviewed_at: str = ""
    reviewer: str = ""
    extra: dict = field(default_factory=dict)


def merge_ground_truth(existing_rows, new_rows):
    """existing_rowsは一切変更しない。new_rowsのうちmedical_keyが既存と
    重複するものはconflictsへ分離し、mergedには追加しない。

    戻り値: {"merged": [既存 + 重複しない新規], "conflicts": [重複した新規行のリスト（既存側も併記）]}
    """
    existing_by_key = {}
    for row in existing_rows:
        existing_by_key.setdefault(row.medical_key, []).append(row)

    merged = list(existing_rows)
    conflicts = []
    seen_new_keys = set()
    for row in new_rows:
        if row.medical_key in existing_by_key:
            conflicts.append({"new": row, "existing": existing_by_key[row.medical_key]})
            continue
        if row.medical_key in seen_new_keys:
            conflicts.append({"new": row, "existing": [r for r in new_rows if r.medical_key == row.medical_key and r is not row]})
            continue
        seen_new_keys.add(row.medical_key)
        merged.append(row)
    return {"merged": merged, "conflicts": conflicts}


def _holdout_bucket(medical_key, buckets=100):
    """medical_keyのハッシュ値だけで決まる決定的な0-99の整数。

    machine_rank/machine_score/review_priority等、機械判定に一切依存しない
    （新holdoutの選定基準を「D detector等で選ばれたものを避ける」ため）。
    """
    digest = hashlib.sha256(medical_key.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % buckets


def select_new_holdout(rows, holdout_fraction=0.2, buckets=100):
    """medical_keyのハッシュだけを基準にholdoutを選ぶ。machine側の情報は使わない。

    同じmedical_keyは常に同じ側（train/holdout）に決定的に入る
    （再実行・行の追加順序が変わっても再現可能）。
    """
    threshold = int(buckets * holdout_fraction)
    holdout = [r for r in rows if _holdout_bucket(r.medical_key, buckets) < threshold]
    train = [r for r in rows if _holdout_bucket(r.medical_key, buckets) >= threshold]
    return {"train": train, "holdout": holdout}
