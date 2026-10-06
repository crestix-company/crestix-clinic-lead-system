"""CLINIC_DB_PATH必須化のテスト。Production DBは自動生成・自動fallbackしない。"""
import importlib.util
import sqlite3
import pytest
from streamlit.testing.v1 import AppTest
from src.utils.config import ROOT
from src.master.store import ClinicStore


def _import_app_v2():
    spec = importlib.util.spec_from_file_location("app_v2_under_test", ROOT / "app_v2.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_resolve_production_db_path_uses_shared_data_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("CLINIC_DB_PATH", raising=False)
    monkeypatch.setenv("CLINIC_DATA_DIR", str(tmp_path))
    app_v2 = _import_app_v2()
    path, error = app_v2.resolve_production_db_path()
    assert path is None
    assert str(tmp_path / "clinics.sqlite3") in error and "見つかりません" in error


def test_resolve_production_db_path_rejects_nonexistent_file(tmp_path, monkeypatch):
    missing = tmp_path / "does_not_exist.sqlite3"
    monkeypatch.setenv("CLINIC_DB_PATH", str(missing))
    app_v2 = _import_app_v2()
    path, error = app_v2.resolve_production_db_path()
    assert path is None
    assert "見つかりません" in error
    assert not missing.exists()  # 自動生成しない


def test_resolve_production_db_path_rejects_directory(tmp_path, monkeypatch):
    directory = tmp_path / "a_directory"
    directory.mkdir()
    monkeypatch.setenv("CLINIC_DB_PATH", str(directory))
    app_v2 = _import_app_v2()
    path, error = app_v2.resolve_production_db_path()
    assert path is None
    assert "ファイルではありません" in error


def test_resolve_production_db_path_rejects_missing_clinics_table(tmp_path, monkeypatch):
    not_a_clinic_db = tmp_path / "other.sqlite3"
    con = sqlite3.connect(not_a_clinic_db)
    con.execute("CREATE TABLE something_else(id INTEGER)")
    con.commit()
    con.close()
    monkeypatch.setenv("CLINIC_DB_PATH", str(not_a_clinic_db))
    app_v2 = _import_app_v2()
    path, error = app_v2.resolve_production_db_path()
    assert path is None
    assert "clinicsテーブル" in error


def test_resolve_production_db_path_accepts_valid_store(tmp_path, monkeypatch):
    db_path = tmp_path / "clinics.sqlite3"
    ClinicStore(db_path)
    monkeypatch.setenv("CLINIC_DB_PATH", str(db_path))
    app_v2 = _import_app_v2()
    path, error = app_v2.resolve_production_db_path()
    assert error is None
    assert path == db_path


def test_app_fails_loudly_when_clinic_db_path_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("CLINIC_DB_PATH", raising=False)
    monkeypatch.setenv("CLINIC_DATA_DIR", str(tmp_path / "external-data"))
    monkeypatch.setenv("CLINIC_DEMO_DB_PATH", str(tmp_path / "demo.db"))
    old_repo_db = ROOT / "data/clinics.sqlite3"
    before_exists = old_repo_db.exists()

    app = AppTest.from_file(str(ROOT / "app_v2.py"), default_timeout=30).run()

    assert not app.exception  # st.error+st.stop()で処理し、未処理の例外にはしない
    assert any("clinics.sqlite3" in e.value and "見つかりません" in e.value for e in app.error)
    assert not any(m.label == "HP確認済み" for m in app.metric)  # 本編は描画されていない
    # 旧repo内DBが「たまたま存在していても」変化しないこと（作成も削除もしない）
    assert old_repo_db.exists() == before_exists


def test_app_fails_loudly_for_nonexistent_clinic_db_path(tmp_path, monkeypatch):
    missing = tmp_path / "no_such_prod.sqlite3"
    monkeypatch.setenv("CLINIC_DB_PATH", str(missing))
    monkeypatch.setenv("CLINIC_DEMO_DB_PATH", str(tmp_path / "demo.db"))

    app = AppTest.from_file(str(ROOT / "app_v2.py"), default_timeout=30).run()

    assert not app.exception
    assert any("見つかりません" in e.value for e in app.error)
    assert not missing.exists()  # 自動生成しない


def test_app_opens_correct_db_when_clinic_db_path_valid(tmp_path, monkeypatch):
    db_path = tmp_path / "prod.sqlite3"
    store = ClinicStore(db_path)
    from src.master.samples import sample_records
    store.import_master(sample_records())
    monkeypatch.setenv("CLINIC_DB_PATH", str(db_path))
    monkeypatch.setenv("CLINIC_DEMO_DB_PATH", str(tmp_path / "demo.db"))

    app = AppTest.from_file(str(ROOT / "app_v2.py"), default_timeout=30).run()

    assert not app.exception and not app.error
    assert len(app.metric) > 0  # 正しいDBが開けて本編が描画されている
