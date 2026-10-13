"""HP Auto Research進捗KPI（開始時対象/処理済み/残り）の表示計算テスト。

Production調査で判明したバグ: remaining_count()はpublic.clinics全体への
ライブ再検索であり、Run開始後に新たにeligibleになった医院まで「残り」に
混入する。結果として total_remaining > initial_count となり、
processed = max(0, initial_count - total_remaining) が0にクランプされる。

修正方針: 「処理済み」はこのRunのDONE(terminal result)累積を直接集計し、
「残り」はinitial_countからの差分のみで求める。remaining_count()のライブ値は
「新規調査候補」として別出しし、残りには混ぜない。
"""
import app_v2


class FakeJobStatus(dict):
    pass


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


def test_processed_and_remaining_match_expected_arithmetic(monkeypatch):
    # SUCCESS 2235 + NOT_FOUND 55 + REVIEW 285 + ERROR 163 = 2738 (累積5batch+現batch相当)
    # ここでは要件通りの processed=2711 を再現する内訳にする。
    cumulative = {"SUCCESS": 2212, "NOT_FOUND": 54, "REVIEW": 283, "ERROR": 162}
    assert sum(cumulative.values()) == 2711
    _patch_job_status(monkeypatch, done=211, pending=287, running=2)
    repo = FakeAutoRepo(_state(), cumulative, remaining_count=7537)

    snapshot = app_v2._auto_hp_progress_snapshot(store=None, auto_repo=repo)

    assert snapshot["initial_count"] == 5742
    assert snapshot["processed"] == 2711
    assert snapshot["total_remaining"] == 3031
    assert snapshot["new_eligible_count"] == 7537


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


def test_processed_exceeding_initial_count_does_not_break_ui(monkeypatch):
    # Auto Runの実行ロジック自体は変更しないため、advance()が母集団外の新規医院を
    # 取り込み続ければ processed が initial_count を超えることは起こり得る。
    # その場合でも例外を出さず、remainingは0にクランプされる必要がある。
    cumulative = {"SUCCESS": 5000, "NOT_FOUND": 500, "REVIEW": 300, "ERROR": 200}
    _patch_job_status(monkeypatch, done=500, pending=0, running=0)
    repo = FakeAutoRepo(_state(initial_count=5742), cumulative, remaining_count=0)

    snapshot = app_v2._auto_hp_progress_snapshot(store=None, auto_repo=repo)

    assert snapshot["processed"] == 6000
    assert snapshot["total_remaining"] == 0  # 負値にならずクランプされる

    # _render_auto_hp_progress が例外なく描画できること（AppTest側で別途検証）。
    overall_ratio = min(1.0, max(0.0, snapshot["processed"] / snapshot["initial_count"]))
    assert overall_ratio == 1.0


def test_growing_new_eligible_count_does_not_regress_processed_or_remaining(monkeypatch):
    cumulative = {"SUCCESS": 2212, "NOT_FOUND": 54, "REVIEW": 283, "ERROR": 162}
    _patch_job_status(monkeypatch, done=211, pending=287, running=2)

    snapshots = []
    for live_remaining in (7537, 7600, 9000, 50000):
        repo = FakeAutoRepo(_state(), cumulative, remaining_count=live_remaining)
        snapshots.append(app_v2._auto_hp_progress_snapshot(store=None, auto_repo=repo))

    processed_values = [s["processed"] for s in snapshots]
    remaining_values = [s["total_remaining"] for s in snapshots]
    assert processed_values == [2711, 2711, 2711, 2711]
    assert remaining_values == [3031, 3031, 3031, 3031]
    # new_eligible_count（参考KPI）だけが動いてよい。
    assert [s["new_eligible_count"] for s in snapshots] == [7537, 7600, 9000, 50000]


def test_remaining_count_live_value_is_never_mixed_into_total_remaining(monkeypatch):
    cumulative = {"SUCCESS": 1, "NOT_FOUND": 0, "REVIEW": 0, "ERROR": 0}
    _patch_job_status(monkeypatch, done=1, pending=0, running=0)
    # わざと「母集団を超える」ほど大きいライブ値を与えて混入チェック。
    repo = FakeAutoRepo(_state(initial_count=10), cumulative, remaining_count=999999)

    snapshot = app_v2._auto_hp_progress_snapshot(store=None, auto_repo=repo)

    assert snapshot["processed"] == 1
    assert snapshot["total_remaining"] == 9
    assert snapshot["total_remaining"] != 999999
