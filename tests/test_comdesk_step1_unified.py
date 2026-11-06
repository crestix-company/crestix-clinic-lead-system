"""Step1 Comdesk rank projection tests; no DB connection."""
import unittest
from types import SimpleNamespace

import pandas as pd

from src.services.comdesk_step1 import rank_only_table


class Step1RankTableTest(unittest.TestCase):
    def test_extract_rank_without_touching_hp_url_or_email(self):
        headers = ["UUID", "HPランク", "URL", "メールアドレス"]
        raw = pd.DataFrame([["CD-1", " A ", "https://existing.invalid/", "private@example.test"]], columns=headers)
        table = rank_only_table(SimpleNamespace(headers=headers, data=raw))
        self.assertEqual(table.data.iloc[0]["HPランク"], "A")
        self.assertEqual(table.data.iloc[0]["URL"], "")
        self.assertEqual(table.data.iloc[0]["メールアドレス"], "")

    def test_reject_file_without_rank_column(self):
        headers = ["UUID", "URL", "メールアドレス"]
        raw = pd.DataFrame([["CD-1", "https://example.test/", "x@example.test"]], columns=headers)
        with self.assertRaises(ValueError):
            rank_only_table(SimpleNamespace(headers=headers, data=raw))


if __name__ == "__main__":
    unittest.main()
