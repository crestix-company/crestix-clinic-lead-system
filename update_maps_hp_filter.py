from pathlib import Path
from datetime import datetime
import shutil
import sys

ROOT = Path(__file__).resolve().parent
# 実行場所優先。Downloads から直接実行された場合でも cwd のプロジェクトを使う。
PROJECT = Path.cwd()
APP = PROJECT / "app_v2.py"
FILTERS = PROJECT / "src" / "master" / "filters.py"

if not APP.exists() or not FILTERS.exists():
    print("エラー: clinic-list-filter-complete のフォルダ内で実行してください。")
    print("必要ファイル: app_v2.py / src\\master\\filters.py")
    sys.exit(1)

stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
backup = PROJECT / f"code_backup_maps_hp_filter_{stamp}"
backup.mkdir(exist_ok=False)
shutil.copy2(APP, backup / "app_v2.py")
(backup / "src_master").mkdir()
shutil.copy2(FILTERS, backup / "src_master" / "filters.py")

app = APP.read_text(encoding="utf-8")
flt = FILTERS.read_text(encoding="utf-8")

already = (
    "GoogleマップHP取得済みのみ" in app
    and "maps_website_only" in flt
    and "maps_website_url<>''" in flt
)
if already:
    print("この修正はすでに適用済みです。DBは変更していません。")
    print("バックアップ:", backup.name)
    sys.exit(0)

old = '        maps_confirmed = st.checkbox("Google Maps掲載確認済みのみ",value=defaults.maps_confirmed_only,key=key("maps_confirmed"))\n'
new = old + '        maps_website = st.checkbox("GoogleマップHP取得済みのみ",value=defaults.maps_website_only,key=key("maps_website"))\n'
if old not in app:
    print("エラー: app_v2.py の想定箇所が見つかりません。ファイルは変更していません。")
    sys.exit(2)
app = app.replace(old, new, 1)

old = '                   signal_min=minimum,hot=hot,owner_equal=equal,uuid_mode=uid,new_only=new,recent_opening=opening,maps_confirmed_only=maps_confirmed,signals=signals,keyword=keyword)\n'
new = '                   signal_min=minimum,hot=hot,owner_equal=equal,uuid_mode=uid,new_only=new,recent_opening=opening,maps_confirmed_only=maps_confirmed,maps_website_only=maps_website,signals=signals,keyword=keyword)\n'
if old not in app:
    print("エラー: app_v2.py の戻り値箇所が見つかりません。ファイルは変更していません。")
    sys.exit(3)
app = app.replace(old, new, 1)

old = '    maps_confirmed_only: bool = False\n    keyword: str = ""\n'
new = '    maps_confirmed_only: bool = False\n    maps_website_only: bool = False\n    keyword: str = ""\n'
if old not in flt:
    print("エラー: filters.py の Filters 定義箇所が見つかりません。ファイルは変更していません。")
    sys.exit(4)
flt = flt.replace(old, new, 1)

old = '    if f.maps_confirmed_only:\n        add("Google Maps掲載確認済み", "maps_presence_status IN (\'MAPS_MATCHED_WEBSITE\',\'MAPS_MATCHED_NO_WEBSITE\')")\n'
new = old + '    if f.maps_website_only:\n        add("Google Maps HP取得済み", "maps_presence_status=\'MAPS_MATCHED_WEBSITE\' AND maps_website_url<>\'\'")\n'
if old not in flt:
    print("エラー: filters.py の Google Maps 条件箇所が見つかりません。ファイルは変更していません。")
    sys.exit(5)
flt = flt.replace(old, new, 1)

APP.write_text(app, encoding="utf-8")
FILTERS.write_text(flt, encoding="utf-8")

print("修正しました。")
print("・『GoogleマップHP取得済みのみ』フィルターを追加しました。")
print("・Google Mapsで本人確認済みかつウェブサイトURLがある医院だけをHP内容調査できます。")
print("・HPなし5件をTavily検索対象に混ぜません。")
print("・SQLite、UUID、既存調査結果は変更していません。")
print("バックアップ:", backup.name)
