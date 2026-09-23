"""厚生局内の異なる医療機関番号を誤って重複保留しないように更新する。

既存のSQLite/CSV/調査結果は変更しない。コードと関連テストだけを安全に更新する。
プロジェクト直下（app_v2.py がある場所）へ置いて実行する。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import os
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
MATCHING = ROOT / "src" / "master" / "matching.py"
TESTS = ROOT / "tests" / "test_v2_master.py"

HELPER_ANCHOR = '''    def ambiguous(pool, reason, score=90):\n        return MasterMatch("AMBIGUOUS", sorted({row["id"] for row in pool + identity}), reason, score)\n\n    def conflict(row):\n'''
HELPER_REPLACEMENT = '''    def ambiguous(pool, reason, score=90):\n        return MasterMatch("AMBIGUOUS", sorted({row["id"] for row in pool + identity}), reason, score)\n\n    def compatible_medical_identity(pool):\n        """厚生局の明示的な医療機関番号が異なる施設は、名称・住所・電話が似ていても別施設として扱う。\n\n        コムデスク由来で medical_key が未設定の候補は残すため、既存UUIDとの名寄せは従来どおり行える。\n        同じ medical_key の月次更新も候補に残る。\n        """\n        if not med:\n            return pool\n        # 同じ医療機関番号をすでに保持している月次更新では、電話等が別の\n        # 医療機関番号を指す矛盾も要確認に残す。新しい厚生局施設を初回投入\n        # するときだけ、別の明示的な医療機関番号を候補から除外する。\n        if any(row["medical_key"] == med for row in identity):\n            return pool\n        return [row for row in pool if not row["medical_key"] or row["medical_key"] == med]\n\n    def conflict(row):\n'''

OLD_TEST_BLOCK = '''def test_ambiguous_shared_phone_held_and_resolved_separate(store):\n    records = sample_records()\n    records[1]["phone"] = records[0]["phone"]\n    result = store.import_master(records[:2])\n    assert result["AMBIGUOUS"]==1 and store.count()==1\n    review = store.reviews()[0]\n    store.resolve_review(review["id"],note="同じ受付番号を共有している別医院")\n    assert store.count()==2 and not store.reviews()\n\n\ndef test_fuzzy_not_auto_merged(store):\n    r = sample_records()[0]\n    store.import_master([r])\n    r2 = {**r,"clinic_id":"new-id","clinic_name":"青空内視鏡クリニック東京","phone":"03-1111-2222","address":r["address"].replace("1-1-1","1-1-2")}\n    assert store.import_master([r2])["AMBIGUOUS"]==1\n    assert store.count()==1\n'''
NEW_TEST_BLOCK = '''def test_distinct_official_medical_ids_with_shared_phone_stay_separate(store):\n    records = sample_records()\n    records[1]["phone"] = records[0]["phone"]\n    result = store.import_master(records[:2])\n    assert result == {"MATCHED":0,"NEW":2,"AMBIGUOUS":0}\n    assert store.count()==2 and not store.reviews()\n\n\ndef test_distinct_official_medical_ids_with_similar_name_address_stay_separate(store):\n    r = sample_records()[0]\n    store.import_master([r])\n    r2 = {**r,"clinic_id":"new-id","clinic_name":"青空内視鏡クリニック東京","phone":"03-1111-2222","address":r["address"].replace("1-1-1","1-1-2")}\n    assert store.import_master([r2]) == {"MATCHED":0,"NEW":1,"AMBIGUOUS":0}\n    assert store.count()==2 and not store.reviews()\n'''

TEST_ANCHOR = '''def test_exact_name_address_matches_changed_phone(store):\n    store.import_comdesk(table())\n    record = {**sample_records()[0],"phone":"03-9999-8888"}\n    assert store.import_master([record])["MATCHED"]==1\n    assert store.query()[0]["uuid"]=="A"\n    assert load_table(store.export(Filters(hp_only=False))["final_comdesk_import.csv"],"x.csv").value(0,9)=="03-0000-0001"\n\n\n'''
TEST_ADDITION = '''def test_official_medical_id_still_matches_comdesk_without_medical_id_by_phone(store):\n    incoming = table([["A","青空内視鏡クリニック","300000001","東京都千代田区架空町1-1-1","","",""]])\n    store.import_comdesk(incoming)\n    official = {**sample_records()[0], "phone":"03-0000-0001"}\n    result = store.import_master([official])\n    assert result == {"MATCHED":1,"NEW":0,"AMBIGUOUS":0}\n    clinic = store.query(Filters(active_only=False,hp_only=False))[0]\n    assert clinic["uuid"] == "A" and clinic["clinic_id"] == official["clinic_id"]\n\n\n'''


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise ValueError(f"{label} の対応箇所が {count} 件でした。想定バージョンと異なるため更新を中止します。")
    return text.replace(old, new, 1)


def patched_matching(text: str) -> str:
    if "def compatible_medical_identity(pool):" in text:
        return text
    text = replace_once(text, HELPER_ANCHOR, HELPER_REPLACEMENT, "医療機関番号保護ロジック")
    text = replace_once(
        text,
        '        pool = rows("tel_match_key=?", (phone,))\n',
        '        pool = compatible_medical_identity(rows("tel_match_key=?", (phone,)))\n',
        "電話番号候補",
    )
    text = replace_once(
        text,
        '        pool = rows("name_norm=? AND address_norm=?", (name, address))\n',
        '        pool = compatible_medical_identity(rows("name_norm=? AND address_norm=?", (name, address)))\n',
        "医院名住所候補",
    )
    text = replace_once(
        text,
        '''        pool = rows("name_prefix=? AND (prefecture=? OR prefecture='')", (name[:2], record.get("prefecture", "")))[:200]\n        scored = [(0.65 * fuzz.ratio(name, row["name_norm"]) + 0.35 * fuzz.ratio(address, row["address_norm"]), row)\n''',
        '''        pool = compatible_medical_identity(\n            rows("name_prefix=? AND (prefecture=? OR prefecture='')", (name[:2], record.get("prefecture", "")))\n        )[:200]\n        scored = [(0.65 * fuzz.ratio(name, row["name_norm"]) + 0.35 * fuzz.ratio(address, row["address_norm"]), row)\n''',
        "類似候補",
    )
    return text


def patched_tests(text: str) -> str:
    if "test_distinct_official_medical_ids_with_shared_phone_stay_separate" not in text:
        text = replace_once(text, OLD_TEST_BLOCK, NEW_TEST_BLOCK, "厚生局別番号テスト")
    if "test_official_medical_id_still_matches_comdesk_without_medical_id_by_phone" not in text:
        text = replace_once(text, TEST_ANCHOR, TEST_ANCHOR + TEST_ADDITION, "既存UUID統合回帰テスト")
    return text


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=path.parent, delete=False) as tmp:
        tmp.write(content)
        temporary = Path(tmp.name)
    os.replace(temporary, path)


def main() -> None:
    if not (ROOT / "app_v2.py").is_file() or not MATCHING.is_file():
        raise ValueError("app_v2.py がある clinic-list-filter-complete フォルダー直下へ、このファイルを置いて実行してください。")

    matching_before = MATCHING.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    tests_before = TESTS.read_text(encoding="utf-8-sig").replace("\r\n", "\n") if TESTS.is_file() else None
    matching_after = patched_matching(matching_before)
    tests_after = patched_tests(tests_before) if tests_before is not None else None

    compile(matching_after, str(MATCHING), "exec")
    if tests_after is not None:
        compile(tests_after, str(TESTS), "exec")

    if matching_after == matching_before and (tests_after is None or tests_after == tests_before):
        print("この修正はすでに適用済みです。データは変更していません。")
        return

    backup = ROOT / ("code_backup_master_identity_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    backup.mkdir()
    shutil.copy2(MATCHING, backup / "matching.py")
    if TESTS.is_file():
        shutil.copy2(TESTS, backup / "test_v2_master.py")

    try:
        atomic_write(MATCHING, matching_after)
        if tests_after is not None:
            atomic_write(TESTS, tests_after)
    except BaseException:
        shutil.copy2(backup / "matching.py", MATCHING)
        if (backup / "test_v2_master.py").is_file():
            shutil.copy2(backup / "test_v2_master.py", TESTS)
        raise

    print("厚生局の医療機関番号保護ロジックを更新しました。")
    print("SQLite・CSV・調査結果は変更していません。")
    print("控え：" + backup.name)
    print("次に保存済みデータを再統合し、統合5／新規追加13,949／要確認0になることを確認してください。")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("更新できませんでした：" + str(exc))
        sys.exit(1)
