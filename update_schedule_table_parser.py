from pathlib import Path
from datetime import datetime
import shutil
import sys

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "src" / "enrichment" / "consultation_schedule.py"


def stop(msg, code=1):
    print("更新できませんでした：" + msg)
    raise SystemExit(code)


def main():
    if not (ROOT / "app_v2.py").is_file() or not TARGET.is_file():
        stop("app_v2.py がある clinic-list-filter-complete フォルダー直下へ、このファイルを置いて実行してください。")

    text = TARGET.read_text(encoding="utf-8-sig").replace("\r\n", "\n")

    already = (
        "if not matrix or not any(matrix):" in text
        and "if not row:\n            continue\n        row_days=days(row[0])" in text
        and "heading = headings[column] if column < len(headings) else ''" in text
    )
    if already:
        print("この修正はすでに適用済みです。DBは変更していません。")
        return

    new = text

    old = "    matrix=grid(table)\n    if not matrix:\n        return []\n"
    repl = "    matrix=grid(table)\n    # 空の<tr>だけを含む表など、列を1つも作れない表は診療時間表として扱わない。\n    if not matrix or not any(matrix):\n        return []\n"
    if old in new:
        new = new.replace(old, repl, 1)
    elif "if not matrix or not any(matrix):" not in new:
        stop("consultation_schedule.py の table_slots 冒頭が想定版と異なります。ファイルは変更していません。", 2)

    old = "        for row in matrix[row_index+1:]:\n            prefix=' '.join(row[:first_day])\n            for column,day_set in columns.items():\n                cell=row[column]\n"
    repl = "        for row in matrix[row_index+1:]:\n            if not row:\n                continue\n            prefix=' '.join(row[:first_day])\n            for column,day_set in columns.items():\n                if column >= len(row):\n                    continue\n                cell=row[column]\n"
    if old in new:
        new = new.replace(old, repl, 1)
    elif "if column >= len(row):" not in new:
        stop("consultation_schedule.py の横型診療時間表処理が想定版と異なります。ファイルは変更していません。", 3)

    old = "    headings=matrix[0]\n    for row in matrix:\n        row_days=days(row[0])\n        if row_days:\n            for column,cell in enumerate(row[1:],1):\n                found.extend(slots(headings[column]+' '+cell,default='ordinary' if schedule else '',day_set=row_days))\n"
    repl = "    headings=matrix[0]\n    for row in matrix:\n        # HTMLによっては空行・セル数不一致の表がある。任意のHP全体調査を止めず、その行だけ無視する。\n        if not row:\n            continue\n        row_days=days(row[0])\n        if row_days:\n            for column,cell in enumerate(row[1:],1):\n                heading = headings[column] if column < len(headings) else ''\n                found.extend(slots((heading+' '+cell).strip(),default='ordinary' if schedule else '',day_set=row_days))\n"
    if old in new:
        new = new.replace(old, repl, 1)
    elif not (
        "if not row:\n            continue\n        row_days=days(row[0])" in new
        and "heading = headings[column] if column < len(headings) else ''" in new
    ):
        stop("consultation_schedule.py の縦型診療時間表処理が想定版と異なります。ファイルは変更していません。", 4)

    compile(new, str(TARGET), "exec")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = ROOT / f"code_backup_schedule_table_parser_{stamp}"
    backup.mkdir(parents=True, exist_ok=False)
    shutil.copy2(TARGET, backup / "consultation_schedule.py")

    TARGET.write_text(new, encoding="utf-8", newline="\n")

    print("修正しました。")
    print("・空の表行や不規則なHTML表で IndexError にならないようにしました。")
    print("・昼の検査・手術専用枠の判定は、読める診療時間表だけで従来どおり行います。")
    print("・Tavily、SQLite、UUID、既存調査結果は変更していません。")
    print("バックアップ：" + backup.name)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("更新できませんでした：" + str(exc))
        sys.exit(1)
