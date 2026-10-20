"""Issue #130: pure domain model for the Cloud Run Google Maps Collection
management screen -- task creation, PC-slot assignment, state transitions,
and the clinic-overlap checks that prevent double-collection and wrong-task
linkage. Deliberately contains NO database access and NO Google Maps
automation of any kind: this module only tracks *who is supposed to be
collecting what*, never performs or scripts the collection itself.

This is the explicit separation point Issue #130 asks for (point 10):
collection EXECUTION (today: a human manually running the existing Chrome
extension, google-maps-clinic-collector v5.7.0, completely unchanged by
this work) is entirely outside this module and this PR. If that execution
mechanism is ever replaced (e.g. an official, billed Places API client),
only how a task's queue CSV is produced and how its result CSV is consumed
would need to change -- the task lifecycle below (PENDING/ASSIGNED/ON_HOLD/
FAILED/COMPLETED, PC-slot exclusivity, clinic-overlap validation) does not
assume anything about the collection mechanism and can be reused as-is.

Every function here takes and returns plain dicts/lists (the full task list
in, a new task list out, or a raises ValueError on an invalid transition) --
no database, no Streamlit, no Supabase. This is what makes the whole state
machine testable with synthetic data only (Issue #130's explicit
requirement), independent of any repository or live connection.
"""
from __future__ import annotations

import uuid

PENDING = "PENDING"
ASSIGNED = "ASSIGNED"
ON_HOLD = "ON_HOLD"
FAILED = "FAILED"
COMPLETED = "COMPLETED"

TASK_STATUSES = (PENDING, ASSIGNED, ON_HOLD, FAILED, COMPLETED)
# Tasks whose clinic_ids are still "claimed" -- a new task must not be
# created covering any of these, since either collection work is still
# outstanding (PENDING/ASSIGNED/ON_HOLD) or already succeeded (COMPLETED).
# FAILED is deliberately excluded: a task that was explicitly given up on
# frees its clinics so a fresh task can cover them again later.
COVERING_STATUSES = frozenset({PENDING, ASSIGNED, ON_HOLD, COMPLETED})
# Tasks that occupy a PC slot -- a PC can only have one of these at a time.
PC_SLOT_ACTIVE_STATUSES = frozenset({ASSIGNED, ON_HOLD})
PC_SLOTS = ("PC1", "PC2", "PC3")


class TaskTransitionError(ValueError):
    pass


def _find(tasks, task_id):
    for task in tasks:
        if task["id"] == task_id:
            return task
    raise TaskTransitionError(f"タスクが見つかりません: {task_id}")


def get_task(tasks, task_id):
    return _find(tasks, task_id)


def covered_clinic_ids(tasks, *, statuses=COVERING_STATUSES):
    covered = set()
    for task in tasks:
        if task["status"] in statuses:
            covered.update(task["clinic_ids"])
    return covered


def pc_slot_active_task(tasks, pc_slot):
    for task in tasks:
        if task["pc_slot"] == pc_slot and task["status"] in PC_SLOT_ACTIVE_STATUSES:
            return task
    return None


def create_task(tasks, prefecture, clinic_ids, *, timestamp):
    """Returns a new task dict (PENDING, unassigned). Raises TaskTransitionError
    if any clinic_id is already covered by an active or completed task --
    this is the primary double-collection guard (Issue #130 point 8): a
    clinic can never be part of two simultaneously-live (or one live and one
    already-completed) collection tasks."""
    clinic_ids = sorted({int(cid) for cid in clinic_ids})
    if not clinic_ids:
        raise TaskTransitionError("対象医院が0件のタスクは作成できません。")
    overlap = covered_clinic_ids(tasks) & set(clinic_ids)
    if overlap:
        raise TaskTransitionError(
            f"既存タスクと重複する医院があるため作成できません（{len(overlap)}件、例: {sorted(overlap)[:5]}）。"
        )
    return {
        "id": uuid.uuid4().hex,
        "prefecture": prefecture,
        "clinic_ids": clinic_ids,
        "pc_slot": None,
        "status": PENDING,
        "created_at": timestamp,
        "assigned_at": None,
        "completed_at": None,
        "released_at": None,
        "result_batch_id": None,
        "note": "",
    }


