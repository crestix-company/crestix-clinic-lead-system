import sqlite3
from pathlib import Path
from streamlit.testing.v1 import AppTest
from src.enrichment.search_provider import SearchError
from src.master.store import ClinicStore


APP_PATH = str(Path(__file__).resolve().parents[1] / "app_v2.py")


def _screen_text(at):
    return " ".join(str(e.value) for e in list(at.error) + list(at.markdown) + list(at.caption))


def _valid_clinic_db_path(tmp_path, monkeypatch):
    monkeypatch.setenv("CLINIC_DATA_BACKEND", "sqlite")  # explicit legacy/admin test mode
    # CLINIC_DB_PATH必須化のバリデーションはClinicStoreを経由しないため、
    # ClinicStore.__init__をmonkeypatchする前に素のsqlite3でclinicsテーブルだけ用意する。
    db_path = tmp_path / "prod.sqlite3"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE clinics(id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    monkeypatch.setenv("CLINIC_DB_PATH", str(db_path))
    return db_path


def test_search_error_without_api_key_does_not_show_tavily(tmp_path, monkeypatch):
    _valid_clinic_db_path(tmp_path, monkeypatch)

    def boom(self, *args, **kwargs):
        raise SearchError("Tavily APIキーを入力してください。Google MapsでHP取得済みの医院だけを調査する場合は検索APIを使用しません。")
    monkeypatch.setattr(ClinicStore, "__init__", boom)
    at = AppTest.from_file(APP_PATH, default_timeout=60).run()
    assert len(at.error) == 1
    assert "avily" not in _screen_text(at)
    assert "Google MapsのHP取得状況" in at.error[0].value


def test_unexpected_error_mentioning_tavily_is_masked(tmp_path, monkeypatch):
    _valid_clinic_db_path(tmp_path, monkeypatch)

    def boom(self, *args, **kwargs):
        raise RuntimeError("Tavily connection failed")
    monkeypatch.setattr(ClinicStore, "__init__", boom)
    at = AppTest.from_file(APP_PATH, default_timeout=60).run()
    assert "avily" not in _screen_text(at)
