from pathlib import Path
from datetime import datetime
import shutil
import sys

ROOT = Path(__file__).resolve().parent
SEARCH_PROVIDER = ROOT / "src" / "enrichment" / "search_provider.py"
APP = ROOT / "app_v2.py"

OLD_PROVIDER = '''        self._api_key = api_key or os.getenv("TAVILY_API_KEY", "")\n        self.session = session or requests.Session()\n        if not self._api_key:\n            raise SearchError("Tavily APIキーを入力してください。")\n\n    def search(self,query):\n        try:\n'''
NEW_PROVIDER = '''        self._api_key = api_key or os.getenv("TAVILY_API_KEY", "")\n        self.session = session or requests.Session()\n\n    def search(self,query):\n        # Google Mapsで公式HP URLを取得済みの医院は検索APIを呼ばない。\n        # キーは、実際に検索が必要になったときだけ必須にする。\n        if not self._api_key:\n            raise SearchError("Tavily APIキーを入力してください。Google MapsでHP取得済みの医院だけを調査する場合は検索APIを使用しません。")\n        try:\n'''

OLD_APP = 'api_key = st.text_input("Tavily APIキー",type="password",key="tavily_api_key",placeholder="環境変数 TAVILY_API_KEY でも設定できます",disabled=demo)'
NEW_APP = '''api_key = st.text_input("Tavily APIキー（Google MapsでHP取得済みの医院だけなら不要）",type="password",key="tavily_api_key",placeholder="HP発見検索が必要な場合のみ入力。環境変数 TAVILY_API_KEY でも設定できます",disabled=demo)\n    st.caption("Google Mapsの確認済みウェブサイトURLがある医院は、Tavilyを使わずそのURLから直接HP内容を調査します。")'''


def stop(msg):
    print("更新できませんでした：" + msg)
    raise SystemExit(1)


def main():
    if not (ROOT / "app_v2.py").is_file():
        stop("app_v2.py がある clinic-list-filter-complete フォルダー直下へ、このファイルを置いて実行してください。")
    if not SEARCH_PROVIDER.is_file():
        stop("src/enrichment/search_provider.py が見つかりません。")

    provider = SEARCH_PROVIDER.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    app = APP.read_text(encoding="utf-8-sig").replace("\r\n", "\n")

    already = "キーは、実際に検索が必要になったときだけ必須にする。" in provider
    if already:
        print("この修正はすでに適用済みです。DBは変更していません。")
        return

    if OLD_PROVIDER not in provider:
        stop("search_provider.py の対象箇所が想定版と異なります。無理に上書きしません。")

    backup = ROOT / ("code_backup_maps_no_tavily_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    backup.mkdir(parents=True, exist_ok=False)
    shutil.copy2(SEARCH_PROVIDER, backup / "search_provider.py")
    shutil.copy2(APP, backup / "app_v2.py")

    provider2 = provider.replace(OLD_PROVIDER, NEW_PROVIDER, 1)
    app2 = app.replace(OLD_APP, NEW_APP, 1) if OLD_APP in app else app

    compile(provider2, str(SEARCH_PROVIDER), "exec")
    compile(app2, str(APP), "exec")

    SEARCH_PROVIDER.write_text(provider2, encoding="utf-8", newline="\n")
    APP.write_text(app2, encoding="utf-8", newline="\n")

    print("修正しました。")
    print("・Google MapsでHP取得済みの医院はTavily APIキーなしでHP内容調査できます。")
    print("・Tavilyが実際に必要な医院だけ、検索時にAPIキーを要求します。")
    print("・SQLite/UUID/履歴/Google Maps取込結果は変更していません。")
    print("バックアップ：" + backup.name)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("更新できませんでした：" + str(exc))
        sys.exit(1)