def assign_task(tasks, task_id, pc_slot, *, timestamp):
    """PENDING -> ASSIGNED. Raises if the task isn't PENDING, the slot name
    is invalid, or the slot already has another active (ASSIGNED/ON_HOLD)
    task -- the one-active-batch-per-PC guard (Issue #130 point 3)."""
    if pc_slot not in PC_SLOTS:
        raise TaskTransitionError(f"PCスロットは{PC_SLOTS}のいずれかを指定してください。")
    task = _find(tasks, task_id)
    if task["status"] != PENDING:
        raise TaskTransitionError(f"PENDING状態のタスクのみ割り当てできます（現在: {task['status']}）。")
    existing = pc_slot_active_task(tasks, pc_slot)
    if existing is not None:
        raise TaskTransitionError(f"{pc_slot}には既に進行中のタスクがあります（タスクID: {existing['id']}）。")
    return [
        {**t, "pc_slot": pc_slot, "status": ASSIGNED, "assigned_at": timestamp} if t["id"] == task_id else t
        for t in tasks
    ]


def hold_task(tasks, task_id, note, *, timestamp):
    """ASSIGNED -> ON_HOLD. For CAPTCHA pauses and other mid-batch stops --
    the slot stays reserved for the same PC while paused (Issue #130 point 9:
    this never attempts to resolve or bypass the pause, only records it)."""
    task = _find(tasks, task_id)
    if task["status"] != ASSIGNED:
        raise TaskTransitionError(f"ASSIGNED状態のタスクのみ保留にできます（現在: {task['status']}）。")
    return [
        {**t, "status": ON_HOLD, "note": note} if t["id"] == task_id else t
        for t in tasks
    ]


def resume_task(tasks, task_id):
    """ON_HOLD -> ASSIGNED, same PC slot. For resuming after a CAPTCHA pause
    clears or any other reason collection stopped temporarily."""
    task = _find(tasks, task_id)
    if task["status"] != ON_HOLD:
        raise TaskTransitionError(f"ON_HOLD状態のタスクのみ再開できます（現在: {task['status']}）。")
    return [
        {**t, "status": ASSIGNED} if t["id"] == task_id else t
        for t in tasks
    ]


def fail_task(tasks, task_id, note, *, timestamp):
    """ASSIGNED/ON_HOLD -> FAILED. Marks the attempt given up on; the task's
    clinic_ids are NOT freed by this alone -- call release_task afterward to
    let a new task cover them again."""
    task = _find(tasks, task_id)
    if task["status"] not in (ASSIGNED, ON_HOLD):
        raise TaskTransitionError(f"ASSIGNED/ON_HOLD状態のタスクのみ失敗にできます（現在: {task['status']}）。")
    return [
        {**t, "status": FAILED, "note": note} if t["id"] == task_id else t
        for t in tasks
    ]


def release_task(tasks, task_id, *, timestamp):
    """ON_HOLD/FAILED -> PENDING, clearing the PC slot and assignment
    timestamp. This is the reassignment/interrupted-processing path (Issue
    #130 point 3/8): the same clinic_ids remain on the same task (so the
    clinics are NOT double-counted as "new" work), but the task becomes
    assignable to any PC slot again, including a different one than before."""
    task = _find(tasks, task_id)
    if task["status"] not in (ON_HOLD, FAILED):
        raise TaskTransitionError(f"ON_HOLD/FAILED状態のタスクのみ再割当てのため解放できます（現在: {task['status']}）。")
    return [
        {**t, "status": PENDING, "pc_slot": None, "assigned_at": None, "released_at": timestamp} if t["id"] == task_id else t
        for t in tasks
    ]


