from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "src" / "master" / "google_maps.py"
APP = ROOT / "app_v2.py"
TEST = ROOT / "tests" / "test_google_maps_integration.py"

COUNT_OLD = 'counts={"TOTAL":len(records),"MATCHED":0,"WEBSITE":0,"NO_WEBSITE":0,"NOT_FOUND":0,"AMBIGUOUS":0,"EXCLUDED":0,"ERROR":0,"UNLINKED":0}'
COUNT_NEW = 'counts={"TOTAL":len(records),"MATCHED":0,"WEBSITE":0,"NO_WEBSITE":0,"NOT_FOUND":0,"AMBIGUOUS":0,"EXCLUDED":0,"ERROR":0,"UNLINKED":0,"PRESERVED_WEBSITE":0}'

OLD_BLOCK = '''                clinic=c.execute("SELECT base_json FROM clinics WHERE id=?",(cid,)).fetchone();base=json.loads(clinic[0])
                before=dict(base)
                base.update({
                    "maps_presence_status":maps_status,"maps_profile_url":_s(row.get("maps_profile_url")),"maps_website_url":_s(row.get("maps_website_url")),
                    "maps_match_method":_s(row.get("maps_match_method")) or method,"maps_checked_at":_s(row.get("scraped_at")),
                    "maps_name":_s(row.get("maps_name")),"maps_phone":_s(row.get("maps_phone")),"maps_address":_s(row.get("maps_address")),
                    "maps_regular_holiday":_s(row.get("休診日")),"maps_business_days":_s(row.get("診療日")),
                    "maps_morning_start":_s(row.get("午前始")),"maps_morning_end":_s(row.get("午前終")),"maps_afternoon_start":_s(row.get("午後始")),"maps_afternoon_end":_s(row.get("午後終")),
                    "maps_hours_raw":_s(row.get("営業時間原文")),"exclude_reason":_s(row.get("exclude_reason")),
                })
'''

NEW_BLOCK = '''                clinic=c.execute("SELECT base_json FROM clinics WHERE id=?",(cid,)).fetchone();base=json.loads(clinic[0])
                before=dict(base)

                # 既にGoogle Mapsで確認済みのHP URLがある医院は、後続バッチの
                # NO_WEBSITE / AMBIGUOUS / NOT_FOUND / ERROR / URL空欄で格下げしない。
                # 各バッチの生データは google_maps_results にそのまま保存されるため、
                # 最新調査の事実は失わず、医院マスターの「確定済みHP」だけを保護する。
                existing_confirmed_website = (
                    _s(base.get("maps_presence_status")) == "MAPS_MATCHED_WEBSITE"
                    and bool(_s(base.get("maps_website_url")))
                )
                incoming_website_status = _s(row.get("website_status")).upper()
                incoming_confirmed_website = (
                    maps_status == "MAPS_MATCHED_WEBSITE"
                    and bool(_s(row.get("maps_website_url")))
                    and "AMBIGUOUS" not in incoming_website_status
                    and "NO_WEBSITE" not in incoming_website_status
                    and "ERROR" not in incoming_website_status
                )
                preserve_confirmed_website = existing_confirmed_website and not incoming_confirmed_website

                maps_update = {
                    "maps_presence_status":maps_status,"maps_profile_url":_s(row.get("maps_profile_url")),"maps_website_url":_s(row.get("maps_website_url")),
                    "maps_match_method":_s(row.get("maps_match_method")) or method,"maps_checked_at":_s(row.get("scraped_at")),
                    "maps_name":_s(row.get("maps_name")),"maps_phone":_s(row.get("maps_phone")),"maps_address":_s(row.get("maps_address")),
                    "maps_regular_holiday":_s(row.get("休診日")),"maps_business_days":_s(row.get("診療日")),
                    "maps_morning_start":_s(row.get("午前始")),"maps_morning_end":_s(row.get("午前終")),"maps_afternoon_start":_s(row.get("午後始")),"maps_afternoon_end":_s(row.get("午後終")),
                    "maps_hours_raw":_s(row.get("営業時間原文")),"exclude_reason":_s(row.get("exclude_reason")),
                }
                if preserve_confirmed_website:
                    for key in (
                        "maps_presence_status", "maps_profile_url", "maps_website_url", "maps_match_method",
                        "maps_checked_at", "maps_name", "maps_phone", "maps_address",
                        "maps_regular_holiday", "maps_business_days", "maps_morning_start", "maps_morning_end",
                        "maps_afternoon_start", "maps_afternoon_end", "maps_hours_raw", "exclude_reason",
                    ):
                        maps_update[key] = base.get(key, "")
                    counts["PRESERVED_WEBSITE"] += 1
                base.update(maps_update)
'''

