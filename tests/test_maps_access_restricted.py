from src.enrichment.researcher import Researcher
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
