from pathlib import Path
from datetime import datetime
import shutil
import sys

ROOT = Path(__file__).resolve().parent
SAFE_WEB = ROOT / "src" / "enrichment" / "safe_web.py"
RESEARCHER = ROOT / "src" / "enrichment" / "researcher.py"


def stop(msg, code=1):
    print("更新できませんでした：" + msg)
    raise SystemExit(code)


def main():
    if not (ROOT / "app_v2.py").is_file() or not SAFE_WEB.is_file() or not RESEARCHER.is_file():
        stop("app_v2.py がある clinic-list-filter-complete フォルダー直下へ、このファイルを置いて実行してください。")

    safe = SAFE_WEB.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    researcher = RESEARCHER.read_text(encoding="utf-8-sig").replace("\r\n", "\n")

    already = (
        "def _same_site_host" in safe
        and "allowed_host = allowed_host or host(url)" in safe
        and "def maps_url_candidates" in researcher
        and "candidates.extend(maps_url_candidates(maps_url))" in researcher
    )
    if already:
        print("この修正はすでに適用済みです。DBは変更していません。")
        return

    new_safe = safe
    new_researcher = researcher

    # 1) www有無だけを同一サイトとして扱う。任意の別サブドメインは許可しない。
    if "def _same_site_host" not in new_safe:
        marker = "def validate_url(url, resolve=True):\n"
        if marker not in new_safe:
            stop("safe_web.py のヘルパー挿入箇所が見つかりません。ファイルは変更していません。", 2)
        helper = '''def _host_key(value):\n    """www有無だけを同一サイトとして扱う。任意の別サブドメインは許可しない。"""\n    text = str(value or "")\n    h = host(text) if "://" in text else text.lower().rstrip(".")\n    return h[4:] if h.startswith("www.") else h\n\n\ndef _same_site_host(a, b):\n    return bool(_host_key(a) and _host_key(a) == _host_key(b))\n\n\n'''
        new_safe = new_safe.replace(marker, helper + marker, 1)

    replacements = [
        (
            "            if allowed_host and h!=allowed_host:\n                raise WebError(\"別ドメインへのリダイレクトを停止しました。\")\n",
            "            if allowed_host and not _same_site_host(h, allowed_host):\n                raise WebError(\"別ドメインへのリダイレクトを停止しました。\")\n",
        ),
        (
            "                if allowed_host and host(url)!=allowed_host:\n                    raise WebError(\"別ドメインへのリダイレクトを停止しました。\")\n",
            "                if allowed_host and not _same_site_host(host(url), allowed_host):\n                    raise WebError(\"別ドメインへのリダイレクトを停止しました。\")\n",
        ),
    ]
    for old, new in replacements:
        if old in new_safe:
            new_safe = new_safe.replace(old, new, 1)
        elif new not in new_safe:
            stop("safe_web.py のリダイレクト判定箇所が想定版と異なります。ファイルは変更していません。", 3)

    # 2) 初回取得でも元サイトを固定する。HTTPS優先パッチ適用済みの fetch にも対応。
    if "allowed_host = allowed_host or host(url)" not in new_safe:
        marker = "    def fetch(self,url,allowed_host=None):\n"
        if marker not in new_safe:
            stop("safe_web.py の fetch 関数が見つかりません。ファイルは変更していません。", 4)
        new_safe = new_safe.replace(
            marker,
            marker + "        # 初回取得も別サイトへは追従しない。www有無だけ同一サイトとして許可する。\n        allowed_host = allowed_host or host(url)\n",
            1,
        )

    # 3) Maps URLが古い /index.html 等なら、同じドメインのルートだけ次候補にする。
    if "from urllib.parse import urlsplit" not in new_researcher:
        import_marker = "import unicodedata\n"
        if import_marker not in new_researcher:
            stop("researcher.py の import 箇所が見つかりません。ファイルは変更していません。", 5)
        new_researcher = new_researcher.replace(import_marker, import_marker + "from urllib.parse import urlsplit\n", 1)

    if "def maps_url_candidates" not in new_researcher:
        marker = "def retained_media_signals(record):\n"
        if marker not in new_researcher:
            stop("researcher.py の補完関数挿入箇所が見つかりません。ファイルは変更していません。", 6)
        helper = '''def maps_url_candidates(url):\n    """Maps URLを最優先し、古いパス時だけ同一ホストのルートを次候補にする。"""\n    if not url or not is_official_candidate(url):\n        return []\n    out = [url]\n    try:\n        p = urlsplit(url)\n        if (p.path or "/") not in {"", "/"} or p.query or p.fragment:\n            root = f"{p.scheme}://{p.netloc}/"\n            if root not in out:\n                out.append(root)\n    except ValueError:\n        pass\n    return out\n\n\n'''
        new_researcher = new_researcher.replace(marker, helper + marker, 1)

    old = "        if maps_url and is_official_candidate(maps_url):\n            candidates.append(maps_url)\n"
    new = "        if maps_url and is_official_candidate(maps_url):\n            candidates.extend(maps_url_candidates(maps_url))\n"
    if old in new_researcher:
        new_researcher = new_researcher.replace(old, new, 1)
    elif new not in new_researcher:
        stop("researcher.py のGoogle Maps URL候補箇所が想定版と異なります。ファイルは変更していません。", 7)

    # 書き込む前に構文確認。
    compile(new_safe, str(SAFE_WEB), "exec")
    compile(new_researcher, str(RESEARCHER), "exec")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = ROOT / f"code_backup_hp_redirect_404_v2_{stamp}"
    backup.mkdir(parents=True, exist_ok=False)
    shutil.copy2(SAFE_WEB, backup / "safe_web.py")
    shutil.copy2(RESEARCHER, backup / "researcher.py")

    SAFE_WEB.write_text(new_safe, encoding="utf-8", newline="\n")
    RESEARCHER.write_text(new_researcher, encoding="utf-8", newline="\n")

    print("修正しました。")
    print("・http→https と www有無の変更は同一サイトとして追従します。")
    print("・任意の別ドメインへのリダイレクトは引き続き拒否します。")
    print("・Maps URLが古い /index.html 等の場合、同じサイトのルートURLだけを次候補にします。")
    print("・Tavilyは使いません。SQLite、UUID、既存調査結果は変更していません。")
    print("バックアップ：" + backup.name)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("更新できませんでした：" + str(exc))
        sys.exit(1)
