"""Issue #130: tests for src.master.maps_collection_tasks -- the pure,
database-free task lifecycle for the Cloud Run Google Maps Collection
management screen. Every test here uses synthetic data only (plain dicts,
fake store objects) -- no SQLite, no Supabase, no network, no real Google
Maps interaction of any kind, per Issue #130's explicit requirement that
this orchestration layer be testable without a live database."""
import pytest

from src.master.maps_collection_tasks import (
    ASSIGNED, COMPLETED, FAILED, ON_HOLD, PENDING,
    TaskTransitionError,
    assign_task, build_collection_candidates, complete_task,
    covered_clinic_ids, create_task, export_task_queue_rows,
    fail_task, get_task, hold_task, pc_slot_active_task,
    release_task, resume_task,
)


def _make_task(**overrides):
    task = create_task([], "東京都", [1, 2, 3], timestamp="2026-10-20T00:00:00+00:00")
    task.update(overrides)
    return task


def test_create_task_is_pending_and_unassigned():
    task = create_task([], "東京都", [3, 1, 2], timestamp="t0")
    assert task["status"] == PENDING
    assert task["pc_slot"] is None
    assert task["clinic_ids"] == [1, 2, 3]  # sorted, deduped
    assert task["created_at"] == "t0"


def test_create_task_rejects_empty_clinic_list():
    with pytest.raises(TaskTransitionError):
        create_task([], "東京都", [], timestamp="t0")


def test_create_task_rejects_overlap_with_pending_task():
    first = create_task([], "東京都", [1, 2], timestamp="t0")
    with pytest.raises(TaskTransitionError):
        create_task([first], "東京都", [2, 3], timestamp="t1")


def test_create_task_rejects_overlap_with_completed_task():
    completed = _make_task(status=COMPLETED, clinic_ids=[1, 2])
    with pytest.raises(TaskTransitionError):
        create_task([completed], "東京都", [2, 5], timestamp="t1")


def test_create_task_rejects_overlap_with_failed_task():
    """Failed clinics stay covered; retry must reuse the failed task."""
    failed = _make_task(status=FAILED, clinic_ids=[1, 2])
    with pytest.raises(TaskTransitionError):
        create_task([failed], "東京都", [1, 2], timestamp="t1")


def test_covered_clinic_ids_includes_failed_by_default():
    tasks = [
        _make_task(status=PENDING, clinic_ids=[1]),
        _make_task(status=ASSIGNED, clinic_ids=[2]),
        _make_task(status=ON_HOLD, clinic_ids=[3]),
        _make_task(status=COMPLETED, clinic_ids=[4]),
        _make_task(status=FAILED, clinic_ids=[5]),
    ]
    assert covered_clinic_ids(tasks) == {1, 2, 3, 4, 5}


def test_failed_task_retry_reuses_same_covered_clinic_ids_after_release():
    failed = _make_task(status=FAILED, clinic_ids=[1, 2])
    with pytest.raises(TaskTransitionError):
        create_task([failed], "東京都", [1, 2], timestamp="t1")
    released = release_task([failed], failed["id"], timestamp="t2")
    assert covered_clinic_ids(released) == {1, 2}
    assert released[0]["id"] == failed["id"]
    assert released[0]["status"] == PENDING
    with pytest.raises(TaskTransitionError):
        create_task(released, "東京都", [1, 2], timestamp="t3")


def test_assign_task_moves_pending_to_assigned_with_pc_slot():
    task = _make_task(status=PENDING)
    tasks = assign_task([task], task["id"], "PC1", timestamp="t1")
    updated = get_task(tasks, task["id"])
    assert updated["status"] == ASSIGNED
    assert updated["pc_slot"] == "PC1"
    assert updated["assigned_at"] == "t1"


def test_assign_task_rejects_invalid_pc_slot():
    task = _make_task(status=PENDING)
    with pytest.raises(TaskTransitionError):
        assign_task([task], task["id"], "PC4", timestamp="t1")


def test_assign_task_rejects_non_pending_task():
    task = _make_task(status=COMPLETED)
    with pytest.raises(TaskTransitionError):
        assign_task([task], task["id"], "PC1", timestamp="t1")


def test_assign_task_rejects_pc_slot_already_active():
    """Issue #130 point 3: one active batch per PC at a time."""
    already_running = _make_task(status=ASSIGNED, pc_slot="PC1", clinic_ids=[1])
    new_task = _make_task(status=PENDING, clinic_ids=[9])
    with pytest.raises(TaskTransitionError, match="PC1"):
        assign_task([already_running, new_task], new_task["id"], "PC1", timestamp="t1")


