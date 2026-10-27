"""Safety tests for the dedicated Mac Supabase reader launcher (no DB needed)."""
import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "launch_v2_mac_reader", ROOT / "scripts/launch_v2_mac_reader.py"
)
reader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reader)


class Cursor:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchone(self):
        return next(self.answers)

    def fetchall(self):
        return []


class Connection:
    def __init__(self, answers):
        self.cursor_obj = Cursor(answers)
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def cursor(self):
        return self.cursor_obj

    def rollback(self):
        self.rolled_back = True


class CursorContext:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self._cursor

    def __exit__(self, *_):
        return False


Connection.cursor = lambda self: CursorContext(self.cursor_obj)


def successful_answers():
    answers = [("clinic_preview_reader", False, False, False, False, False),
               (False,), (False,), (False,)]
    for name in reader.REQUIRED_TABLES:
        answers += [(name,), (True,)]
    return answers + [(164201,)]


class ReaderSafetyTests(unittest.TestCase):
    def test_missing_reader_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "設定がありません"):
                reader.load_reader_dsn(Path(tmp) / "missing")

    def test_reader_file_requires_private_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "reader.env"
            p.write_text("SUPABASE_RUNTIME_DB_URL=postgresql://example.invalid/db\n")
            p.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "600"):
                reader.load_reader_dsn(p)
            p.chmod(0o600)
            self.assertEqual(
                reader.load_reader_dsn(p), "postgresql://example.invalid/db"
            )

    def test_writer_role_is_rejected(self):
        conn = Connection([("clinic_runtime", False, False, False, False, False)])
        with self.assertRaisesRegex(RuntimeError, "Writer"):
            reader.verify_reader("fake-url", connect=lambda *a, **kw: conn)

    def test_write_privilege_is_rejected(self):
        conn = Connection([
            ("clinic_preview_reader", False, False, False, False, False),
            (False,), (True,),
        ])
        with self.assertRaisesRegex(RuntimeError, "書き込み権限"):
            reader.verify_reader("fake-url", connect=lambda *a, **kw: conn)

    def test_valid_reader_is_accepted(self):
        conn = Connection(successful_answers())
        role, count = reader.verify_reader("fake-url", connect=lambda *a, **kw: conn)
        self.assertEqual((role, count), ("clinic_preview_reader", 164201))
        self.assertTrue(conn.rolled_back)

    def test_normal_and_preview_environments_are_separate_and_read_only(self):
        source = {
            "CLINIC_DB_PATH": "/must/not/be/used.sqlite3",
            "CLINIC_DATA_BACKEND": "sqlite",
            "CLINIC_WRITE_BACKEND": "sqlite",
            "EMAIL_AUTO_RUN_ENABLED": "true",
            "OUTREACH_GMAIL_DRAFTS_ENABLED": "true",
        }
        normal = reader.runtime_environment("reader-dsn", "normal", source)
        preview = reader.runtime_environment("preview-dsn", "preview", source)
        self.assertEqual(normal["SUPABASE_RUNTIME_DB_URL"], "reader-dsn")
        self.assertEqual(normal["CLINIC_DATA_BACKEND"], "supabase")
        self.assertEqual(normal["CLINIC_READ_ONLY_UI"], "1")
        self.assertEqual(normal["CLINIC_COMDESK_PREVIEW_ONLY"], "0")
        self.assertEqual(preview["CLINIC_COMDESK_PREVIEW_ONLY"], "1")
        self.assertNotIn("CLINIC_DB_PATH", normal)
        self.assertEqual(normal["EMAIL_AUTO_RUN_ENABLED"], "false")
        self.assertEqual(normal["OUTREACH_GMAIL_DRAFTS_ENABLED"], "false")
        self.assertIn("public.clinic_cloudrun_readonly", reader.NORMAL_REQUIRED_TABLES)
        self.assertIn("hp_research.clinic_hp_research", reader.NORMAL_REQUIRED_TABLES)
        self.assertNotIn("provenance.google_maps_results", reader.NORMAL_REQUIRED_TABLES)
        self.assertNotIn("email_research.email_candidates", reader.NORMAL_REQUIRED_TABLES)

    def test_normal_preflight_counts_visible_rows_from_projected_view(self):
        answers = [("clinic_mac_reader", False, False, False, False, False),
                   (False,), (False,), (False,)]
        for relation in reader.NORMAL_REQUIRED_TABLES:
            answers += [(relation,), (True,)]
        answers += [(164201,), (0,)]
        conn = Connection(answers)
        role, count = reader.verify_reader(
            "fake-url", connect=lambda *a, **kw: conn,
            required_tables=reader.NORMAL_REQUIRED_TABLES,
            count_relation="public.clinic_cloudrun_readonly",
        )
        self.assertEqual((role, count), ("clinic_mac_reader", 164201))
        self.assertIn("SELECT count(*) FROM email_research.clinic_email_status_readonly",
                      conn.cursor_obj.executed[-1][0])
        self.assertTrue(any("research_status,candidate_count,researched_at" in query
                            for query, _ in conn.cursor_obj.executed))


if __name__ == "__main__":
    unittest.main()
