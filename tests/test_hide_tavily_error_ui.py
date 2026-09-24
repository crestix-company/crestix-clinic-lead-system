from streamlit.testing.v1 import AppTest
from src.enrichment.search_provider import SearchError
from src.master.store import ClinicStore


def _screen_text(at):
    return " ".join(str(e.value) for e in list(at.error) + list(at.markdown) + list(at.caption))


def test_search_error_without_api_key_does_not_show_tavily(monkeypatch):
    def boom(self, *args, **kwargs):
        raise SearchError("Tavily APIキーを入力してください。Google MapsでHP取得済みの医院だけを調査する場合は検索APIを使用しません。")
    monkeypatch.setattr(ClinicStore, "__init__", boom)
    at = AppTest.from_file("../app_v2.py", default_timeout=60).run()
    assert len(at.error) == 1
    assert "avily" not in _screen_text(at)
    assert "Google MapsのHP取得状況" in at.error[0].value


def test_unexpected_error_mentioning_tavily_is_masked(monkeypatch):
    def boom(self, *args, **kwargs):
        raise RuntimeError("Tavily connection failed")
    monkeypatch.setattr(ClinicStore, "__init__", boom)
    at = AppTest.from_file("../app_v2.py", default_timeout=60).run()
    assert "avily" not in _screen_text(at)
