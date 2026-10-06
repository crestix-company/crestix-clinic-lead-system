from pathlib import Path

from src.master.data_paths import (
    clinic_data_dir, hp_batch_sidecar_path, production_db_path, treatment_sidecar_path,
)


def test_shared_data_dir_supplies_all_three_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("CLINIC_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CLINIC_DB_PATH", raising=False)
    monkeypatch.delenv("TREATMENT_RESEARCH_DB_PATH", raising=False)
    monkeypatch.delenv("HP_RESEARCH_BATCH_DB_PATH", raising=False)
    assert clinic_data_dir() == tmp_path
    assert production_db_path() == tmp_path / "clinics.sqlite3"
    assert treatment_sidecar_path() == tmp_path / "treatment_research_final.sqlite3"
    assert hp_batch_sidecar_path() == tmp_path / "hp_abc_batch_sidecar.sqlite3"


def test_existing_specific_env_vars_override_shared_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CLINIC_DATA_DIR", str(tmp_path / "root"))
    expected = {
        "CLINIC_DB_PATH": tmp_path / "one.sqlite3",
        "TREATMENT_RESEARCH_DB_PATH": tmp_path / "two.sqlite3",
        "HP_RESEARCH_BATCH_DB_PATH": tmp_path / "three.sqlite3",
    }
    for key, value in expected.items():
        monkeypatch.setenv(key, str(value))
    assert production_db_path() == expected["CLINIC_DB_PATH"]
    assert treatment_sidecar_path() == expected["TREATMENT_RESEARCH_DB_PATH"]
    assert hp_batch_sidecar_path() == expected["HP_RESEARCH_BATCH_DB_PATH"]
