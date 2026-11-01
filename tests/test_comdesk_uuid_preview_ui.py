import csv
import io
from pathlib import Path

from src.comdesk_uuid_preview_ui import classify_rows, summarize
from src.io.input_loader import load_table


HEADERS = [
    "UUID", "種別", "名前", "カナ", "郵便番号", "都道府県", "住所１", "住所２",
    "住所カナ", "Tel1", "Tel2", "Tel3", "Tel4", "FAX", "URL", "備考", "旧社名",
    "リードソース", "履歴", "記事名", "休診日", "診療日", "午前始", "午前終",
    "午後始", "午後終", "院長名", "開業日", "案件ID", "プロジェクト名",
]


def _row(uid, name, phone, address, case="", project=""):
    row = [""] * 30
    row[0], row[2], row[5], row[6], row[9], row[28], row[29] = (
        uid, name, "東京都", address, phone, case, project
    )
    return row


def _table(rows, headers=HEADERS):
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(headers)
    writer.writerows(rows)
    return load_table(out.getvalue().encode("utf-8-sig"), "synthetic.csv")


def _clinic(clinic_id, uuid="", name="青空クリニック", phone="03-1111-2222", address="千代田区丸の内1-1",
            comdesk_uuid=""):
    return {"id": clinic_id, "uuid": uuid, "comdesk_uuid": comdesk_uuid,
            "clinic_name": name, "phone": phone,
            "address": address, "prefecture": "東京都", "merged_into": None}


def test_synthetic_categories_keep_comdesk_and_clinic_uuids_separate():
    clinics = [
        _clinic(1, "CLINIC-1"),
        _clinic(2, "CLINIC-2", name="既存リンク医院", phone="03-2222-3333", address="港区芝1-1",
                comdesk_uuid="CD-ALREADY"),
        _clinic(3, "CLINIC-3", name="UUID矛盾医院", phone="03-3333-4444", address="新宿区西新宿1-1",
                comdesk_uuid="CD-OLD"),
        _clinic(4, name="曖昧医院", phone="03-4444-5555", address="渋谷区渋谷1-1"),
        _clinic(5, name="曖昧医院", phone="03-4444-5555", address="渋谷区渋谷1-1"),
        _clinic(6, "CLINIC-6", name="別医院", phone="03-6666-7777", address="品川区大井1-1",
                comdesk_uuid="CD-BOUND"),
    ]
    rows = [
        _row("CD-NEW", "青空クリニック", "03-1111-2222", "千代田区丸の内1-1", "CASE-1", "プロジェクトA"),
        _row("CD-ALREADY", "既存リンク医院", "03-2222-3333", "港区芝1-1", "CASE-2", "プロジェクトA"),
        _row("CD-NEW-CONFLICT", "UUID矛盾医院", "03-3333-4444", "新宿区西新宿1-1", "CASE-3", "プロジェクトA"),
        _row("CD-AMB", "曖昧医院", "03-4444-5555", "渋谷区渋谷1-1", "CASE-4", "プロジェクトA"),
        _row("CD-NONE", "存在しない医院", "03-9999-9999", "台東区浅草1-1", "CASE-5", "プロジェクトA"),
        _row("CD-DUP", "青空クリニック", "03-1111-2222", "千代田区丸の内1-1", "CASE-6", "プロジェクトA"),
        _row("CD-DUP", "青空クリニック", "03-1111-2222", "千代田区丸の内1-1", "CASE-6", "プロジェクトA"),
        _row("CD-DUP", "青空クリニック", "03-1111-2222", "千代田区丸の内1-1", "CASE-7", "プロジェクトA"),
        _row("CD-BOUND", "青空クリニック", "03-1111-2222", "千代田区丸の内1-1", "CASE-8", "プロジェクトA"),
    ]
    result = classify_rows(_table(rows), clinics)
    assert summarize(result) == {
        "MATCHED_UNIQUE": 3, "ALREADY_LINKED": 1, "AMBIGUOUS": 1,
        "UNMATCHED": 1, "ID_CONFLICT": 2, "DUPLICATE": 1,
        "same_name_phone_candidate_groups": 1, "same_name_phone_candidate_rows": 5,
    }
    assert result[0]["clinic_uuid"] == "CLINIC-1"
    assert result[0]["comdesk_uuid"] == "CD-NEW"
    assert result[0]["clinic_uuid"] != result[0]["comdesk_uuid"]
    assert result[0]["project_case_status"] == "PREVIEW_ONLY"
    assert result[5]["project_case_status"] == "PREVIEW_ONLY"
    assert result[7]["status"] == "MATCHED_UNIQUE"  # same clinic, distinct case remains separate
    assert result[5]["same_name_phone_candidate"] is True


def test_missing_project_and_case_ids_holds_case_registration_but_still_previews_clinic_match():
    table = _table([_row("CD-1", "青空クリニック", "03-1111-2222", "千代田区丸の内1-1")])
    result = classify_rows(table, [_clinic(1)])
    assert result[0]["status"] == "MATCHED_UNIQUE"
    assert result[0]["project_case_status"] == "HOLD_NO_PROJECT_OR_CASE_ID"


