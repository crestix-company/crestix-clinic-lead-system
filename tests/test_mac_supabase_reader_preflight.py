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
        answers += [(True,), (name,), (True,)]
    return answers + [(164201,)]


class ReaderSafetyTests(unittest.TestCase):
    def test_missing_reader_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "設定がありません"):
                reader.load_reader_dsn(Path(tmp) / "missing")

    def test_reader_file_requires_private_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "reader.env"
            p.write_text("SUPABASE_READONLY_DB_URL=postgresql://example.invalid/db\n")
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

    def test_schema_without_usage_is_rejected(self):
        conn = Connection([
            ("clinic_preview_reader", False, False, False, False, False),
            (False,), (False,), (False,), (False,),
        ])
        with self.assertRaisesRegex(RuntimeError, "スキーマの参照権限"):
            reader.verify_reader("fake-url", connect=lambda *a, **kw: conn)

    def test_valid_reader_is_accepted(self):
        conn = Connection(successful_answers())
        role, count = reader.verify_reader("fake-url", connect=lambda *a, **kw: conn)
        self.assertEqual((role, count), ("clinic_preview_reader", 164201))
        self.assertTrue(conn.rolled_back)


if __name__ == "__main__":
    unittest.main()
