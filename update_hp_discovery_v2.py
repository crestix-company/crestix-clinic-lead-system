from pathlib import Path
from datetime import datetime
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parent
RESEARCHER = ROOT / "src" / "enrichment" / "researcher.py"

def stop(message):
    print("更新できませんでした: " + message)
    raise SystemExit(1)

def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        stop(f"{label} の対象が {count} 件でした。想定版と異なるため上書きしません。")
    return text.replace(old, new, 1)

def main():
    if not (ROOT / "app_v2.py").is_file():
        stop("app_v2.py がある clinic-list-filter-complete フォルダー直下で実行してください。")
    if not RESEARCHER.is_file():
        stop("src/enrichment/researcher.py が見つかりません。")

    before = RESEARCHER.read_text(encoding="utf-8-sig").replace("\r\n", "\n")

    if (
        "if not force and existing and is_official_candidate(existing):" in before
        and 'query_for(record,"公式ホームページ")' in before
        and "def looks_like_portal(page):" in before
    ):
        print("この修正は適用済みです。")
        return

    old_import = "from src.master.store import now\n"
    new_import = '''from src.master.store import now

PORTAL_TITLE_RE = re.compile(
    r"病院検索|クリニック検索|医療機関検索|医療情報ネット|病院なび|口コミ|求人|転職|"
    r"施設情報|医療機関情報|病院情報|Doctors?\\s*File|Medical\\s*DOC|Caloo|EPARK",
    re.I,
)

def looks_like_portal(page):
    title = unicodedata.normalize("NFKC", page.title or "")
    headings = unicodedata.normalize("NFKC", page.headings or "")
    text = title + " " + headings
    return bool(PORTAL_TITLE_RE.search(text))

'''

    old_block = '''        results = []
        candidates = []
        existing = record.get("hp_url") or record.get("hp_candidate_url")
        if existing and is_official_candidate(existing):
            candidates.append(existing)
        failures,reviews,seen = [],[],set()
        queries = iter([query_for(record),hp_fallback_query(record)])
'''

    new_block = '''        results = []
        existing = record.get("hp_url") or record.get("hp_candidate_url")

        candidates = []
        if not force and existing and is_official_candidate(existing):
            candidates.append(existing)

        failures,reviews,seen = [],[],set()
        queries = iter([query_for(record,"公式ホームページ"),hp_fallback_query(record)])
'''

    old_check = '''                    check = identity(record,page)
                    if not is_official_candidate(page.url):
                        reviews.append({"url":page.url,**check})
                        continue
'''

    new_check = '''                    check = identity(record,page)
                    if not is_official_candidate(page.url) or looks_like_portal(page):
                        reasons = list(check.get("reasons", []))
                        if looks_like_portal(page):
                            reasons.append("ポータル・求人・検索ページの可能性")
                        reviews.append({"url":page.url,**check,"reasons":reasons})
                        continue
'''

    after = before
    if "def looks_like_portal(page):" not in after:
        after = replace_once(after, old_import, new_import, "ポータル判定追加")

    if "if not force and existing and is_official_candidate(existing):" not in after:
        after = replace_once(after, old_block, new_block, "強制再調査ロジック")

    if 'query_for(record,"公式ホームページ")' not in after:
        after = replace_once(
            after,
            "queries = iter([query_for(record),hp_fallback_query(record)])",
            'queries = iter([query_for(record,"公式ホームページ"),hp_fallback_query(record)])',
            "公式HP検索語",
        )

    if "or looks_like_portal(page)" not in after:
        after = replace_once(after, old_check, new_check, "ポータル候補除外")

    compile(after, str(RESEARCHER), "exec")

    backup_dir = ROOT / ("code_backup_hp_discovery_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    backup_file = backup_dir / "src" / "enrichment" / "researcher.py"
    backup_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(RESEARCHER, backup_file)

    RESEARCHER.write_text(after, encoding="utf-8", newline="\n")

    print("HP発見処理の修正が完了しました。")
    print("・強制再調査では保存済みの旧HP URLを先に再利用しません。")
    print("・新しいTavily検索から公式HPを探し直します。")
    print("・ポータル／求人／医療機関検索ページは公式HPとして採用しません。")
    print("・公式HPを本人確認できなければ無理に確定せず要確認にします。")
    print("・DB、元CSV、UUID、手動修正は変更していません。")
    print("バックアップ: " + backup_dir.name)

if __name__ == "__main__":
    main()
