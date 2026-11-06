"""Pure planning tests for the Comdesk UUID-only reimport.

No database connection or production writes are used.
"""
import unittest

from src.services.comdesk_uuid_reimport import build_uuid_only_plan


def case(uid, phone="03-1111-2222", name="中央クリニック", addr="東京都千代田区1"):
    return {"row_number": 2, "uuid": uid, "phone": phone,
            "clinic_name": name, "address": addr}


def clinic(cid=10, uuid="", phone="0311112222", name="中央クリニック",
           addr="東京都千代田区1"):
    return {"id": cid, "uuid": uuid, "phone": phone,
            "clinic_name": name, "address": addr}


class TestUuidOnlyPlanning(unittest.TestCase):
    def test_one_matching_unassigned_clinic_is_update(self):
        plan = build_uuid_only_plan(
            [case("CD-001")], {"0311112222": [clinic()]})
        self.assertEqual(plan[0]["status"], "UPDATE")
        self.assertEqual(plan[0]["clinic_id"], 10)

    def test_prior_uuid_is_not_overwritten(self):
        plan = build_uuid_only_plan(
            [case("CD-002")], {"0311112222": [clinic(uuid="CD-OLD")]})
        self.assertEqual(plan[0]["status"], "HOLD")

    def test_two_identical_candidates_are_held(self):
        plan = build_uuid_only_plan(
            [case("CD-001")], {"0311112222": [clinic(), clinic(cid=11)]})
        self.assertEqual(plan[0]["status"], "HOLD")

    def test_uuid_used_elsewhere_is_held(self):
        plan = build_uuid_only_plan(
            [case("CD-001")], {"0311112222": [clinic()]}, existing_uuids=["CD-001"])
        self.assertEqual(plan[0]["status"], "HOLD")

    def test_duplicate_uuid_in_file_is_held(self):
        rows = [case("CD-001"), {**case("CD-001"), "row_number": 3}]
        plan = build_uuid_only_plan(rows, {"0311112222": [clinic()]})
        self.assertTrue(all(x["status"] == "HOLD" for x in plan))

    def test_different_name_and_address_is_held(self):
        plan = build_uuid_only_plan(
            [case("CD-001", name="別医院", addr="別住所")],
            {"0311112222": [clinic()]})
        self.assertEqual(plan[0]["status"], "HOLD")

    def test_blank_uuid_is_held(self):
        plan = build_uuid_only_plan(
            [case("")], {"0311112222": [clinic()]})
        self.assertEqual(plan[0]["status"], "HOLD")


if __name__ == "__main__":
    unittest.main()
