from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
ROLE_SQL = ROOT / "docs/supabase_migration/review/clinic_email_target_reader_role.sql"


def _sql():
    return ROLE_SQL.read_text(encoding="utf-8").casefold()


def test_role_review_sql_is_not_a_migration_and_never_changes_existing_roles():
    assert ROLE_SQL.exists()
    assert "review only" in _sql()
    assert not list((ROOT / "supabase/migrations").glob("*clinic_email_target_reader*"))
    sql = _sql()
    for existing_role in ("clinic_readonly", "clinic_runtime", "clinic_email_runtime"):
        assert not re.search(
            rf"\b(?:alter|drop)\s+role\s+{existing_role}\b|"
            rf"\bgrant\b[^;]*\bto\s+{existing_role}\b|"
            rf"\brevoke\b[^;]*\bfrom\s+{existing_role}\b",
            sql,
        )


def test_role_sql_grants_only_the_two_target_tables_and_select_policies():
    sql = _sql()
    assert "create role clinic_email_target_reader" in sql
    assert "password null" in sql
    assert "noinherit" in sql
    assert "nosuperuser" in sql and "nobypassrls" in sql
    assert "grant usage on schema public, hp_research" in sql
    assert re.search(
        r"grant select on table\s+public\.clinics,\s+"
        r"hp_research\.clinic_hp_research\s+to clinic_email_target_reader",
        sql,
    )
    assert "create policy clinic_email_target_reader_clinics_select" in sql
    assert "create policy clinic_email_target_reader_hp_select" in sql
    assert "for select" in sql
    assert "for all" not in sql
    assert not re.search(r"grant\s+(?:all|insert|update|delete|truncate|execute)\b", sql)
    assert "email_research" not in sql.split("-- validation", 1)[0]


def test_role_review_sql_documents_validation_and_full_rollback():
    sql = _sql()
    for required in (
        "current_user", "current_database()", "transaction_read_only",
        "has_table_privilege", "has_sequence_privilege", "has_schema_privilege",
        "clinic_email_target_reader'=any(roles)", "rollback (only after",
        "drop role clinic_email_target_reader",
    ):
        assert required in sql
