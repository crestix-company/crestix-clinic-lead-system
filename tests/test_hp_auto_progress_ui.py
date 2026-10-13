"""_render_auto_hp_progress()のメトリクス表示を検証するUIテスト。"""
from streamlit.testing.v1 import AppTest


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
    "current": {"DONE": 211, "PENDING": 287, "RUNNING": 2},
    "initial_count": 5742,
    "processed": 2711,
    "total_remaining": 3031,
    "current_done": 211,
    "current_pending": 287,
    "current_running": 2,
    "current_total": 500,
    "cumulative": {"SUCCESS": 2212, "NOT_FOUND": 54, "REVIEW": 283, "ERROR": 162},
    "estimated_total": 12,
    "new_eligible_count": 7537,
    "last_updated": "2026-10-13T12:34:59+00:00",
}
app_v2._render_auto_hp_progress(snapshot)
"""

_SCRIPT_OVER_PROCESSED = _SCRIPT.replace(
    '"processed": 2711,\n    "total_remaining": 3031,',
    '"processed": 6000,\n    "total_remaining": 0,',
)


def test_render_shows_correct_processed_and_remaining_not_zero_bug():
    app = AppTest.from_string(_SCRIPT)
    app.run()
    assert not app.exception

    metrics = {m.label: m.value for m in app.metric}
    assert metrics["開始時対象"] == "5,742件"
    assert metrics["処理済み"] == "2,711件"
    assert metrics["残り"] == "3,031件"
    assert metrics["Batch"] == "6 / 推定12"
    assert metrics["成功"] == "2,212件"
    assert metrics["未発見"] == "54件"
    assert metrics["要確認"] == "283件"
    assert metrics["エラー"] == "162件"

    captions = [c.value for c in app.caption]
    assert any("新規調査候補 7,537件" in c for c in captions)
    assert any("残り」にも混ぜていません" in c for c in captions)


def test_render_does_not_break_when_processed_exceeds_initial_count():
    app = AppTest.from_string(_SCRIPT_OVER_PROCESSED)
    app.run()
    assert not app.exception

    metrics = {m.label: m.value for m in app.metric}
    assert metrics["処理済み"] == "6,000件"
    assert metrics["残り"] == "0件"
