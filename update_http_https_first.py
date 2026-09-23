from pathlib import Path
from datetime import datetime
import shutil
import sys

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "src" / "enrichment" / "safe_web.py"

OLD = '''    def fetch(self,url,allowed_host=None):
        if not self.allowed(url):
            raise WebError("robots.txtで取得が許可されていません。")
        final,html = self._get(url,allowed_host)
        if final!=url and not self.allowed(final):
            raise WebError("移転先のrobots.txtで取得が許可されていません。")
        return Page(final,html)
'''

NEW = '''    def fetch(self,url,allowed_host=None):
        # Google Business Profile等が http:// を返しても、実サイトがHTTPS運用の
        # 場合がある。ブラウザ同様にHTTPSを先に試し、失敗時だけ元のHTTPへ戻す。
        p = urlsplit(url)
        candidates = [url]
        if p.scheme == "http" and p.port in {None,80}:
            https_netloc = p.hostname or ""
            https_url = p._replace(scheme="https",netloc=https_netloc).geturl()
            candidates = [https_url,url]

        last_error = None
        for candidate in candidates:
            try:
                if not self.allowed(candidate):
                    raise WebError("robots.txtで取得が許可されていません。")
                final,html = self._get(candidate,allowed_host)
                if final!=candidate and not self.allowed(final):
                    raise WebError("移転先のrobots.txtで取得が許可されていません。")
                return Page(final,html)
            except WebError as exc:
                last_error = exc
                # HTTPS優先候補が接続できない場合だけ、元のHTTPを試す。
                if candidate == candidates[-1]:
                    raise
        raise last_error or WebError("ページを取得できません。")
'''

def main():
    if not (ROOT / "app_v2.py").is_file() or not TARGET.is_file():
        print("app_v2.py がある clinic-list-filter-complete フォルダー直下へ、このファイルを置いて実行してください。")
        raise SystemExit(1)
    text = TARGET.read_text(encoding="utf-8-sig").replace("\r\n","\n")
    if "Google Business Profile等が http:// を返しても" in text:
        print("この修正はすでに適用済みです。DBは変更していません。")
        return
    if OLD not in text:
        print("想定した safe_web.py の取得処理が見つかりません。既存コードは変更していません。")
        raise SystemExit(1)
    backup = ROOT / ("code_backup_https_first_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    backup.mkdir(exist_ok=False)
    shutil.copy2(TARGET, backup / TARGET.name)
    new_text = text.replace(OLD, NEW, 1)
    compile(new_text, str(TARGET), "exec")
    TARGET.write_text(new_text, encoding="utf-8", newline="\n")
    print("修正しました。")
    print("・Google MapsのHP URLが http:// の場合、まず https:// を試します。")
    print("・HTTPSが使えないサイトだけ元のHTTPへフォールバックします。")
    print("・SQLite/UUID/履歴/Google Maps取込結果は変更していません。")
    print("バックアップ：" + backup.name)

if __name__ == "__main__":
    main()
