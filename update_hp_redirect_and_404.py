from pathlib import Path
from datetime import datetime
import shutil
import sys

PROJECT = Path.cwd()
SAFE_WEB = PROJECT / "src" / "enrichment" / "safe_web.py"
RESEARCHER = PROJECT / "src" / "enrichment" / "researcher.py"

if not SAFE_WEB.exists() or not RESEARCHER.exists():
    print("エラー: clinic-list-filter-complete のフォルダ内で実行してください。")
    print("必要ファイル: src\\enrichment\\safe_web.py / src\\enrichment\\researcher.py")
    sys.exit(1)

safe = SAFE_WEB.read_text(encoding="utf-8")
researcher = RESEARCHER.read_text(encoding="utf-8")

already = (
    "def _same_site_host" in safe
    and "allowed_host = allowed_host or host(url)" in safe
    and "def maps_url_candidates" in researcher
    and "candidates.extend(maps_url_candidates(maps_url))" in researcher
)
if already:
    print("この修正はすでに適用済みです。DBは変更していません。")
    sys.exit(0)

stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
backup = PROJECT / f"code_backup_hp_redirect_404_{stamp}"
(backup / "src_enrichment").mkdir(parents=True)
shutil.copy2(SAFE_WEB, backup / "src_enrichment" / "safe_web.py")
shutil.copy2(RESEARCHER, backup / "src_enrichment" / "researcher.py")

needle = 'class WebError(Exception):\n    """利用者にそのまま見せてよい、秘密を含まない取得エラー。"""\n\n\n'
insert = needle + '''def _host_key(value):\n    """www有無だけを同一サイトとして扱う。任意の別サブドメインは許可しない。"""\n    h = host(value) if "://" in str(value) else str(value or "").lower().rstrip(".")\n    return h[4:] if h.startswith("www.") else h\n\n\ndef _same_site_host(a,b):\n    return bool(_host_key(a) and _host_key(a) == _host_key(b))\n\n\n'''
if "def _same_site_host" not in safe:
    if needle not in safe:
        print("エラー: safe_web.py の挿入箇所が見つかりません。ファイルは変更していません。")
        sys.exit(2)
    safe = safe.replace(needle, insert, 1)

old = '            if allowed_host and h!=allowed_host:\n                raise WebError("別ドメインへのリダイレクトを停止しました。")\n'
new = '            if allowed_host and not _same_site_host(h,allowed_host):\n                raise WebError("別ドメインへのリダイレクトを停止しました。")\n'
if old in safe:
    safe = safe.replace(old,new,1)
elif new not in safe:
    print("エラー: safe_web.py のドメイン判定箇所1が見つかりません。ファイルは変更していません。")
    sys.exit(3)

old = '                if allowed_host and host(url)!=allowed_host:\n                    raise WebError("別ドメインへのリダイレクトを停止しました。")\n'
new = '                if allowed_host and not _same_site_host(host(url),allowed_host):\n                    raise WebError("別ドメインへのリダイレクトを停止しました。")\n'
if old in safe:
    safe = safe.replace(old,new,1)
elif new not in safe:
    print("エラー: safe_web.py のドメイン判定箇所2が見つかりません。ファイルは変更していません。")
    sys.exit(4)

old = '    def fetch(self,url,allowed_host=None):\n        if not self.allowed(url):\n'
new = '    def fetch(self,url,allowed_host=None):\n        # 初回取得も任意の別ドメインへは追従しない。www有無の変更だけ許可する。\n        allowed_host = allowed_host or host(url)\n        if not self.allowed(url):\n'
if old in safe:
    safe = safe.replace(old,new,1)
elif new not in safe:
    print("エラー: safe_web.py の fetch 箇所が見つかりません。ファイルは変更していません。")
    sys.exit(5)

if "from urllib.parse import urlsplit" not in researcher:
    old = "import unicodedata\n"
    if old not in researcher:
        print("エラー: researcher.py の import 箇所が見つかりません。ファイルは変更していません。")
        sys.exit(6)
    researcher = researcher.replace(old, old + "from urllib.parse import urlsplit\n", 1)

helper = '''def maps_url_candidates(url):\n    """MapsのURLを最優先し、古い /index.html 等に備えて同一サイトのルートだけ補完する。"""\n    if not url or not is_official_candidate(url):\n        return []\n    out = [url]\n    try:\n        p = urlsplit(url)\n        if (p.path or "/") not in {"", "/"} or p.query or p.fragment:\n            root = f"{p.scheme}://{p.netloc}/"\n            if root not in out:\n                out.append(root)\n    except ValueError:\n        pass\n    return out\n\n\n'''
if "def maps_url_candidates" not in researcher:
    marker = "def retained_media_signals(record):\n"
    if marker not in researcher:
        print("エラー: researcher.py の補完関数挿入箇所が見つかりません。ファイルは変更していません。")
        sys.exit(7)
    researcher = researcher.replace(marker, helper + marker, 1)

old = '        if maps_url and is_official_candidate(maps_url):\n            candidates.append(maps_url)\n'
new = '        if maps_url and is_official_candidate(maps_url):\n            candidates.extend(maps_url_candidates(maps_url))\n'
if old in researcher:
    researcher = researcher.replace(old,new,1)
elif new not in researcher:
    print("エラー: researcher.py の Maps候補箇所が見つかりません。ファイルは変更していません。")
    sys.exit(8)

SAFE_WEB.write_text(safe,encoding="utf-8")
RESEARCHER.write_text(researcher,encoding="utf-8")

print("修正しました。")
print("・http→https / www有無の変更は同一サイトとして追従します。")
print("・任意の別ドメインへのリダイレクトは引き続き拒否します。")
print("・Google Mapsの古い /index.html 等が404の場合、同一サイトのルートURLだけを再確認します。")
print("・最終採用は従来どおり医院名＋電話/住所の本人確認を通ったHPだけです。")
print("・Tavily検索は使いません。SQLite、UUID、既存調査結果は変更していません。")
print("バックアップ:", backup.name)
