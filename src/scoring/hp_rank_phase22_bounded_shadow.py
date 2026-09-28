"""Aggregation helpers for a bounded, label-free Phase2.2 shadow run."""

import hashlib
from collections import Counter

from src.scoring.hp_rank_phase22 import RANKS
from src.scoring.hp_rank_phase22_shadow import ShadowValidationError


ORDER = {rank: index for index, rank in enumerate(RANKS)}


def deterministic_sample(rows, limit=500):
    if not 1 <= limit <= 500:
        raise ValueError("sample limit must be between 1 and 500")
    return sorted(
        rows,
        key=lambda row: hashlib.sha256(row["medical_key"].encode("utf-8")).hexdigest(),
    )[:limit]


def aggregate_agreement(rows):
    evaluated = [row for row in rows if row.get("prediction_status") == "OK"]
    buckets = Counter()
    current_distribution = Counter()
    shadow_distribution = Counter()
    ab_to_cd = cd_to_ab = a_to_d = d_to_a = 0
    for row in evaluated:
        current = row.get("current_rank")
        shadow = row.get("shadow_rank")
        if current not in RANKS or shadow not in RANKS:
            raise ShadowValidationError("agreement row contains unknown rank")
        distance = abs(ORDER[current] - ORDER[shadow])
        buckets[distance] += 1
        current_distribution[current] += 1
        shadow_distribution[shadow] += 1
        if current in ("A", "B") and shadow in ("C", "D"):
            ab_to_cd += 1
        if current in ("C", "D") and shadow in ("A", "B"):
            cd_to_ab += 1
        a_to_d += int(current == "A" and shadow == "D")
        d_to_a += int(current == "D" and shadow == "A")
    feature_missing = sum(row.get("prediction_status") == "REVIEW:FEATURE_MISSING" for row in rows)
    return {
        "eligible": len(rows),
        "evaluated": len(evaluated),
        "skipped": len(rows) - len(evaluated),
        "feature_missing": feature_missing,
        "network_calls": 0,
        "same": buckets[0],
        "one_step_difference": buckets[1],
        "two_step_difference": buckets[2],
        "three_step_difference": buckets[3],
        "ab_to_cd": ab_to_cd,
        "cd_to_ab": cd_to_ab,
        "a_to_d": a_to_d,
        "d_to_a": d_to_a,
        "current_distribution": {rank: current_distribution[rank] for rank in RANKS},
        "shadow_distribution": {rank: shadow_distribution[rank] for rank in RANKS},
    }


def build_review_queue(rows, limit=50):
    if not 1 <= limit <= 50:
        raise ValueError("review queue limit must be between 1 and 50")
    queue = []
    for row in rows:
        current = row.get("current_rank")
        shadow = row.get("shadow_rank")
        status = row.get("prediction_status")
        confidence = row.get("shadow_score")
        if status != "OK":
            priority, reason = 4, "feature_missing"
        elif {current, shadow} == {"A", "D"}:
            priority, reason = 1, "A_D_transition"
        elif (current in ("A", "B")) != (shadow in ("A", "B")):
            priority, reason = 2, "AB_CD_transition"
        elif current != shadow and isinstance(confidence, (int, float)) and confidence >= 0.8:
            priority, reason = 3, "high_confidence_disagreement"
        else:
            continue
        queue.append({**row, "review_priority": priority, "review_reason": reason})
    queue.sort(key=lambda row: (
        row["review_priority"],
        -(row.get("shadow_score") if isinstance(row.get("shadow_score"), (int, float)) else -1.0),
        row["medical_key"],
    ))
    return queue[:limit]
