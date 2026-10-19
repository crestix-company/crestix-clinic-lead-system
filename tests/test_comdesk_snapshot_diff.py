from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from src.master.comdesk_snapshot import (
    parse_comdesk_clinic_export,
    parse_comdesk_sales_export,
    plan_comdesk_snapshot_pair,
    read_snapshot_csv,
    snapshot_batch_key,
    snapshot_time_from_filenames,
)


UUID = "cd-uuid-001"
MASTER = {"clinic_id": 41, "common_clinic_id": 9, "canonical_comdesk_uuid": UUID,
          "clinic_name": "匿名医院", "address": "東京都千代田区1-2", "phone": "03-1234-5678"}


def inputs(*, history="受付時間を確認", progress="未対応", projects="内科\n内視鏡"):
    sales = [{"uuid": UUID, "clinic_name": "匿名医院", "address": "東京都千代田区１－２",
              "phone": "03 1234 5678", "progress": progress, "last_call_at": "2026-10-19",
              "last_call_by": "匿名担当", "call_count": "2", "projects": projects}]
    clinic = [{"uuid": UUID, "clinic_name": "匿名医院", "address": "東京都千代田区1-2",
               "phone": "0312345678", "history": history}]
    return sales, clinic


def plan(sales, clinic, old=None, *, at="2026-10-19T09:00:00+09:00", key="batch-1", master=None):
    return plan_comdesk_snapshot_pair(sales, clinic, {UUID: master or MASTER}, old or {},
                                     source_snapshot_at=at, source_batch_key=key)[0]


def test_initial_and_identical_snapshot_and_multiline_memberships_are_idempotent():
    sales, clinic = inputs()
    first = plan(sales, clinic)
    assert (first.action, first.history_action) == ("INSERT", "INITIAL")
    assert first.project_keys == ("内科", "内視鏡")
    old = {9: {"clinic_id": 41, "comdesk_uuid": UUID, "sales_state": dict(first.sales_state),
              "project_keys": first.project_keys, "history_text": first.history_text,
              "source_snapshot_at": first.source_snapshot_at}}
    same = plan(sales, clinic, old, key="batch-2", at="2026-10-19T09:01:00+09:00")
    assert (same.action, same.history_action) == ("UNCHANGED", "UNCHANGED")


def test_changed_free_text_and_sales_state_are_snapshots_not_call_events():
    sales, clinic = inputs()
    first = plan(sales, clinic)
    old = {9: {"clinic_id": 41, "comdesk_uuid": UUID, "sales_state": dict(first.sales_state),
              "project_keys": first.project_keys, "history_text": first.history_text,
              "source_snapshot_at": first.source_snapshot_at}}
    new_sales, new_clinic = inputs(history="受付時間を確認\n院長不在", progress="対応中")
    changed = plan(new_sales, new_clinic, old, key="batch-2", at="2026-10-19T10:00:00+09:00")
    assert (changed.action, changed.history_action) == ("UPDATE", "CHANGE")
    assert changed.history_text.endswith("院長不在")
    assert not hasattr(changed, "call_events")


def test_uuid_assigned_later_reuses_same_internal_ids_without_generating_uuid():
    sales, clinic = inputs()
    # The clinic has no Comdesk UUID yet. It is linked to its existing IDs only
    # after a unique, normalized identity match; the UUID comes from the CSV.
    later = plan(sales, clinic, master=dict(MASTER, canonical_comdesk_uuid=""))
    assert (later.clinic_id, later.common_clinic_id, later.comdesk_uuid) == (41, 9, UUID)
    no_uuid_sales, no_uuid_clinic = inputs()
    no_uuid_sales[0]["uuid"] = no_uuid_clinic[0]["uuid"] = ""
    held = plan_comdesk_snapshot_pair(no_uuid_sales, no_uuid_clinic, {}, {},
                                      source_snapshot_at="2026-10-19T09:00:00+09:00",
                                      source_batch_key="batch-no-uuid")
    assert all(row.action == "HOLD" and row.comdesk_uuid == "" for row in held)


def test_mismatch_duplicate_and_ambiguous_identity_are_held():
    sales, clinic = inputs()
    wrong = [dict(clinic[0], phone="03-9999-0000")]
    assert plan(sales, wrong).action == "HOLD"
    assert plan(sales + sales, clinic).action == "HOLD"
    insufficient = dict(MASTER, clinic_name="", address="", phone="03-0000-0000")
    assert plan(sales, clinic, master=insufficient).action == "HOLD"


