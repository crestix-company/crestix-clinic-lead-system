"""append-onlyの全国厚生局Masterインポーター（scripts/national_append_only_import.py）の仕様テスト。

本番DBには一切触れない。すべてtmp_path上の合成DBで検証する。
"""
import csv
from datetime import date

import pytest

from src.master.store import ClinicStore
from src.master.filters import Filters
from src.master.samples import sample_records
from src.io.input_loader import load_table
from src.io.output_writer import csv_bytes
from scripts.national_append_only_import import run

TODAY = date.today().replace(day=1).isoformat()


def comdesk_table(uuid, name, phone, address):
    headers = ["UUID", "医院名", "電話番号", "住所"]
    rows = [[uuid, name, phone, address]]
    return load_table(csv_bytes(headers, rows), "legacy.csv")


@pytest.fixture
def seeded_prod(tmp_path):
    """既存本番相当のDB: sample_records()の4件（medical_keyあり）+ Comdesk単独の1件（medical_keyなし）。"""
    path = tmp_path / "prod.sqlite3"
    store = ClinicStore(path)
    store.import_master(sample_records())  # id1..4, medical_key = 東京都:医科:sampleN
    store.import_comdesk(comdesk_table("LEAD-005", "ひまわり歯科医院", "03-9999-0005", "東京都渋谷区架空町5-5-5"))  # id5, medical_keyなし

    rows = {r["clinic_name"]: r for r in store.query(Filters(active_only=False, hp_only=False), limit=10)}
    target = rows["青空内視鏡クリニック"]
    store.save_research(target["id"], {"hp_status": "VERIFIED", "hp_url": "https://example.test/", "hp_rank": "A"})

    with store.connect() as c:
        c.execute(
            "UPDATE clinics SET uuid=?, maps_website_url=?, maps_presence_status=? WHERE clinic_name=?",
            ("LEAD-002", "https://maps.example.test/", "MAPS_MATCHED_WEBSITE", "若葉眼科医院"),
        )
    return path


