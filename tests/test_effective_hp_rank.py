import pytest

from src.master.hp_effective_rank import effective_hp_rank, effective_rank_reason


@pytest.mark.parametrize("machine,old,expected", [
    ("A", "D", "A"), ("B", "D", "B"), ("C", "A", "C"), ("D", "A", "D"),
    ("UNKNOWN", "A", "A"), ("UNKNOWN", "B", "B"), ("UNKNOWN", "C", "D"),
    ("UNKNOWN", "D", "D"), ("UNKNOWN", "UNKNOWN", "D"),
    ("UNKNOWN", "NO_HP", "D"), ("UNKNOWN", None, "D"), ("NO_HP", "A", "D"),
])
def test_effective_hp_rank_all_branches(machine, old, expected):
    assert effective_hp_rank(machine, old) == expected


def test_unknown_old_b_reason_is_traceable():
    assert effective_rank_reason("UNKNOWN", "B") == "旧B踏襲"


def test_treatment_is_not_an_input_to_effective_rank():
    assert effective_hp_rank("B", "D") == "B"
