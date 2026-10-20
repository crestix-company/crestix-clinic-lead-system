from src.cloud_run.feature_flags import cloud_write_feature_enabled


def test_default_off_when_unset(monkeypatch):
    monkeypatch.delenv("CLINIC_CLOUD_COMDESK_IMPORT_ENABLED", raising=False)
    assert cloud_write_feature_enabled("CLINIC_CLOUD_COMDESK_IMPORT_ENABLED") is False


def test_enabled_with_truthy_values(monkeypatch):
    for value in ("1", "true", "True", "YES", "on"):
        monkeypatch.setenv("CLINIC_CLOUD_COMDESK_IMPORT_ENABLED", value)
        assert cloud_write_feature_enabled("CLINIC_CLOUD_COMDESK_IMPORT_ENABLED") is True


def test_other_values_stay_disabled(monkeypatch):
    for value in ("0", "false", "no", "", "enabled-ish"):
        monkeypatch.setenv("CLINIC_CLOUD_COMDESK_IMPORT_ENABLED", value)
        assert cloud_write_feature_enabled("CLINIC_CLOUD_COMDESK_IMPORT_ENABLED") is False