def complete_task(tasks, task_id, imported_clinic_ids, result_batch_id, *, timestamp, min_overlap=1):
    """ASSIGNED/ON_HOLD -> COMPLETED, after a Google Maps result CSV has
    actually been imported (via the existing, unchanged src.master.
    google_maps.import_maps_results() / cloud_maps_import_ui() Writer path --
    this function never imports anything itself, it only records that an
    import happened and links it to this task).

    Raises TaskTransitionError if the imported CSV's clinic ids don't
    sufficiently overlap this task's assigned clinic ids -- the defense
    against linking the wrong result file to the wrong task (Issue #130
    point 8's "曖昧一致防止" for task completion, not to be confused with
    src.master.maps_cloud_import.preview_maps_import()'s own AMBIGUOUS
    bucket, which is a different, clinic-matching-level concept)."""
    task = _find(tasks, task_id)
    if task["status"] not in (ASSIGNED, ON_HOLD):
        raise TaskTransitionError(f"ASSIGNED/ON_HOLD状態のタスクのみ完了にできます（現在: {task['status']}）。")
    overlap = set(int(cid) for cid in imported_clinic_ids) & set(task["clinic_ids"])
    if len(overlap) < min_overlap:
        raise TaskTransitionError(
            "アップロードされた結果に、このタスクの対象医院と一致するものがありません。"
            "タスクの選択、または取り込むファイルをご確認ください。"
        )
    return [
        {**t, "status": COMPLETED, "completed_at": timestamp, "result_batch_id": result_batch_id} if t["id"] == task_id else t
        for t in tasks
    ]


def build_collection_candidates(store, prefecture, count, *, exclude_clinic_ids=()):
    """Selects up to `count` clinic ids eligible for a brand-new task:
    matching `prefecture`, not already excluded as a hospital/center (same
    rule the existing queue_csv()/Collector population already applies via
    excluded_reason()), not already Maps-collected (any prior
    maps_presence_status at all), and not already covered by `exclude_clinic_ids`
    (the caller passes covered_clinic_ids(existing_tasks) here -- this
    function does not know about tasks at all, keeping it independently
    testable with just a fake store and plain dicts).

    Uses store.query(Filters(...)), the same generic read interface every
    other screen in this app already uses (both the SQLite and Supabase
    backends implement it identically) -- no new SQL.
    """
    from src.master.filters import Filters
    from src.master.google_maps import excluded_reason

    exclude = {int(cid) for cid in exclude_clinic_ids}
    records = store.query(Filters(prefectures=[prefecture] if prefecture else [], active_only=True, hp_only=False), limit=max(int(count) * 5, 500))
    candidates = []
    for record in records:
        clinic_id = int(record["id"])
        if clinic_id in exclude:
            continue
        if excluded_reason(record):
            continue
        if (record.get("maps_presence_status") or "").strip():
            continue
        candidates.append(clinic_id)
        if len(candidates) >= count:
            break
    return candidates


def export_task_queue_rows(store, task):
    """Builds the exact same QUEUE_HEADERS rows the existing
    store.google_maps_queue_csv() produces (same queue_csv() function, same
    Collector v5.7.0 input format), scoped to one task's clinic_ids -- the
    "作業指示" a human loads into the Collector on one PC (Issue #130 point 5).
    """
    from src.master.google_maps import queue_csv

    records = []
    seen = set()
    for clinic_id in task["clinic_ids"]:
        try:
            record = store.get(clinic_id)
        except ValueError:
            # Supabase's .get() raises for an id that no longer exists (e.g.
            # merged/deleted between task creation and export); skip rather
            # than failing the whole export over one stale id.
            continue
        if not record or record["id"] in seen:
            continue
        seen.add(record["id"])
        records.append(record)
    return queue_csv(records)
