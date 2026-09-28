"""HPランク改善Phase2.1: C/D専用D detector候補（未採用）のテスト。

学習240件・検証60件（実HTML再取得、1回だけ適用）での結果:
  train: D_recall 0%->48.4%, D_precision 42.9%
  val  : D_recall 0%->22.2%, D_precision 20.0%
Phase2のcache由来特徴（copyright年）は検証で0/9だったのに対し、汎化は
確認できたが絶対値はまだ弱く、本番採用はまだ推奨しない、という結論を
コード側でも固定する。
"""
import sqlite3
import pytest
from src.scoring.hp_rank_d_detector import d_detector_features, d_detector_vote_count, is_d_candidate
from src.scoring.hp_rank_feedback import (
    init_feedback_schema, record_machine_rank, record_manual_override, latest_feedback, MachineRankEvent,
)

STRONG_D_SIGNAL = {
    "clinic_photo_many": False, "doctor_photo_candidate": False,
    "html_byte_size": 30000, "phone_cta": False,
}
STRONG_C_SIGNAL = {
    "clinic_photo_many": True, "doctor_photo_candidate": True,
    "html_byte_size": 200000, "phone_cta": True,
}


def test_all_weak_signals_vote_for_d():
    assert d_detector_vote_count(STRONG_D_SIGNAL) == 4
    assert is_d_candidate(STRONG_D_SIGNAL) is True


def test_all_strong_signals_vote_against_d():
    assert d_detector_vote_count(STRONG_C_SIGNAL) == 0
    assert is_d_candidate(STRONG_C_SIGNAL) is False


def test_boundary_at_min_votes():
    two_of_four = {**STRONG_C_SIGNAL, "clinic_photo_many": False, "doctor_photo_candidate": False}
    three_of_four = {**two_of_four, "phone_cta": False}
    assert d_detector_vote_count(two_of_four) == 2
    assert is_d_candidate(two_of_four) is False
    assert d_detector_vote_count(three_of_four) == 3
    assert is_d_candidate(three_of_four) is True


def test_custom_min_votes_threshold():
    two_of_four = {**STRONG_C_SIGNAL, "clinic_photo_many": False, "doctor_photo_candidate": False}
    assert is_d_candidate(two_of_four, min_votes=2) is True
    assert is_d_candidate(two_of_four, min_votes=4) is False


def test_missing_feature_key_raises_clear_error_not_silent_wrong_answer():
    # 特徴抽出が失敗/一部欠損した場合、静かに間違った判定をするのではなく
    # KeyErrorとして明示的に落ちることを確認する（呼び出し側でfetch失敗を
    # 判定対象から除外する運用を強制するため）。
    incomplete = {"clinic_photo_many": True, "doctor_photo_candidate": True}
    with pytest.raises(KeyError):
        d_detector_vote_count(incomplete)


def test_d_detector_features_breakdown():
    breakdown = d_detector_features(STRONG_D_SIGNAL)
    assert breakdown == {
        "not_many_photos": True, "no_doctor_photo": True,
        "small_html": True, "no_phone_cta": True,
    }


def test_d_detector_output_flows_through_feedback_system_and_preserves_manual_override(tmp_path):
    # STEP9: 新特徴量モデル(D detector)のmachine_rankでもmachine/manual/final/model_versionの
    # 分離とmanual_rank保護が維持されることを確認する。
    conn = sqlite3.connect(tmp_path / "feedback.sqlite3")
    conn.row_factory = sqlite3.Row
    init_feedback_schema(conn)
    mk = "東京都:医科:9999999"
    machine_rank = "D" if is_d_candidate(STRONG_D_SIGNAL) else "C"
    record_machine_rank(conn, MachineRankEvent(
        medical_key=mk, machine_rank=machine_rank, model_version="hp-content-2.1-d-detector-candidate",
        features=d_detector_features(STRONG_D_SIGNAL),
    ))
    conn.commit()
    fb = latest_feedback(conn, mk)
    assert fb["machine_rank"] == "D"
    assert fb["final_rank"] == "D"
    assert fb["model_version"] == "hp-content-2.1-d-detector-candidate"

    # 人間がCへ修正した後、D detectorを再実行してもmanual_rankは消えない。
    record_manual_override(conn, mk, manual_rank="C", reviewer="is-reader-3", reason="写真は少ないが実際はC相当")
    conn.commit()
    record_machine_rank(conn, MachineRankEvent(
        medical_key=mk, machine_rank="D", model_version="hp-content-2.1-d-detector-candidate-v2",
        features=d_detector_features(STRONG_D_SIGNAL),
    ))
    conn.commit()
    fb2 = latest_feedback(conn, mk)
    assert fb2["machine_rank"] == "D"
    assert fb2["manual_rank"] == "C"
    assert fb2["final_rank"] == "C"
    conn.close()
