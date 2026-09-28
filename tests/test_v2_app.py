import importlib.util
from pathlib import Path
from streamlit.testing.v1 import AppTest
from src.utils.config import ROOT
from src.io.input_loader import load_table
from src.master.comdesk import COMDESK_HEADERS
from src.master.store import ClinicStore


def test_import_has_no_database_side_effects(tmp_path,monkeypatch):
    db=tmp_path/"must-not-exist.db"
    monkeypatch.setenv("CLINIC_DB_PATH",str(db))
    spec=importlib.util.spec_from_file_location("test_import_v2",ROOT/"app_v2.py")
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert not db.exists()


def test_v2_dashboard_sample_filters_export_details_and_settings(tmp_path,monkeypatch):
    db_path=tmp_path/"app.db"
    ClinicStore(db_path)  # CLINIC_DB_PATH必須化に対応し、事前に空のスキーマだけ用意する
    monkeypatch.setenv("CLINIC_DB_PATH",str(db_path))
    # サンプル用のパスも隔離し、pytestで配布dataにDBを残さない。
    monkeypatch.setenv("CLINIC_DEMO_DB_PATH",str(tmp_path/"demo.db"))
    app=AppTest.from_file(str(ROOT/"app_v2.py"),default_timeout=30).run()
    assert not app.exception and not app.error
    app.toggle[0].set_value(True).run()
    assert not app.exception and not app.error
    assert any(m.label=="HP確認済み" and m.value=="3件" for m in app.metric)
    app.radio[0].set_value("営業対象・出力").run()
    assert not app.exception and not app.error
    next(b for b in app.button if b.label=="CSV・Excelを作成").click().run()
    assert not app.exception and not app.error
    assert "final_comdesk_import.xlsx" in app.session_state["simple_export_files"]["files"]
    for filename, content in app.session_state["simple_export_files"]["files"].items():
        assert load_table(content,filename).headers==COMDESK_HEADERS
    detail=next(s for s in app.selectbox if s.label=="詳細を確認する医院")
    detail.set_value(1).run()
    assert not app.exception and not app.error
    app.radio[0].set_value("詳細設定").run()
    section=next(s for s in app.selectbox if s.label=="開く画面")
    section.set_value("要確認・設定").run()
    assert not app.exception and not app.error
    next(b for b in app.button if b.label=="バックアップを作成").click().run()
    assert not app.exception and not app.error
    next(s for s in app.selectbox if s.label=="開く画面").set_value("マスター管理").run()
    assert not app.exception and not app.error
    next(s for s in app.selectbox if s.label=="開く画面").set_value("自動情報収集（詳細）").run()
    assert not app.exception and not app.error
