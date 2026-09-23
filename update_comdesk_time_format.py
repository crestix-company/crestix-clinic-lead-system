from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "src" / "master" / "fixed_export.py"
TESTS = ROOT / "tests" / "test_v2_fixed_export.py"
TESTS_MAPS = ROOT / "tests" / "test_google_maps_integration.py"

HELPER_MARK = "def _format_comdesk_time(value):"
TIME_FIELDS = '("午前始", "午前終", "午後始", "午後終")'

HELPER = r'''
TIME_FIELDS = ("午前始", "午前終", "午後始", "午後終")
_TIME_HM_RE = re.compile(r"^\s*(\d{1,2}):(\d{1,2})(?::\d{1,2}(?:\.\d+)?)?\s*$")
_TIME_NUMBER_RE = re.compile(r"^\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)\s*$")


def _format_comdesk_time(value):
    """Comdeskの診療時刻4列を常にHH:MM文字列へ正規化する。

    Excel/CSV由来の時刻小数（例 0.5416666667=13:00）も変換する。
    DBやComdesk元行そのものは変更せず、最終出力時だけ整形する。
    """
    if value is None:
        return ""

    if hasattr(value, "hour") and hasattr(value, "minute") and not isinstance(value, str):
        try:
            return f"{int(value.hour):02d}:{int(value.minute):02d}"
        except (TypeError, ValueError):
            pass

    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "nat"}:
        return ""

    match = _TIME_HM_RE.match(text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        if (0 <= hour <= 23 and 0 <= minute <= 59) or (hour == 24 and minute == 0):
            return f"{hour:02d}:{minute:02d}"
        return text

    if _TIME_NUMBER_RE.match(text):
        try:
            number = float(text)
        except ValueError:
            return text
        if 0 <= number < 1:
            total_minutes = int(round(number * 24 * 60))
            if total_minutes == 24 * 60:
                return "24:00"
            hour, minute = divmod(total_minutes, 60)
            return f"{hour:02d}:{minute:02d}"

    return text
'''

NORMALIZE_BLOCK = '''    # Comdeskの診療時刻4列は、元値がExcel時刻小数でも最終出力ではHH:MMへ統一する。\n    for heading in TIME_FIELDS:\n        values[heading] = _format_comdesk_time(values.get(heading, ""))\n'''

TEST_BLOCK = r'''

def test_comdesk_time_columns_are_always_hhmm():
    row = fixed_row({
        "clinic_name": "時刻整形テスト医院",
        "maps_morning_start": "0.3958333333",
        "maps_morning_end": 0.5416666667,
        "maps_afternoon_start": "14:30",
        "maps_afternoon_end": "9:05:00",
    })
    assert row[22:26] == ["09:30", "13:00", "14:30", "09:05"]

    raw = [""] * 28
    raw[2] = "既存Comdesk時刻テスト"
    raw[22:26] = ["0.4166666667", "0.4791666667", "0.625", "0.7291666667"]
    row = fixed_row({}, COMDESK_HEADERS, {}, raw)
    assert row[22:26] == ["10:00", "11:30", "15:00", "17:30"]
'''


def fail(message: str) -> None:
    print("更新できませんでした：" + message)
    raise SystemExit(1)


def patch_fixed_export(text: str) -> str:
    text = text.replace("\r\n", "\n")
    if "import re\n" not in text:
        if "import json\n" not in text:
            fail("fixed_export.py のimport jsonを確認できません。")
        text = text.replace("import json\n", "import json\nimport re\n", 1)
    if HELPER_MARK not in text:
        anchor = '''OUTPUT_FIELDS = {\n    "uuid": "UUID", "clinic_name": "名前", "phone": "Tel1",\n    "prefecture": "都道府県", "address": "住所１", "address2": "住所２",\n    "postal_code": "郵便番号", "manager_name": "院長名", "url": "URL",\n}\n'''
        if anchor not in text:
            fail("fixed_export.py のOUTPUT_FIELDS定義を確認できません。")
        text = text.replace(anchor, anchor + "\n" + HELPER, 1)

    if NORMALIZE_BLOCK.strip() not in text:
        anchor = "    return [values[heading] for heading in COMDESK_HEADERS]\n"
        if anchor not in text:
            fail("fixed_export.py の最終return位置を確認できません。")
        text = text.replace(anchor, NORMALIZE_BLOCK + anchor, 1)

    return text