def test_stale_timestamp_does_not_leak_comparison_state_between_rows():
    sales, clinic = inputs()
    first = plan(sales, clinic)
    old = {9: {"clinic_id": 41, "comdesk_uuid": UUID, "sales_state": dict(first.sales_state),
              "project_keys": first.project_keys, "history_text": first.history_text,
              "source_snapshot_at": "2026-10-19T09:30:00+09:00"}}
    result = plan(sales, clinic, old, at="2026-10-19T09:00:00+09:00")
    assert result.action == "HOLD"
    assert "古い" in result.reason


def test_csv_reader_requires_observed_30_column_header_mapping(tmp_path):
    headers = ["UUID", "医院名", "住所", "電話", *[f"col{i}" for i in range(26)]]
    path = tmp_path / "anonymized.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=headers)
        writer.writeheader()
        writer.writerow({"UUID": UUID, "医院名": "匿名医院", "住所": "匿名住所", "電話": "000"})
    mapping = {"uuid": "UUID", "clinic_name": "医院名", "address": "住所", "phone": "電話"}
    assert read_snapshot_csv(path, mapping, encoding="utf-8-sig")[0]["uuid"] == UUID
    short = tmp_path / "short.csv"
    with short.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=headers[:-1])
        writer.writeheader()
    with pytest.raises(ValueError, match="30-column"):
        read_snapshot_csv(short, mapping, encoding="utf-8")


def test_batch_key_is_stable_for_same_pair_and_sensitive_to_each_file():
    a = "a" * 64
    b = "b" * 64
    assert snapshot_batch_key(a, b) == snapshot_batch_key(a, b)
    assert snapshot_batch_key(a, b) != snapshot_batch_key(a, "c" * 64)


def test_1411_synthetic_paired_rows_join_without_duplicate_common_clinics():
    sales_rows, clinic_rows, uuid_map = [], [], {}
    for number in range(1411):
        uid = f"anonymous-comdesk-uuid-{number:04d}"
        common_id, clinic_id = number + 1, number + 10001
        name, address, phone = f"匿名医院{number}", f"匿名住所{number}", f"000{number:08d}"
        sales_rows.append({"uuid": uid, "clinic_name": name, "address": address, "phone": phone,
                           "projects": "内科\n内視鏡", "progress": "未対応"})
        clinic_rows.append({"uuid": uid, "clinic_name": name, "address": address, "phone": phone,
                            "history": ""})
        uuid_map[uid] = {"clinic_id": clinic_id, "common_clinic_id": common_id,
                         "canonical_comdesk_uuid": uid, "clinic_name": name,
                         "address": address, "phone": phone}
    plans = plan_comdesk_snapshot_pair(sales_rows, clinic_rows, uuid_map, {},
                                       source_snapshot_at="2026-10-19T09:00:00+09:00",
                                       source_batch_key="synthetic-1411")
    assert len(plans) == 1411
    assert all(p.action == "INSERT" for p in plans)
    assert len({p.common_clinic_id for p in plans}) == 1411


def test_migration_limits_snapshot_updates_to_non_identity_columns():
    migration = Path("supabase/migrations/20261019183211_comdesk_crm_snapshot_diff.sql").read_text()
    assert "grant select, insert on public.sales_entity_comdesk_crm_snapshots" in migration
    assert "grant update (progress, last_call_at, last_call_by, call_count, project_keys," in migration
    assert "grant select, insert, update on public.sales_entity_comdesk_crm_snapshots" not in migration
    assert "alter table public.sales_entity_comdesk_crm_snapshots enable row level security" in migration
    assert "force row level security" in migration
    assert "create role clinic_comdesk_snapshot_runtime" in migration
    assert "grant clinic_comdesk_snapshot_runtime to clinic_runtime" in migration
    assert "grant update (progress, last_call_at, last_call_by, call_count, project_keys," in migration
    assert "clinic_id, comdesk_uuid" not in migration.split("grant update (progress", 1)[1].split("on public.sales_entity_comdesk_crm_snapshots", 1)[0]
    assert "truncate" not in migration.casefold()
    assert "grant delete" not in migration.casefold()
    assert "sales_entity_identity_audit_id_seq" in migration