def test_assign_task_allows_pc_slot_whose_prior_task_is_completed():
    done = _make_task(status=COMPLETED, pc_slot="PC1", clinic_ids=[1])
    new_task = _make_task(status=PENDING, clinic_ids=[9])
    tasks = assign_task([done, new_task], new_task["id"], "PC1", timestamp="t1")
    assert get_task(tasks, new_task["id"])["status"] == ASSIGNED


def test_pc_slot_active_task_finds_assigned_or_on_hold_only():
    assigned = _make_task(status=ASSIGNED, pc_slot="PC2")
    assert pc_slot_active_task([assigned], "PC2") is assigned
    held = _make_task(status=ON_HOLD, pc_slot="PC3")
    assert pc_slot_active_task([held], "PC3") is held
    done = _make_task(status=COMPLETED, pc_slot="PC1")
    assert pc_slot_active_task([done], "PC1") is None


def test_hold_task_requires_assigned_state():
    task = _make_task(status=PENDING)
    with pytest.raises(TaskTransitionError):
        hold_task([task], task["id"], "CAPTCHA発生", timestamp="t1")


def test_hold_and_resume_round_trip_keeps_same_pc_slot():
    task = _make_task(status=ASSIGNED, pc_slot="PC1")
    held = hold_task([task], task["id"], "CAPTCHA発生", timestamp="t1")
    held_task = get_task(held, task["id"])
    assert held_task["status"] == ON_HOLD
    assert held_task["note"] == "CAPTCHA発生"
    assert held_task["pc_slot"] == "PC1"
    resumed = resume_task(held, task["id"])
    resumed_task = get_task(resumed, task["id"])
    assert resumed_task["status"] == ASSIGNED
    assert resumed_task["pc_slot"] == "PC1"


def test_fail_task_from_assigned_or_on_hold():
    assigned = _make_task(status=ASSIGNED)
    failed = fail_task([assigned], assigned["id"], "操作者が中断", timestamp="t1")
    assert get_task(failed, assigned["id"])["status"] == FAILED

    held = _make_task(status=ON_HOLD)
    failed2 = fail_task([held], held["id"], "長時間CAPTCHA", timestamp="t1")
    assert get_task(failed2, held["id"])["status"] == FAILED


def test_fail_task_rejects_pending_or_completed():
    for status in (PENDING, COMPLETED):
        task = _make_task(status=status)
        with pytest.raises(TaskTransitionError):
            fail_task([task], task["id"], "note", timestamp="t1")


def test_release_task_clears_pc_slot_and_returns_to_pending():
    """Issue #130: interrupted-processing / reassignment path."""
    task = _make_task(status=FAILED, pc_slot="PC1", clinic_ids=[1, 2])
    released = release_task([task], task["id"], timestamp="t2")
    updated = get_task(released, task["id"])
    assert updated["status"] == PENDING
    assert updated["pc_slot"] is None
    assert updated["assigned_at"] is None
    assert updated["released_at"] == "t2"
    assert updated["clinic_ids"] == [1, 2]  # same clinics, now reassignable


def test_released_task_can_then_be_assigned_to_a_different_pc():
    task = _make_task(status=ON_HOLD, pc_slot="PC1")
    released = release_task([task], task["id"], timestamp="t2")
    reassigned = assign_task(released, task["id"], "PC3", timestamp="t3")
    assert get_task(reassigned, task["id"])["pc_slot"] == "PC3"


def test_release_task_rejects_pending_or_assigned():
    for status in (PENDING, ASSIGNED):
        task = _make_task(status=status)
        with pytest.raises(TaskTransitionError):
            release_task([task], task["id"], timestamp="t1")


def test_complete_task_requires_sufficient_clinic_overlap():
    """Issue #130 point 8: refuse to link a result CSV to a task it
    doesn't actually belong to."""
    task = _make_task(status=ASSIGNED, clinic_ids=[1, 2, 3])
    with pytest.raises(TaskTransitionError):
        complete_task([task], task["id"], imported_clinic_ids=[99, 98], result_batch_id="key1", timestamp="t1")


def test_complete_task_succeeds_with_overlap():
    task = _make_task(status=ASSIGNED, clinic_ids=[1, 2, 3])
    completed = complete_task([task], task["id"], imported_clinic_ids=[2, 3, 50], result_batch_id="key1", timestamp="t1")
    updated = get_task(completed, task["id"])
    assert updated["status"] == COMPLETED
    assert updated["result_batch_id"] == "key1"
    assert updated["completed_at"] == "t1"


