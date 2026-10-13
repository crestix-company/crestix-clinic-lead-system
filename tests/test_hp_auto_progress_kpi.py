"""HP Auto Research進捗KPI（開始時対象/処理済み/現在残り候補/Batch）の表示計算テスト。

背景: Auto Runは固定コホートではない。hp_auto_run_repository.advance()は
Batch完了のたびに_available_count(..., auto_run_id)を再実行し、その時点で
新たにeligibleになった医院も同じRunの次Batchへ取り込む（candidate selection
自体はここでは変更しない）。

そのため、以前の修正（remaining = max(0, initial_count - processed)）は
「initial_countを固定母集団」とみなしており、実装と意味が一致していなかった。
processed >= initial_count になった時点でremaining=0と表示されるのに、
Auto Runは実際には次Batchを処理し続けるという表示矛盾が発生する。

本テストが固定する仕様:
- processed = このRunのDONE(terminal result)累積の直接集計（SUCCESS/NOT_FOUND/REVIEW/ERROR）。
- current_remaining = current_pending + current_running。
- future_remaining  = auto_repo.remaining_count(state)（現在のライブ値、次Batch以降で
  実際に取り込まれ得る候補数）。
- total_remaining   = current_remaining + future_remaining（UIラベルは「現在残り候補」）。
- current_scope_total = processed + total_remaining（全体進捗率の分母）。
- initial_count は「開始時対象」として履歴参考値のまま表示し、
  processed + remaining == initial_count という前提は持たない。
- estimated_total   = current_batch + ceil(future_remaining / batch_size)。
"""
import app_v2


class FakeAutoRepo:
    def __init__(self, state, cumulative, remaining_count=0):
        self._state = state
        self._cumulative = cumulative
        self._remaining_count = remaining_count

    def get_state(self):
        return dict(self._state)

    def cumulative_results(self, run_id):
        assert run_id == self._state.get("run_id")
        return dict(self._cumulative)

    def remaining_count(self, state):
        return self._remaining_count


def _state(initial_count=5742, current_batch=6, completed_batches=5, batch_size=500):
    return {
        "status": "RUNNING",
        "run_id": "run-1",
        "prefecture": "",
        "medical_types": ["医科", "歯科"],
        "force": False,
        "batch_size": batch_size,
        "initial_count": initial_count,
        "current_job_id": "job-6",
        "current_batch": current_batch,
        "completed_batches": completed_batches,
        "last_progress_at": "2026-10-13T12:34:59+00:00",
    }


def _patch_job_status(monkeypatch, *, done=0, pending=0, running=0):
    counts = {"DONE": done, "PENDING": pending, "RUNNING": running}

    def fake_job_status(store, jid):
        return {
            "counts": counts,
            "total": sum(counts.values()),
            "updated_at": "2026-10-13T12:34:59+00:00",
        }

    monkeypatch.setattr(app_v2, "job_status", fake_job_status)


def test_dynamic_scope_arithmetic_matches_production_snapshot(monkeypatch):
    # processed=2711, current_pending=287, current_running=2, future_remaining=7537
    cumulative = {"SUCCESS": 2212, "NOT_FOUND": 54, "REVIEW": 283, "ERROR": 162}
    assert sum(cumulative.values()) == 2711
    _patch_job_status(monkeypatch, done=211, pending=287, running=2)
    repo = FakeAutoRepo(_state(), cumulative, remaining_count=7537)

    snapshot = app_v2._auto_hp_progress_snapshot(store=None, auto_repo=repo)

    assert snapshot["initial_count"] == 5742
    assert snapshot["processed"] == 2711
    assert snapshot["current_pending"] == 287
    assert snapshot["current_running"] == 2
    assert snapshot["future_remaining"] == 7537
    assert snapshot["total_remaining"] == 7826  # 289 + 7537
    assert snapshot["current_scope_total"] == 10537  # 2711 + 7826
    assert snapshot["estimated_total"] == 22  # 6 + ceil(7537/500)=6+16


def test_success_not_found_review_error_sum_equals_processed(monkeypatch):
    cumulative = {"SUCCESS": 100, "NOT_FOUND": 10, "REVIEW": 5, "ERROR": 3}
    _patch_job_status(monkeypatch, done=18, pending=0, running=0)
    repo = FakeAutoRepo(_state(initial_count=1000), cumulative, remaining_count=42)

    snapshot = app_v2._auto_hp_progress_snapshot(store=None, auto_repo=repo)

    total = (
        snapshot["cumulative"].get("SUCCESS", 0)
        + snapshot["cumulative"].get("NOT_FOUND", 0)
        + snapshot["cumulative"].get("REVIEW", 0)
        + snapshot["cumulative"].get("ERROR", 0)
    )
    assert total == snapshot["processed"]