def _csv_bytes(headers, rows, *, leading_blank=False):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    if leading_blank:
        writer.writerow([])
    writer.writerow(headers)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def test_real_positional_contract_handles_bom_leading_blank_tab_and_multiline_values():
    sales_headers = [f"sales-{i}" for i in range(30)]
    for index, label in {0: "UUID", 2: "進捗", 4: "最終架電日", 5: "最終架電者",
                         9: "コール数", 12: "フィルタープロジェクト名",
                         13: "プロジェクト\t", 18: "履歴"}.items():
        sales_headers[index] = label
    sales_row = [""] * 30
    sales_row[0], sales_row[2], sales_row[4], sales_row[5] = UUID, "対応中", "2026/10/19", "匿名担当"
    sales_row[9], sales_row[12], sales_row[13], sales_row[18] = "3", "内科", "内科\n内視鏡", "履歴\n追加行"
    clinic_headers = [f"clinic-{i}" for i in range(30)]
    for index, label in {0: "UUID", 2: "名前", 6: "住所１", 9: "Tel1",
                         18: "履歴", 26: "院長名", 29: "メールアドレス"}.items():
        clinic_headers[index] = label
    clinic_row = [""] * 30
    clinic_row[0], clinic_row[2], clinic_row[6], clinic_row[9] = UUID, "匿名医院", "匿名住所", "000-000-0000"
    clinic_row[18], clinic_row[26], clinic_row[29] = "履歴\n追加行", "匿名院長", "private@example.invalid"
    sales = parse_comdesk_sales_export(_csv_bytes(sales_headers, [sales_row], leading_blank=True),
                                       filename="データ_20261019182717.csv")
    clinics = parse_comdesk_clinic_export(_csv_bytes(clinic_headers, [clinic_row], leading_blank=True),
                                          filename="エクスポート_20261019182722.csv")
    assert sales[0]["uuid"] == clinics[0]["uuid"] == UUID
    assert sales[0]["projects"] == "内科\n内視鏡"
    assert sales[0]["history"] == clinics[0]["history"] == "履歴\n追加行"
    assert "manager_name" not in clinics[0] and "email" not in clinics[0]
    assert snapshot_time_from_filenames("データ_20261019182717.csv", "エクスポート_20261019182722.csv") == "2026-10-19T18:27:22+09:00"


def test_sales_export_does_not_need_identity_columns_and_projects_can_change_without_history_event():
    sales, clinic = inputs()
    sales[0].pop("clinic_name")
    sales[0].pop("address")
    sales[0].pop("phone")
    first = plan(sales, clinic)
    old = {9: {"clinic_id": 41, "comdesk_uuid": UUID, "sales_state": dict(first.sales_state),
              "project_keys": first.project_keys, "history_text": first.history_text,
              "source_snapshot_at": first.source_snapshot_at}}
    new_sales, new_clinic = inputs(projects="内科\n内視鏡\n健診")
    for field in ("clinic_name", "address", "phone"):
        new_sales[0].pop(field)
    changed = plan(new_sales, new_clinic, old, key="batch-2", at="2026-10-19T10:00:00+09:00")
    assert changed.action == "UPDATE"
    assert changed.project_keys == ("内科", "内視鏡", "健診")
    assert changed.history_action == "UNCHANGED"


def test_empty_history_cell_does_not_erase_known_snapshot():
    sales, clinic = inputs(history="")
    first = plan(sales, clinic)
    old = {9: {"clinic_id": 41, "comdesk_uuid": UUID, "sales_state": dict(first.sales_state),
              "project_keys": first.project_keys, "history_text": "既存履歴",
              "source_snapshot_at": first.source_snapshot_at}}
    next_plan = plan(sales, clinic, old, key="batch-2", at="2026-10-19T10:00:00+09:00")
    assert next_plan.history_text == "既存履歴"
    assert next_plan.history_action == "UNCHANGED"


def test_ui_and_runtime_adapter_keep_snapshot_import_opt_in_and_separate():
    app = Path("app_v2.py").read_text()
    adapter = Path("src/repository/supabase_write_adapter.py").read_text()
    service = Path("src/master/comdesk_snapshot_import.py").read_text()
    assert "render_comdesk_snapshot_diff_import(store, demo, key_prefix=key_prefix)" in app
    assert "CLINIC_COMDESK_SNAPSHOT_DIFF_ENABLED" in app
    assert "def preview_comdesk_snapshot_pair" in adapter
    assert "def import_comdesk_snapshot_pair" in adapter
    assert "environment != \"development\"" in service
    assert "current_user != \"clinic_runtime\"" in service