def test_complete_task_works_from_on_hold_too():
    task = _make_task(status=ON_HOLD, clinic_ids=[1, 2])
    completed = complete_task([task], task["id"], imported_clinic_ids=[1], result_batch_id="key2", timestamp="t1")
    assert get_task(completed, task["id"])["status"] == COMPLETED


def test_complete_task_rejects_pending_or_completed():
    for status in (PENDING, COMPLETED):
        task = _make_task(status=status, clinic_ids=[1])
        with pytest.raises(TaskTransitionError):
            complete_task([task], task["id"], imported_clinic_ids=[1], result_batch_id="key", timestamp="t1")


def test_get_task_raises_for_unknown_id():
    with pytest.raises(TaskTransitionError):
        get_task([], "does-not-exist")


class _FakeStore:
    """Minimal stand-in for the real store's .query()/.get() -- plain dicts,
    no database. Exactly the kind of synthetic data Issue #130 asks this
    module to be testable with."""

    def __init__(self, records):
        self._records = {r["id"]: r for r in records}

    def query(self, filters, limit=100, offset=0, as_of=None):
        return list(self._records.values())[:limit]

    def get(self, clinic_id):
        if clinic_id not in self._records:
            raise ValueError("医院が見つかりません。")
        return self._records[clinic_id]


def test_build_collection_candidates_excludes_hospitals_and_centers():
    store = _FakeStore([
        {"id": 1, "clinic_name": "山田クリニック", "facility_type": "診療所"},
        {"id": 2, "clinic_name": "〇〇病院", "facility_type": "病院"},
        {"id": 3, "clinic_name": "△△センター", "facility_type": "診療所"},
    ])
    candidates = build_collection_candidates(store, "東京都", 10)
    assert candidates == [1]


def test_build_collection_candidates_excludes_already_maps_collected():
    store = _FakeStore([
        {"id": 1, "clinic_name": "A", "facility_type": "診療所", "maps_presence_status": "MAPS_MATCHED_WEBSITE"},
        {"id": 2, "clinic_name": "B", "facility_type": "診療所", "maps_presence_status": ""},
    ])
    candidates = build_collection_candidates(store, "東京都", 10)
    assert candidates == [2]


def test_build_collection_candidates_excludes_explicitly_excluded_ids():
    store = _FakeStore([
        {"id": 1, "clinic_name": "A", "facility_type": "診療所"},
        {"id": 2, "clinic_name": "B", "facility_type": "診療所"},
    ])
    candidates = build_collection_candidates(store, "東京都", 10, exclude_clinic_ids=[1])
    assert candidates == [2]


def test_build_collection_candidates_stops_at_requested_count():
    store = _FakeStore([
        {"id": i, "clinic_name": f"clinic{i}", "facility_type": "診療所"} for i in range(1, 11)
    ])
    candidates = build_collection_candidates(store, "東京都", 3)
    assert len(candidates) == 3


def test_export_task_queue_rows_produces_queue_headers_for_task_clinics_only():
    from src.master.google_maps import QUEUE_HEADERS

    store = _FakeStore([
        {"id": 1, "clinic_name": "A", "phone": "03-0000-0001", "address": "東京都千代田区1",
         "prefecture": "東京都", "facility_type": "診療所", "status": "active", "medical_institution_number": ""},
        {"id": 2, "clinic_name": "B", "phone": "03-0000-0002", "address": "東京都千代田区2",
         "prefecture": "東京都", "facility_type": "診療所", "status": "active", "medical_institution_number": ""},
        {"id": 3, "clinic_name": "C (not in task)", "phone": "", "address": "",
         "prefecture": "東京都", "facility_type": "診療所", "status": "active", "medical_institution_number": ""},
    ])
    task = _make_task(clinic_ids=[1, 2])
    csv_bytes = export_task_queue_rows(store, task)
    text = csv_bytes.decode("utf-8-sig")
    assert text.splitlines()[0].split(",") == QUEUE_HEADERS
    assert "A" in text and "B" in text
    assert "C (not in task)" not in text


def test_export_task_queue_rows_skips_a_clinic_id_that_no_longer_exists():
    store = _FakeStore([
        {"id": 1, "clinic_name": "A", "phone": "", "address": "", "prefecture": "東京都",
         "facility_type": "診療所", "status": "active", "medical_institution_number": ""},
    ])
    task = _make_task(clinic_ids=[1, 999])  # 999 does not exist
    csv_bytes = export_task_queue_rows(store, task)
    text = csv_bytes.decode("utf-8-sig")
    assert "A" in text
    assert len(text.splitlines()) == 2  # header + the one existing row
