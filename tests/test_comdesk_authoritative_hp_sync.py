"""Contract tests for Comdesk-authoritative HP rank preview.

Do not require a live DB and never write to production.
"""
import unittest

import pandas as pd

from src.services.comdesk_sync import plan_sync


class FakeClinicRepository:
    def __init__(self, data):
        self.data = data

    def comdesk_sync_clinics(self, uuids):
        return {uid: self.data.get(uid, []) for uid in uuids}


class ComdeskAuthoritativeHpSyncTest(unittest.TestCase):
    def _plan(self, rank, *, manual=True):
        headers = ["UUID", "HPランク", "URL", "メールアドレス"]
        table = type("Table", (), {
            "headers": headers,
            "data": pd.DataFrame([["COMDESK-1", rank, "", ""]], columns=headers),
        })()
        repo = FakeClinicRepository({
            "COMDESK-1": [{
                "id": 123,
                "hp_rank": "C",
                "hp_rank_manual": manual,
                "hp_url": "",
                "email": "",
            }]
        })
        return plan_sync(table, repo)

    def test_comdesk_rank_replaces_prior_manual_rank(self):
        plan = self._plan("A")
        row = plan["rows"][0]
        self.assertEqual(row["status"], "AUTO_UPDATE")
        self.assertEqual(row["changes"]["hp_rank"], {"before": "C", "after": "A"})

    def test_invalid_rank_remains_on_hold(self):
        plan = self._plan("Z")
        self.assertEqual(plan["rows"][0]["status"], "HOLD")

    def test_unchanged_rank_does_not_write(self):
        plan = self._plan("C")
        self.assertEqual(plan["rows"][0]["status"], "NO_CHANGE")


if __name__ == "__main__":
    unittest.main()
