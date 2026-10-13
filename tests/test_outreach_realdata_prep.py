import json
from datetime import date

import pytest

from src.outreach.compose import ARTICLE_UNCONFIRMED, TEMPLATE_RECOMMEND
from src.outreach.email_template import five_weekdays
from src.outreach.generation import NEEDS_REVIEW, GenerationSettings, generate_batch
from src.outreach.readonly_access import (
    ReadOnlyAccessError, open_readonly_clinic_repository, readonly_url, verify_read_only_role,
)
from src.outreach.staff import load_staff, validate_staff_name
from src.outreach.targets import select_targets
from tests.test_outreach_mail_send_text import ARTICLES, record


# --- ② 記事JOIN CSVなし ---
def test_without_join_all_unconfirmed_and_safe_text_generated():
    records = [record(i) for i in range(1, 6)]
    sel = select_targets(records, email_lookup=lambda ids: {i: f"c{i}@example.jp" for i in ids}, links=[])
    assert {t.article.status for t in sel.targets} == {ARTICLE_UNCONFIRMED}
    results = generate_batch(sel.targets, ARTICLES, GenerationSettings("前田", five_weekdays(date(2026, 10, 19))))
    for r in results:
        assert r.status == NEEDS_REVIEW and r.template == TEMPLATE_RECOMMEND
        assert r.first.article.url in r.body
        assert "掲載いただいている" not in r.body and "掲載させていただいている" not in r.body
        assert "お電話" not in r.body


# --- ③ IS担当者設定 ---
def test_staff_yaml_add_and_change_without_code(tmp_path):
    path = tmp_path / "staff.yml"
    path.write_text("staff:\n  - 前田\n  - 山田\n  - 前田\n", encoding="utf-8")
    assert load_staff(path).names == ("前田", "山田")
    path.write_text("staff:\n  - 佐藤\n", encoding="utf-8")
    assert load_staff(path).names == ("佐藤",)


def test_staff_env_override_and_bad_entries(tmp_path):
    path = tmp_path / "other.yml"
    path.write_text('staff:\n  - "a\\nb"\n  - ""\n  - 鈴木\n', encoding="utf-8")
    config = load_staff(environ={"OUTREACH_IS_STAFF_CONFIG": str(path)})
    assert config.names == ("鈴木",) and len(config.errors) == 2
    assert load_staff(tmp_path / "missing.yml").errors
    (tmp_path / "broken.yml").write_text("staff: [", encoding="utf-8")
    assert load_staff(tmp_path / "broken.yml").names == ()
    assert validate_staff_name("x" * 31)


def test_repo_staff_config_is_valid_yaml():
    assert load_staff().errors == ()


# --- ④ READ ONLY権限ゲート ---
class FakeCursor:
    def __init__(self, privileges, exists=True):
        self.privileges, self.exists, self.result = privileges, exists, None
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=()):
        if "to_regclass" in sql:
            self.result = self.exists
        elif "has_table_privilege" in sql:
            self.result = params[-1] if len(params) == 2 else "SELECT"
            self.result = self.result in self.privileges
        else:
            self.result = None
    def fetchone(self): return (self.result,)


class FakeConn:
    def __init__(self, privileges, exists=True):
        self.cursor_obj, self.closed = FakeCursor(privileges, exists), False
    def cursor(self): return self.cursor_obj
    def close(self): self.closed = True


def test_readonly_url_required_and_not_runtime():
    with pytest.raises(ReadOnlyAccessError, match="CLINIC_READONLY_DB_URL"):
        readonly_url({})
    with pytest.raises(ReadOnlyAccessError):
        readonly_url({"CLINIC_READONLY_DB_URL": "postgres://x", "SUPABASE_RUNTIME_DB_URL": "postgres://x"})


def test_role_with_write_privilege_is_rejected_and_closed():
    conn = FakeConn({"SELECT", "UPDATE"})
    with pytest.raises(ReadOnlyAccessError, match="UPDATE"):
        open_readonly_clinic_repository({"CLINIC_READONLY_DB_URL": "postgres://ro"}, connect_fn=lambda url: conn)
    assert conn.closed


def test_role_without_select_or_table_is_rejected():
    with pytest.raises(ReadOnlyAccessError, match="SELECT権限なし"):
        verify_read_only_role(FakeConn(set()))
    with pytest.raises(ReadOnlyAccessError, match="参照できません"):
        verify_read_only_role(FakeConn({"SELECT"}, exists=False))


def test_select_only_role_passes():
    verify_read_only_role(FakeConn({"SELECT"}))


def test_canary_stops_without_permission_and_keeps_master(tmp_path, monkeypatch, capsys):
    from scripts import outreach_mail_canary as canary
    master = tmp_path / "master.csv"
    master.write_text("No.,タイトル,URL,KW\n1,世田谷区の内科,https://clinic.mynavi.jp/article/a/,世田谷 内科\n",
                      encoding="utf-8")
    before = master.read_bytes()
    monkeypatch.delenv("CLINIC_READONLY_DB_URL", raising=False)
    monkeypatch.setenv("OUTREACH_IS_STAFF_CONFIG", str(tmp_path / "none.yml"))
    code = canary.main(["--articles", str(master), "--staff", "前田", "--start", "2026-10-19",
                        "--out", str(tmp_path / "out")])
    assert code == canary.EXIT_PERMISSION
    assert "CLINIC_READONLY_DB_URL" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()
    assert master.read_bytes() == before


def test_canary_rejects_unregistered_staff(tmp_path, monkeypatch):
    from scripts import outreach_mail_canary as canary
    master = tmp_path / "master.csv"
    master.write_text("No.,タイトル,URL,KW\n", encoding="utf-8")
    staff = tmp_path / "staff.yml"
    staff.write_text("staff:\n  - 山田\n", encoding="utf-8")
    monkeypatch.setenv("OUTREACH_IS_STAFF_CONFIG", str(staff))
    assert canary.main(["--articles", str(master), "--staff", "前田", "--start", "2026-10-19",
                        "--out", str(tmp_path / "out")]) == canary.EXIT_INPUT
