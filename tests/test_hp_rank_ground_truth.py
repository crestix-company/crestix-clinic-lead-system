"""HPランクGround Truthのappend-onlyマージ・新holdout選定のテスト。

Ground Truthそのもの（医院名・住所等）はここではテストデータとして
使わず、合成のmedical_key・仮名だけを使う。
"""
from src.scoring.hp_rank_ground_truth import GroundTruthRow, merge_ground_truth, select_new_holdout, _holdout_bucket


def gt(medical_key, name="X", rank="B", source="initial_300"):
    return GroundTruthRow(medical_key=medical_key, clinic_name=name, website_url="https://x.example/",
                           human_rank=rank, source=source)


def test_existing_rows_are_never_mutated_or_dropped():
    existing = [gt("mk-1", rank="B"), gt("mk-2", rank="C")]
    new = [gt("mk-3", rank="D", source="phase2.2_active_learning")]
    result = merge_ground_truth(existing, new)
    assert existing[0].human_rank == "B"  # 参照そのものも変わっていない
    assert existing[1].human_rank == "C"
    assert len(result["merged"]) == 3
    assert result["conflicts"] == []


def test_duplicate_medical_key_against_existing_is_flagged_not_merged():
    existing = [gt("mk-1", rank="B")]
    new = [gt("mk-1", rank="D", source="phase2.2_active_learning")]  # 同じ医院を別ランクで再評価
    result = merge_ground_truth(existing, new)
    assert len(result["merged"]) == 1  # 新規側は追加されない
    assert result["merged"][0].human_rank == "B"  # 既存は不変
    assert len(result["conflicts"]) == 1
    assert result["conflicts"][0]["new"].human_rank == "D"
    assert result["conflicts"][0]["existing"][0].human_rank == "B"


def test_duplicate_within_new_batch_is_also_flagged():
    existing = []
    new = [gt("mk-5", rank="C"), gt("mk-5", rank="D")]
    result = merge_ground_truth(existing, new)
    assert len(result["merged"]) == 1
    assert len(result["conflicts"]) == 1


def test_phase21_review_queue_105_are_all_conflicts_against_existing_300():
    # Phase2.1のreview queue105件は、実際には既存300 Ground Truthのサブセット
    # だったため、素朴にmergeすればこの通り全件conflictになることを固定する。
    existing = [gt(f"mk-{i}", rank="B") for i in range(300)]
    new = [gt(f"mk-{i}", rank="D", source="phase2.2_active_learning") for i in range(105)]
    result = merge_ground_truth(existing, new)
    assert len(result["conflicts"]) == 105
    assert len(result["merged"]) == 300  # 新規側は1件も追加されない


def test_holdout_bucket_is_deterministic():
    assert _holdout_bucket("mk-abc") == _holdout_bucket("mk-abc")


def test_select_new_holdout_does_not_depend_on_machine_fields():
    # GroundTruthRowにはmachine_rank等のフィールドが無い
    # （=holdout選定関数にそもそも渡せない設計）ことを確認する。
    rows = [gt(f"mk-{i}") for i in range(200)]
    result = select_new_holdout(rows, holdout_fraction=0.2)
    assert not hasattr(rows[0], "machine_rank")
    assert not hasattr(rows[0], "priority")
    total = len(result["train"]) + len(result["holdout"])
    assert total == 200
    # 20%前後（ハッシュの偏りで多少ずれてもよい）
    assert 20 <= len(result["holdout"]) <= 60


def test_select_new_holdout_is_reproducible_regardless_of_row_order():
    rows = [gt(f"mk-{i}") for i in range(50)]
    result_a = select_new_holdout(rows)
    result_b = select_new_holdout(list(reversed(rows)))
    keys_a = {r.medical_key for r in result_a["holdout"]}
    keys_b = {r.medical_key for r in result_b["holdout"]}
    assert keys_a == keys_b
