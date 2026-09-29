from mhlw_dry_run.rule_c_identity_rules import classify_identity, stale_or_move


def test_normal_closure_schedule_is_not_stale():
    for text in ("木曜休診", "祝日休診", "午後休診"):
        assert stale_or_move(text) == ""


def test_explicit_stale_phrases():
    for text in ("閉院いたしました", "このドメインは販売中", "parked domain", "404 not found", "サイト利用不可"):
        assert stale_or_move(text) == "STALE"


def test_move_is_review():
    assert stale_or_move("移転しました") == "MOVE"


def test_cross_medical_type_is_never_matched():
    status, reason, _ = classify_identity(same_medical_type=False, stale_kind="", cm_name=True,
        mhlw_name=True, cm_address=True, mhlw_address=True, cm_phone=True, mhlw_phone=True)
    assert (status, reason) == ("HP_CONFLICT_REVIEW", "MEDICAL_TYPE_MISMATCH")


def test_same_medical_mall_address_alone_is_never_matched():
    status, _, _ = classify_identity(same_medical_type=True, stale_kind="", cm_name=False,
        mhlw_name=False, cm_address=True, mhlw_address=True, cm_phone=False, mhlw_phone=False)
    assert status == "HP_CONFLICT_REVIEW"
