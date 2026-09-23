from pathlib import Path
from datetime import datetime
import shutil
import sys

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "src" / "enrichment" / "researcher.py"
TEST = ROOT / "tests" / "test_maps_access_restricted.py"

ANCHOR = '''        status = "REVIEW" if reviews else "ERROR" if failures else "NOT_FOUND"\n        signals = dedupe_signals(media_signals(record,results)+retained_media_signals(record))\n'''

INSERT = '''        # Google Mapsで医院本人確認済みのウェブサイトURLは、HP側が\n        # 403/429/robots.txt/アクセス確認等で自動取得を拒否してもURL自体を失わない。\n        # アクセス制限を回避する処理は行わず、内容解析だけ要確認として残す。\n        access_restricted = re.compile(\n            r"アクセス制限|アクセス確認画面|HTTP\\s*(?:401|403|429)|"\n            r"robots\\.txtで取得が許可|取得間隔が長",\n            re.I,\n        )\n        if maps_url and failures and not reviews and all(\n            access_restricted.search(str(item.get("reason", ""))) for item in failures\n        ):\n            signals = retained_media_signals(record)\n            result = empty_hp_result("VERIFIED", record)\n            result.update(\n                hp_status="VERIFIED",\n                hp_verified=True,\n                hp_url=maps_url,\n                hp_rank="UNKNOWN",\n                hp_score=0,\n                hp_match_reason=[\n                    "Google Mapsで医院本人確認済みのウェブサイトURLです。"\n                    "サイト側のアクセス制限によりHP内容の自動解析は未完了です。"\n                ],\n                hp_candidates=[],\n                crawl_errors=failures,\n                hp_checked_at=now(),\n                hp_content_status="ACCESS_RESTRICTED",\n                hp_content_note="サイト側のアクセス制限のため自動解析せず、手動確認対象として保持します。",\n                research_status="REVIEW",\n                research_error="",\n                marketing_signals=signals,\n                marketing_signal_count=len(signals),\n                hot_status=hot_status(len(signals)),\n            )\n            return result, []\n\n        status = "REVIEW" if reviews else "ERROR" if failures else "NOT_FOUND"\n        signals = dedupe_signals(media_signals(record,results)+retained_media_signals(record))\n'''

TEST_CONTENT = r'''from src.enrichment.researcher import Researcher
from src.enrichment.safe_web import WebError


class NeverSearch:
    def search(self, *args, **kwargs):
        raise AssertionError("Maps website must not fall back to search")


class BlockedFetcher:
    def fetch(self, url, allowed_host=None):
        raise WebError("アクセス制限があるため、このサイトの取得を停止しました。")


def test_confirmed_maps_website_access_restriction_keeps_url_as_verified():
    record = {
        "clinic_name": "架空眼科",
        "phone": "03-1234-5678",
        "address": "東京都千代田区1-2-3",
        "maps_presence_status": "MAPS_MATCHED_WEBSITE",
        "maps_website_url": "http://clinic.example/",
        "marketing_signals": [],
    }
    result, pages = Researcher(NeverSearch(), fetcher=BlockedFetcher(), max_pages=1).hp(record, force=True)
    assert pages == []
    assert result["hp_status"] == "VERIFIED"
    assert result["hp_verified"] is True
    assert result["hp_url"] == "http://clinic.example/"
    assert result["hp_content_status"] == "ACCESS_RESTRICTED"
    assert result["research_status"] == "REVIEW"
    assert result["hp_rank"] == "UNKNOWN"
    assert result["crawl_errors"]


class BrokenFetcher:
    def fetch(self, url, allowed_host=None):
        raise WebError("接続・SSL・タイムアウトのエラーです。")


def test_non_access_failure_is_still_error():
    record = {
        "clinic_name": "架空眼科",
        "phone": "03-1234-5678",
        "address": "東京都千代田区1-2-3",
        "maps_presence_status": "MAPS_MATCHED_WEBSITE",
        "maps_website_url": "https://clinic.example/",
        "marketing_signals": [],
    }
    result, _ = Researcher(NeverSearch(), fetcher=BrokenFetcher(), max_pages=1).hp(record, force=True)
    assert result["hp_status"] == "ERROR"
'''


def stop(msg):
    print("更新できませんでした：" + msg)
    raise SystemExit(1)


def main():
    if not (ROOT / "app_v2.py").is_file() or not TARGET.is_file():
        stop("app_v2.py がある clinic-list-filter-complete フォルダー直下へ、このファイルを置いて実行してください。")

    text = TARGET.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    if 'hp_content_status="ACCESS_RESTRICTED"' in text:
        print("この修正はすでに適用済みです。DBは変更していません。")
        return
    if ANCHOR not in text:
        stop("researcher.py の対象箇所が想定版と異なります。無理に上書きしません。")

    backup = ROOT / ("code_backup_maps_access_restricted_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    backup.mkdir(parents=True, exist_ok=False)
    shutil.copy2(TARGET, backup / "researcher.py")
    if TEST.is_file():
        shutil.copy2(TEST, backup / TEST.name)

    new_text = text.replace(ANCHOR, INSERT, 1)
    compile(new_text, str(TARGET), "exec")
    TARGET.write_text(new_text, encoding="utf-8", newline="\n")

    TEST.parent.mkdir(parents=True, exist_ok=True)
    TEST.write_text(TEST_CONTENT, encoding="utf-8", newline="\n")
    compile(TEST_CONTENT, str(TEST), "exec")

    print("修正しました。")
    print("・Google Mapsで確認済みのHPがサイト側のアクセス制限で取得できない場合、HP URLは確認済みとして保持します。")
    print("・HP内容解析は ACCESS_RESTRICTED / 要確認として残し、成功扱いにはしません。")
    print("・403/429/robots.txt等を回避する処理は追加していません。")
    print("・SQLite/UUID/Google Maps取込結果は変更していません。")
    print("バックアップ：" + backup.name)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("更新できませんでした：" + str(exc))
        sys.exit(1)
