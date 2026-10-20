"""_render_auto_hp_progress()のメトリクス表示を検証するUIテスト。

「残り」は「現在残り候補」に改名した: Auto Runは固定コホートではなく、
advance()が新たにeligibleになった医院も同じRunの次Batchへ取り込むため、
この数値はRun開始時母集団の残数ではなく、今後処理する可能性のある件数を表す。
"""
from streamlit.testing.v1 import AppTest
import app_v2
import pytest


_SCRIPT = """
import app_v2

snapshot = {
    "state": {
        "status": "RUNNING",
        "prefecture": "",
        "medical_types": ["医科", "歯科"],
        "force": False,
        "batch_size": 500,
        "current_job_id": "job-6",
        "current_batch": 6,
        "completed_batches": 5,
    },
    "controller_health": "ACTIVE",
    "current": {"DONE": 211, "PENDING": 287, "RUNNING": 2},
    "initial_count": 5742,
    "processed": 2711,
    "total_remaining": 7826,
    "current_scope_total": 10537,
    "future_remaining": 7537,
    "current_done": 211,
    "current_pending": 287,
    "current_running": 2,
    "current_total": 500,
    "cumulative": {"SUCCESS": 2212, "NOT_FOUND": 54, "REVIEW": 283, "ERROR": 162},
    "estimated_total": 22,
    "last_updated": "2026-10-13T12:34:59+00:00",
}
app_v2._render_auto_hp_progress(snapshot)
"""

_SCRIPT_OVER_PROCESSED = _SCRIPT.replace(
    '"processed": 2711,\n    "total_remaining": 7826,\n    "current_scope_total": 10537,\n    "future_remaining": 7537,',
    '"processed": 6000,\n    "total_remaining": 0,\n    "current_scope_total": 6000,\n    "future_remaining": 0,',
)


def test_render_shows_dynamic_scope_labels_and_values():
    app = AppTest.from_string(_SCRIPT)
    app.run()
    assert not app.exception

    metrics = {m.label: m.value for m in app.metric}
    assert metrics["開始時対象"] == "5,742件"
    assert metrics["処理済み"] == "2,711件"
    assert metrics["現在残り候補"] == "7,826件"
    assert "残り" not in metrics  # 旧ラベルは残っていない
    assert metrics["HP取得成功"] == "2,212件"
    assert metrics["未発見"] == "54件"
    assert metrics["要確認"] == "283件"
    assert metrics["エラー"] == "162件"

    captions = [c.value for c in app.caption]
    assert any("全体進捗 2,711/10,537件" in c for c in captions)

    assert metrics["現在Batch"] == "211/500件"
    assert metrics["Batch番号"] == "6 / 推定22"
    assert metrics["次Batch以降の候補"] == "7,537件"
    # 「開始時対象には含まれず、残りにも混ぜていません」という旧説明は事実と異なるため削除済み。
    assert not any("残りにも混ぜていません" in c for c in captions)


def test_render_does_not_break_when_processed_exceeds_initial_count():
    app = AppTest.from_string(_SCRIPT_OVER_PROCESSED)
    app.run()
    assert not app.exception

    metrics = {m.label: m.value for m in app.metric}
    assert metrics["処理済み"] == "6,000件"
    assert metrics["現在残り候補"] == "0件"

    captions = [c.value for c in app.caption]
    assert any("100.0%" in c for c in captions if "全体進捗" in c)


def test_treatment_quality_count_uses_the_complete_detected_and_not_detected_cohort():
    assert app_v2.treatment_quality_completed_count({
        "treatment_detected": 31,
        "treatment_not_detected": 19,
    }) == 50


@pytest.mark.parametrize(
    ("status", "status_label"),
    [("WAITING_NETWORK", "通信回復待ち"), ("PAUSED", "一時停止"), ("COMPLETED", "完了")],
)
def test_progress_layout_renders_network_paused_and_completed_states(status, status_label):
    script = _SCRIPT.replace('"status": "RUNNING"', f'"status": "{status}"')
    script = script.replace('"current_total": 500,', '"current_total": 0,')
    app = AppTest.from_string(script).run()
    assert not app.exception
    assert {metric.label for metric in app.metric} >= {
        "開始時対象", "処理済み", "現在残り候補", "HP取得成功", "要確認", "エラー",
    }
    assert any(status_label in item.value for item in app.markdown)