def patch_tests(text: str) -> str:
    text = text.replace("\r\n", "\n")
    if "test_comdesk_time_columns_are_always_hhmm" not in text:
        text = text.rstrip() + TEST_BLOCK + "\n"
    return text


def patch_maps_tests(text: str) -> str:
    text = text.replace("\r\n", "\n")
    old = 'assert out.data.iloc[0,20:26].tolist()==["日","月・火","9:00","12:00","15:00","18:00"]'
    new = 'assert out.data.iloc[0,20:26].tolist()==["日","月・火","09:00","12:00","15:00","18:00"]'
    if old in text:
        text = text.replace(old, new, 1)
    return text


def atomic_write(path: Path, content: str) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8", newline="\n")
    temp.replace(path)


def main() -> None:
    if not (ROOT / "app_v2.py").is_file():
        fail("app_v2.py がある clinic-list-filter-complete フォルダー直下へこのファイルを置いて実行してください。")
    if not TARGET.is_file():
        fail("src/master/fixed_export.py が見つかりません。")

    before = TARGET.read_text(encoding="utf-8-sig")
    after = patch_fixed_export(before)
    test_before = TESTS.read_text(encoding="utf-8-sig") if TESTS.is_file() else None
    test_after = patch_tests(test_before) if test_before is not None else None
    maps_test_before = TESTS_MAPS.read_text(encoding="utf-8-sig") if TESTS_MAPS.is_file() else None
    maps_test_after = patch_maps_tests(maps_test_before) if maps_test_before is not None else None

    compile(after, str(TARGET), "exec")
    if test_after is not None:
        compile(test_after, str(TESTS), "exec")
    if maps_test_after is not None:
        compile(maps_test_after, str(TESTS_MAPS), "exec")

    if (
        after == before.replace("\r\n", "\n")
        and (test_before is None or test_after == test_before.replace("\r\n", "\n"))
        and (maps_test_before is None or maps_test_after == maps_test_before.replace("\r\n", "\n"))
    ):
        print("この修正はすでに適用済みです。DBは変更していません。")
        return

    backup = ROOT / ("code_backup_comdesk_time_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    backup.mkdir(exist_ok=False)
    shutil.copy2(TARGET, backup / "fixed_export.py")
    if TESTS.is_file():
        shutil.copy2(TESTS, backup / "test_v2_fixed_export.py")
    if TESTS_MAPS.is_file():
        shutil.copy2(TESTS_MAPS, backup / "test_google_maps_integration.py")

    try:
        atomic_write(TARGET, after)
        if test_after is not None:
            atomic_write(TESTS, test_after)
        if maps_test_after is not None:
            atomic_write(TESTS_MAPS, maps_test_after)
    except BaseException:
        shutil.copy2(backup / "fixed_export.py", TARGET)
        if (backup / "test_v2_fixed_export.py").is_file():
            shutil.copy2(backup / "test_v2_fixed_export.py", TESTS)
        if (backup / "test_google_maps_integration.py").is_file():
            shutil.copy2(backup / "test_google_maps_integration.py", TESTS_MAPS)
        raise

    print("修正しました。")
    print("・Comdesk出力の 午前始／午前終／午後始／午後終 を HH:MM 形式に固定します。")
    print("・例：0.5416666667 → 13:00、9:05 → 09:05")
    print("・既存SQLite、UUID、元Comdesk行は変更しません。最終出力時だけ整形します。")
    print("バックアップ：" + backup.name)


if __name__ == "__main__":
    main()
