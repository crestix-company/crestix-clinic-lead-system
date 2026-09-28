"""HPランクのフィードバックログ（machine_rank/manual_rank/final_rank/model_version分離）のテスト。

すべてtmp_pathの一時sqlite3接続だけを使う。本番DBには一切触れない。
"""
import sqlite3
import pytest
from src.scoring.hp_rank_feedback import (
    init_feedback_schema, record_machine_rank, record_manual_override,
    latest_feedback, resolve_final_rank, review_priority, MachineRankEvent,
)


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "feedback.sqlite3")
    c.row_factory = sqlite3.Row
    init_feedback_schema(c)
    yield c
    c.close()


def test_resolve_final_rank_prefers_manual():
    assert resolve_final_rank("B", "D") == "D"
    assert resolve_final_rank("B", None) == "B"
    assert resolve_final_rank("B", "") == "B"


def test_machine_rank_without_manual_resolves_to_machine(conn):
    record_machine_rank(conn, MachineRankEvent(
        medical_key="東京都:医科:0000001", machine_rank="B", model_version="hp-content-1",
        clinic_id=1, website_url="https://x.example/", machine_score=9, features={"HTTPS": True},
    ))
    conn.commit()
    fb = latest_feedback(conn, "東京都:医科:0000001")
    assert fb["machine_rank"] == "B"
    assert fb["manual_rank"] is None
    assert fb["final_rank"] == "B"
    assert fb["model_version"] == "hp-content-1"
    assert fb["features"] == {"HTTPS": True}


def test_manual_override_becomes_final_rank(conn):
    record_machine_rank(conn, MachineRankEvent(
        medical_key="東京都:医科:0000002", machine_rank="B", model_version="hp-content-1",
    ))
    conn.commit()
    record_manual_override(conn, "東京都:医科:0000002", manual_rank="D", reviewer="is-reader-1", reason="デザインが古い")
    conn.commit()
    fb = latest_feedback(conn, "東京都:医科:0000002")
    assert fb["manual_rank"] == "D"
    assert fb["final_rank"] == "D"
    assert fb["reviewer"] == "is-reader-1"
    assert fb["reason"] == "デザインが古い"


def test_recomputing_machine_rank_does_not_erase_manual_override(conn):
    # 「再計算してもmanual_rankを上書き・削除しない」ことの回帰テスト。
    mk = "東京都:医科:0000003"
    record_machine_rank(conn, MachineRankEvent(medical_key=mk, machine_rank="B", model_version="hp-content-1"))
    conn.commit()
    record_manual_override(conn, mk, manual_rank="C", reviewer="is-reader-2")
    conn.commit()
    # モデルを再計算した想定（新しいmodel_versionでmachine_rankが変わる）。
    record_machine_rank(conn, MachineRankEvent(medical_key=mk, machine_rank="A", model_version="hp-content-2-candidate"))
    conn.commit()

    fb = latest_feedback(conn, mk)
    assert fb["machine_rank"] == "A"  # 最新の機械判定
    assert fb["model_version"] == "hp-content-2-candidate"
    assert fb["manual_rank"] == "C"  # 人間の修正は消えていない
    assert fb["final_rank"] == "C"  # manual優先


def test_manual_override_requires_prior_machine_event(conn):
    with pytest.raises(ValueError):
        record_manual_override(conn, "存在しないkey", manual_rank="A", reviewer="x")


def test_latest_feedback_returns_none_when_unknown(conn):
    assert latest_feedback(conn, "未知のmedical_key") is None


def test_review_priority_flags_boundary_score():
    result = review_priority("C", machine_score=4, features={}, threshold=5)
    assert result["priority"] == "HIGH"
    assert "境界" in result["reason"]


def test_review_priority_flags_a_candidate():
    features = {"SNS導線": True, "独自LP・専門サイト": True}
    result = review_priority("B", machine_score=9, features=features, threshold=5)
    assert result["priority"] == "HIGH"
    assert "A候補" in result["reason"]


def test_review_priority_normal_when_nothing_notable():
    result = review_priority("B", machine_score=9, features={}, threshold=5)
    assert result["priority"] == "NORMAL"


def test_review_priority_low_when_already_reviewed():
    result = review_priority("B", machine_score=4, features={}, threshold=5, manual_rank="B")
    assert result["priority"] == "LOW"


def test_init_feedback_schema_is_idempotent(conn):
    init_feedback_schema(conn)  # 2回目も落ちない
    init_feedback_schema(conn)