APP_OLD = '''st.success(f"取込 {result.get('TOTAL',0):,}件／自動紐付け {result.get('MATCHED',0):,}件／HP取得 {result.get('WEBSITE',0):,}件／HPなし {result.get('NO_WEBSITE',0):,}件／Maps未発見 {result.get('NOT_FOUND',0):,}件／要確認 {result.get('AMBIGUOUS',0):,}件／除外 {result.get('EXCLUDED',0):,}件／エラー {result.get('ERROR',0):,}件")'''
APP_NEW = '''st.success(f"取込 {result.get('TOTAL',0):,}件／自動紐付け {result.get('MATCHED',0):,}件／HP取得 {result.get('WEBSITE',0):,}件／HPなし {result.get('NO_WEBSITE',0):,}件／Maps未発見 {result.get('NOT_FOUND',0):,}件／要確認 {result.get('AMBIGUOUS',0):,}件／除外 {result.get('EXCLUDED',0):,}件／エラー {result.get('ERROR',0):,}件／既存HP保持 {result.get('PRESERVED_WEBSITE',0):,}件")'''

TEST_BLOCK = r'''

def test_confirmed_maps_website_is_not_downgraded_by_later_ambiguous_or_blank(tmp_path):
    store=ClinicStore(tmp_path/"m.db"); r=sample_records()[0]; store.import_master([r]); cid=store.query(ALL)[0]["id"]
    first=maps_frame(
        r,
        internal_clinic_id=str(cid),
        maps_match_status="MAPS_MATCHED_WEBSITE",
        maps_website_url="http://sakuraganka.jp/",
        website_status="MAPS_WEBSITE_CONFIRMED",
        maps_profile_url="https://www.google.com/maps/place/confirmed",
        scraped_at="2026-09-15T00:00:00+00:00",
    )
    store.import_google_maps(first)

    second=maps_frame(
        r,
        internal_clinic_id=str(cid),
        maps_match_status="MAPS_MATCHED_NO_WEBSITE",
        maps_website_url="",
        website_status="MAPS_WEBSITE_AMBIGUOUS",
        maps_profile_url="https://www.google.com/maps/place/later",
        scraped_at="2026-09-16T00:00:00+00:00",
    )
    result=store.import_google_maps(second)
    got=store.get(cid)
    assert result["PRESERVED_WEBSITE"] == 1
    assert got["maps_presence_status"] == "MAPS_MATCHED_WEBSITE"
    assert got["maps_website_url"] == "http://sakuraganka.jp/"
    assert got["maps_profile_url"] == "https://www.google.com/maps/place/confirmed"

    # 後続バッチそのものは監査用に保存する。
    with store.connect() as c:
        rows=c.execute("select maps_match_status,maps_website_url from google_maps_results order by id").fetchall()
    assert len(rows)==2
    assert rows[1][0] == "MAPS_MATCHED_NO_WEBSITE"
    assert rows[1][1] == ""


def test_later_confirmed_maps_website_can_update_existing_confirmed_url(tmp_path):
    store=ClinicStore(tmp_path/"m.db"); r=sample_records()[0]; store.import_master([r]); cid=store.query(ALL)[0]["id"]
    store.import_google_maps(maps_frame(
        r, internal_clinic_id=str(cid), maps_website_url="https://old.example/",
        website_status="MAPS_WEBSITE_CONFIRMED", scraped_at="2026-09-15T00:00:00+00:00"
    ))
    result=store.import_google_maps(maps_frame(
        r, internal_clinic_id=str(cid), maps_website_url="https://new.example/",
        website_status="MAPS_WEBSITE_CONFIRMED", scraped_at="2026-09-16T00:00:00+00:00"
    ))
    got=store.get(cid)
    assert result["PRESERVED_WEBSITE"] == 0
    assert got["maps_presence_status"] == "MAPS_MATCHED_WEBSITE"
    assert got["maps_website_url"] == "https://new.example/"
'''


