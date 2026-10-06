"""Production SQLite群の共通パス解決。存在確認や生成は呼び出し側の責務。"""
import os
from pathlib import Path


CLINIC_DATA_DIR_ENV_VAR = "CLINIC_DATA_DIR"
CLINIC_DB_ENV_VAR = "CLINIC_DB_PATH"
TREATMENT_DB_ENV_VAR = "TREATMENT_RESEARCH_DB_PATH"
HP_BATCH_DB_ENV_VAR = "HP_RESEARCH_BATCH_DB_PATH"


def clinic_data_dir():
    raw = os.getenv(CLINIC_DATA_DIR_ENV_VAR)
    return Path(raw).expanduser() if raw else Path.home() / "CrestixData" / "clinic-lead"


def _resolved_path(env_var, filename):
    raw = os.getenv(env_var)
    return Path(raw).expanduser() if raw else clinic_data_dir() / filename


def production_db_path():
    return _resolved_path(CLINIC_DB_ENV_VAR, "clinics.sqlite3")


def treatment_sidecar_path():
    return _resolved_path(TREATMENT_DB_ENV_VAR, "treatment_research_final.sqlite3")


def hp_batch_sidecar_path():
    return _resolved_path(HP_BATCH_DB_ENV_VAR, "hp_abc_batch_sidecar.sqlite3")
