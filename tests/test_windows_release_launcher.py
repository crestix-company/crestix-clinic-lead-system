import json
from pathlib import Path

from src.utils.config import ROOT


def _text(relative):
    return (ROOT / relative).read_text(encoding="utf-8-sig")


def _bytes(relative):
    return (ROOT / relative).read_bytes()


def test_powershell_launchers_use_utf8_bom_and_decode_as_utf8_sig():
    for relative in ("scripts/update_and_launch_windows.ps1", "scripts/launch_v2_windows.ps1"):
        payload = _bytes(relative)
        assert payload.startswith(b"\xef\xbb\xbf")
        payload.decode("utf-8-sig")


def test_windows_batch_launchers_use_crlf_only():
    for relative in ("start_v2_windows.bat", "setup_v2_windows.bat"):
        payload = _bytes(relative)
        assert b"\n" in payload
        assert b"\n" not in payload.replace(b"\r\n", b"")


def test_gitattributes_preserves_windows_script_line_endings():
    attributes = _text(".gitattributes")
    assert "*.bat text eol=crlf" in attributes
    assert "*.ps1 text eol=crlf" in attributes


def test_start_bat_uses_update_launcher_and_launch_v2_is_single_runtime():
    start = _text("start_v2_windows.bat")
    update = _text("scripts/update_and_launch_windows.ps1")
    assert "update_and_launch_windows.ps1" in start
    assert "git switch main" in update
    assert "git pull --ff-only origin main" in update
    assert update.count('Join-Path $RepoRoot "scripts\\launch_v2.py"') == 1


def test_both_windows_launchers_fail_closed_for_all_three_databases():
    for relative in ("scripts/update_and_launch_windows.ps1", "scripts/launch_v2_windows.ps1"):
        text = _text(relative)
        assert "CLINIC_DATA_DIR" in text
        assert "CLINIC_DB_PATH" in text
        assert "TREATMENT_RESEARCH_DB_PATH" in text
        assert "HP_RESEARCH_BATCH_DB_PATH" in text
        assert "clinics.sqlite3" in text
        assert "treatment_research_final.sqlite3" in text
        assert "hp_abc_batch_sidecar.sqlite3" in text
        assert "mode=ro&immutable=1" in text
        assert "自動取得・自動生成・copy・migrateしません" in text
        assert "hp_batch_clinics" in text
        assert "hp_treatment_detected" in text


def test_release_snapshot_contract_contains_hp_batch_counts():
    expected = json.loads(_text("config/production_data_version.json"))
    assert expected["clinics_count"] == 162_258
    assert expected["hp_batch_clinics"] == 10_313
    assert expected["hp_researched"] == 9_751
    assert expected["hp_failed"] == 562
    assert expected["hp_treatment_detected"] == 3_033
    assert expected["hp_treatment_not_detected"] == 6_718