def test_uploaded_rows_are_retained_and_original_rows_are_stable():
    rows = [_row("CD-1", "青空クリニック", "03-1111-2222", "千代田区丸の内1-1", "CASE-A", "P-A"),
            _row("CD-2", "青空クリニック", "03-1111-2222", "千代田区丸の内1-1", "CASE-B", "P-A")]
    result = classify_rows(_table(rows), [_clinic(1)])
    assert len(result) == 2
    assert [r["source_row"] for r in result] == [2, 3]
    assert [r["raw_row"][28] for r in result] == ["CASE-A", "CASE-B"]


def test_preview_reimport_is_deterministic_and_keeps_distinct_cases_for_one_clinic():
    rows = [_row("CD-1", "青空クリニック", "03-1111-2222", "千代田区丸の内1-1", "CASE-A", "P-A"),
            _row("CD-2", "青空クリニック", "03-1111-2222", "千代田区丸の内1-1", "CASE-B", "P-A")]
    table = _table(rows)
    clinics = [_clinic(1, "CLINIC-1")]
    first = classify_rows(table, clinics)
    second = classify_rows(table, clinics)
    assert first == second
    assert [row["comdesk_case_id"] for row in first] == ["CASE-A", "CASE-B"]
    assert [row["clinic_uuid"] for row in first] == ["CLINIC-1", "CLINIC-1"]
    assert all(row["comdesk_uuid"] != row["clinic_uuid"] for row in first)


def test_project_id_is_a_distinct_scope_and_is_not_treated_as_a_case_id():
    headers = [*HEADERS]
    headers[29] = "プロジェクトID"
    table = _table([_row("CD-1", "青空クリニック", "03-1111-2222", "千代田区丸の内1-1", "CASE-A", "PROJECT-9")], headers)
    result = classify_rows(table, [_clinic(1, "CLINIC-1")])[0]
    assert result["project_scope"] == "PROJECT-9"
    assert result["project_scope_source"] == "CSV_PROJECT_COLUMN"
    assert result["comdesk_case_id"] == "CASE-A"
    assert result["project_case_status"] == "PREVIEW_ONLY"


def test_40309_row_30_column_synthetic_csv_preserves_case_ids_and_source_rows():
    rows = [
        _row(f"CD-{index}", "合成医院", "03-1111-2222", "千代田区丸の内1-1",
             f"CASE-{index}", "PROJECT-SYNTH")
        for index in range(40309)
    ]
    plans = classify_rows(_table(rows), [_clinic(1, "CLINIC-SYNTH", name="合成医院")])
    assert len(plans) == 40309
    assert plans[0]["source_row"] == 2
    assert plans[-1]["source_row"] == 40310
    assert plans[0]["comdesk_case_id"] == "CASE-0"
    assert plans[-1]["comdesk_case_id"] == "CASE-40308"
    assert all(plan["clinic_uuid"] == "CLINIC-SYNTH" for plan in (plans[0], plans[-1]))


def test_app_route_is_opt_in_and_uses_synthetic_master_without_runtime_store(monkeypatch):
    from streamlit.testing.v1 import AppTest
    import src.repository.write_backend as write_backend

    writer_calls = []

    def forbidden_writer(*args, **kwargs):
        writer_calls.append((args, kwargs))
        raise AssertionError("read-only Comdesk preview must never construct a writer")

    monkeypatch.setattr(write_backend, "write_repositories_for", forbidden_writer)

    monkeypatch.setenv("CLINIC_COMDESK_UUID_PREVIEW_ENABLED", "1")
    monkeypatch.setenv("CLINIC_READ_ONLY_UI", "1")
    monkeypatch.delenv("K_SERVICE", raising=False)
    app = AppTest.from_file(str(Path(__file__).parents[1] / "app_v2.py"), default_timeout=20)
    app.session_state["navigation"] = "Comdesk UUID照合プレビュー"
    app.run()
    assert not app.exception
    assert any("Comdesk CSV・Clinic UUID照合プレビュー" in e.value for e in app.header)
    assert any("合成Clinic Master（実DB未接続）" in e.value for e in app.caption)
    csv_text = io.StringIO(newline="")
    writer = csv.writer(csv_text)
    writer.writerow(HEADERS)
    writer.writerow(_row("CD-SYNTH", "未登録医院", "03-8888-9999", "中央区1-1"))
    app.file_uploader[0].upload("synthetic.csv", csv_text.getvalue().encode("utf-8-sig"), "text/csv")
    app.run()
    assert not app.exception
    assert any("営業案件の自動登録はHOLD" in str(element.value) for element in app.warning), {
        "warnings": [e.value for e in app.warning], "errors": [e.value for e in app.error],
        "info": [e.value for e in app.info], "upload": app.file_uploader[0].value,
    }
    assert len(app.dataframe) == 1
    assert writer_calls == []