def test_current_scope_total_equals_processed_plus_total_remaining(monkeypatch):
    cumulative = {"SUCCESS": 2212, "NOT_FOUND": 54, "REVIEW": 283, "ERROR": 162}
    _patch_job_status(monkeypatch, done=211, pending=287, running=2)
    repo = FakeAutoRepo(_state(), cumulative, remaining_count=7537)

    snapshot = app_v2._auto_hp_progress_snapshot(store=None, auto_repo=repo)

    assert snapshot["current_scope_total"] == snapshot["processed"] + snapshot["total_remaining"]


def test_processed_exceeding_initial_count_does_not_break_ui(monkeypatch):
    # Auto Runの実行ロジック自体は変更しないため、advance()が母集団外の新規医院を
    # 取り込み続ければ processed が initial_count を超えることは起こり得る。
    # この修正ではinitial_countは単なる履歴参考値であり、processed/remainingの
    # 算出には使われないため、超過しても例外は起きない。
    cumulative = {"SUCCESS": 5000, "NOT_FOUND": 500, "REVIEW": 300, "ERROR": 200}
    _patch_job_status(monkeypatch, done=500, pending=0, running=0)
    repo = FakeAutoRepo(_state(initial_count=5742), cumulative, remaining_count=0)

    snapshot = app_v2._auto_hp_progress_snapshot(store=None, auto_repo=repo)

    assert snapshot["processed"] == 6000
    assert snapshot["initial_count"] == 5742  # 履歴参考値として変化しない
    assert snapshot["total_remaining"] == 0
    assert snapshot["current_scope_total"] == 6000

    overall_ratio = min(1.0, max(0.0, snapshot["processed"] / snapshot["current_scope_total"]))
    assert overall_ratio == 1.0


def test_remaining_zero_does_not_hide_existing_future_candidates(monkeypatch):
    # remaining=0なのにfuture candidateが存在する、という矛盾を作らないことを確認する。
    # future_remaining>0であれば、必ずtotal_remaining>0になる（current側が0でも）。
    cumulative = {"SUCCESS": 1, "NOT_FOUND": 0, "REVIEW": 0, "ERROR": 0}
    _patch_job_status(monkeypatch, done=1, pending=0, running=0)
    repo = FakeAutoRepo(_state(initial_count=10), cumulative, remaining_count=500)

    snapshot = app_v2._auto_hp_progress_snapshot(store=None, auto_repo=repo)

    assert snapshot["future_remaining"] == 500
    assert snapshot["total_remaining"] > 0
    assert snapshot["total_remaining"] == snapshot["future_remaining"]


def test_growing_future_remaining_does_not_regress_processed(monkeypatch):
    cumulative = {"SUCCESS": 2212, "NOT_FOUND": 54, "REVIEW": 283, "ERROR": 162}
    _patch_job_status(monkeypatch, done=211, pending=287, running=2)

    snapshots = []
    for live_remaining in (7537, 7600, 9000, 50000):
        repo = FakeAutoRepo(_state(), cumulative, remaining_count=live_remaining)
        snapshots.append(app_v2._auto_hp_progress_snapshot(store=None, auto_repo=repo))

    processed_values = [s["processed"] for s in snapshots]
    assert processed_values == [2711, 2711, 2711, 2711]
    # future_remaining/total_remaining/current_scope_totalは増えて良い（dynamic母集団の仕様通り）。
    assert [s["future_remaining"] for s in snapshots] == [7537, 7600, 9000, 50000]
    assert [s["total_remaining"] for s in snapshots] == [7826, 7889, 9289, 50289]
    assert [s["current_scope_total"] for s in snapshots] == [10537, 10600, 12000, 53000]


def test_estimated_total_tracks_current_batch_plus_future_batches(monkeypatch):
    cumulative = {"SUCCESS": 1}
    _patch_job_status(monkeypatch, done=1, pending=0, running=0)

    # future_remaining=0 -> もう次Batchは無い -> 推定は現在Batchのまま。
    repo = FakeAutoRepo(_state(current_batch=3), cumulative, remaining_count=0)
    snapshot = app_v2._auto_hp_progress_snapshot(store=None, auto_repo=repo)
    assert snapshot["estimated_total"] == 3

    # future_remaining=500 (batch_size=500) -> ちょうど1 Batch分。
    repo = FakeAutoRepo(_state(current_batch=3), cumulative, remaining_count=500)
    snapshot = app_v2._auto_hp_progress_snapshot(store=None, auto_repo=repo)
    assert snapshot["estimated_total"] == 4

    # future_remaining=501 -> 切り上げでもう1 Batch追加。
    repo = FakeAutoRepo(_state(current_batch=3), cumulative, remaining_count=501)
    snapshot = app_v2._auto_hp_progress_snapshot(store=None, auto_repo=repo)
    assert snapshot["estimated_total"] == 5