def write_master_csv(path, rows):
    fieldnames = ["clinic_id", "clinic_name", "address", "phone", "prefecture", "medical_type",
                  "facility_type", "owner_name", "manager_name", "designation_date",
                  "registration_reason", "status", "as_of", "source_url", "departments"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({**{k: "" for k in fieldnames}, **row})


def base_row(**overrides):
    row = {"clinic_name": "テストクリニック", "address": "東京都千代田区テスト1-1-1", "phone": "03-1111-9999",
            "prefecture": "東京都", "medical_type": "医科", "facility_type": "診療所", "status": "現存",
            "as_of": TODAY, "clinic_id": "new-1"}
    row.update(overrides)
    return row


def test_full_scenario(tmp_path, seeded_prod):
    csv_path = tmp_path / "master.csv"
    write_master_csv(csv_path, [
        # (1) 既存medical_key(sample1) と同じキー、だが名称/住所が違う更新行 -> SKIPされ、既存clinic1は不変であるべき
        base_row(clinic_id="sample-1", clinic_name="更新後の名称クリニック", address="更新後の住所", phone="03-9999-9999"),
        # 既存medical_key(sample2)も同様にSKIP対象、UUID/Maps保持を確認する
        base_row(clinic_id="sample-2", clinic_name="別名称眼科", address="別の住所", phone="03-8888-8888"),
        # (3) 新規medical_key -> INSERTされるべき
        base_row(clinic_id="new-1", clinic_name="新規クリニックA", address="東京都新宿区新規1-1-1", phone="03-2222-0001"),
        # (6a) CSV内で同じ新規medical_keyが2回出現 -> 2回目はduplicate、二重登録されない
        base_row(clinic_id="new-1", clinic_name="新規クリニックA(重複行)", address="東京都新宿区新規1-1-1", phone="03-2222-0001"),
        # (5) medical_keyが空(clinic_id空) かつ既存と一致しない -> REVIEW、INSERTされない
        base_row(clinic_id="", clinic_name="不明な医院", address="東京都不明区0-0-0", phone="03-0000-0000"),
        # (6b) medical_keyが空だが、既存のmedical_keyなしComdesk医院(id5)と電話/名称/住所が完全一致 -> duplicate、INSERTされない・既存id5も不変
        base_row(clinic_id="", clinic_name="ひまわり歯科医院", address="東京都渋谷区架空町5-5-5", phone="03-9999-0005", medical_type="歯科"),
    ])

    staging_path = tmp_path / "staging.sqlite3"
    report, duplicate_rows, review_rows = run(seeded_prod, staging_path, csv_path, test_unique_index=False)

    # --- 分類件数 ---
    assert report["existing_skip"] == 2         # sample-1, sample-2
    assert report["new_insert"] == 1             # new-1 (1回目のみ)
    assert report["duplicate"] == 2              # new-1の2回目 + ひまわり歯科医院
    assert report["review"] == 1                 # 不明な医院
    assert report["errors"] == 0
    assert report["after_total"] == report["orig_total"] + report["new_insert"]

    # --- (1)(2) 既存medical_keyは追加されない・既存clinicの全columnが不変 ---
    assert report["changed_records"] == 0
    assert report["changed_cells"] == 0

    store = ClinicStore(staging_path)
    by_name = {r["clinic_name"]: r for r in store.query(Filters(active_only=False, hp_only=False), limit=50)}

    # (2) 既存clinic1(sample-1)の名称・住所はCSVの更新行に書き換えられていない
    assert "青空内視鏡クリニック" in by_name
    assert "更新後の名称クリニック" not in by_name
    # (7) 既存のHP調査結果が保持されている
    assert by_name["青空内視鏡クリニック"]["hp_status"] == "VERIFIED"
    assert by_name["青空内視鏡クリニック"]["hp_url"] == "https://example.test/"

    # (7) 既存clinic2のUUID/Mapsが保持されている（raw columnで確認: Maps系はeffective_jsonに
    # 反映されるのはimport_google_maps経由のときのみのため、ここでは直接カラムを見る）
    assert by_name["若葉眼科医院"]["uuid"] == "LEAD-002"
    with store.connect() as c:
        maps_url = c.execute("SELECT maps_website_url FROM clinics WHERE clinic_name='若葉眼科医院'").fetchone()[0]
    assert maps_url == "https://maps.example.test/"

    # (3) 新規medical_keyの1件だけがINSERTされている
    assert "新規クリニックA" in by_name
    assert "新規クリニックA(重複行)" not in by_name
    new_row = by_name["新規クリニックA"]
    assert new_row["uuid"] == ""  # UUIDを勝手に生成しない
    assert new_row["hp_status"] == "UNRESEARCHED"
    with store.connect() as c:
        new_maps_url = c.execute("SELECT maps_website_url FROM clinics WHERE clinic_name='新規クリニックA'").fetchone()[0]
    assert new_maps_url == ""

    # (5)(6) REVIEW/duplicateはどちらも新規行としてDBに存在しない
    assert "不明な医院" not in by_name
    assert "ひまわり歯科医院" in by_name  # 既存id5のみ。CSV由来の重複行がINSERTされていないことの確認
    assert by_name["ひまわり歯科医院"]["uuid"] == "LEAD-005"  # 既存id5も不変

    # (4) 同じmedical_keyを2回importしても件数が増えない(idempotent)
    report2, _, _ = run(seeded_prod, staging_path, csv_path, test_unique_index=False, reset_staging=False)
    assert report2["new_insert"] == 0
    assert report2["after_total"] == report["after_total"]
    assert store.count(Filters(active_only=False, hp_only=False)) == report["after_total"]


def test_ambiguous_no_key_row_goes_to_review_not_duplicate(tmp_path, seeded_prod):
    # id5と同じ電話番号を持つが名称/住所が異なる2件目のno-key医院を直接INSERTし、電話一致だけでは
    # 一意に確定できない状態を作る -> REVIEWになりINSERTされないことを確認。
    # (import_comdesk経由だと、同一電話番号は本来のmatch_record()がAMBIGUOUSとして
    #  match_reviewsに退避してしまい、2件目のclinic行自体が作られないため直接INSERTする)
    from src.normalizer.phone import tel_match_key as _tel_key
    from src.normalizer.clinic_name import normalize_clinic_name as _norm_name
    from src.normalizer.address import normalize_address as _norm_addr
    from src.master.store import now as _now, dumps as _dumps

    store = ClinicStore(seeded_prod)
    name, addr, phone = "べつの歯科医院", "東京都新宿区べつの場所9-9-9", "03-9999-0005"
    with store.connect() as c:
        c.execute(
            "INSERT INTO clinics(uuid,clinic_name,phone,address,phone_norm,tel_match_key,name_norm,name_prefix,"
            "address_norm,medical_key,prefecture,base_json,effective_json,first_seen_at,last_seen_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("LEAD-006", name, phone, addr, phone, _tel_key(phone), _norm_name(name), _norm_name(name)[:2],
             _norm_addr(addr), "", "東京都", _dumps({"clinic_name": name, "phone": phone, "address": addr}),
             _dumps({"clinic_name": name, "phone": phone, "address": addr}), _now(), _now()),
        )

    csv_path = tmp_path / "master_ambiguous.csv"
    write_master_csv(csv_path, [
        base_row(clinic_id="", clinic_name="ambiguous phone row", address="東京都どこか1-1-1",
                  phone="03-9999-0005", medical_type="歯科"),
    ])
    staging_path = tmp_path / "staging_ambiguous.sqlite3"
    report, duplicate_rows, review_rows = run(seeded_prod, staging_path, csv_path, test_unique_index=False)

    assert report["new_insert"] == 0
    assert report["duplicate"] == 0
    assert report["review"] == 1
    assert len(review_rows) == 1