def fail(message: str) -> None:
    print("更新できませんでした：" + message)
    raise SystemExit(1)


def atomic_write(path: Path, content: str) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8", newline="\n")
    temp.replace(path)


def main() -> None:
    if not (ROOT / "app_v2.py").is_file() or not TARGET.is_file():
        fail("app_v2.py がある clinic-list-filter-complete フォルダー直下へ、このファイルを置いて実行してください。")

    target_before = TARGET.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    app_before = APP.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    test_before = TEST.read_text(encoding="utf-8-sig").replace("\r\n", "\n") if TEST.is_file() else ""

    if 'counts["PRESERVED_WEBSITE"] += 1' in target_before:
        print("この修正はすでに適用済みです。DBは変更していません。")
        return

    if COUNT_OLD not in target_before:
        fail("google_maps.py の件数集計箇所が想定版と異なります。無理に上書きしません。")
    if OLD_BLOCK not in target_before:
        fail("google_maps.py のGoogle Maps取込箇所が想定版と異なります。無理に上書きしません。")
    if APP_OLD not in app_before:
        fail("app_v2.py の取込結果表示箇所が想定版と異なります。無理に上書きしません。")

    target_after = target_before.replace(COUNT_OLD, COUNT_NEW, 1).replace(OLD_BLOCK, NEW_BLOCK, 1)
    app_after = app_before.replace(APP_OLD, APP_NEW, 1)
    test_after = test_before
    if TEST.is_file() and "test_confirmed_maps_website_is_not_downgraded_by_later_ambiguous_or_blank" not in test_after:
        test_after = test_after.rstrip() + TEST_BLOCK + "\n"

    compile(target_after, str(TARGET), "exec")
    compile(app_after, str(APP), "exec")
    if TEST.is_file():
        compile(test_after, str(TEST), "exec")

    backup = ROOT / ("code_backup_maps_preserve_confirmed_hp_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    backup.mkdir(parents=True, exist_ok=False)
    shutil.copy2(TARGET, backup / "google_maps.py")
    shutil.copy2(APP, backup / "app_v2.py")
    if TEST.is_file():
        shutil.copy2(TEST, backup / TEST.name)

    try:
        atomic_write(TARGET, target_after)
        atomic_write(APP, app_after)
        if TEST.is_file():
            atomic_write(TEST, test_after)
    except BaseException:
        shutil.copy2(backup / "google_maps.py", TARGET)
        shutil.copy2(backup / "app_v2.py", APP)
        if (backup / TEST.name).is_file():
            shutil.copy2(backup / TEST.name, TEST)
        raise

    print("修正しました。")
    print("・既にGoogle Mapsで確認済みのHP URLは、後続のHPなし／要確認／未発見／エラー／URL空欄で上書きしません。")
    print("・後続バッチの生データはGoogle Maps履歴としてそのまま保存します。")
    print("・後続で新しい確認済みHP URLが取得できた場合は更新できます。")
    print("・SQLite、UUID、既存Comdesk元行は直接変更していません。")
    print("バックアップ：" + backup.name)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("更新できませんでした：" + str(exc))
        sys.exit(1)
