from copy import deepcopy
import pytest
from src.scoring.succession_detector import Succession
from src.scoring.lead_ranker import rank_lead


def decide(**overrides):
    params = dict(eligible=True, gate_reasons=[], recent=True, young=True,
                  succession=Succession(True,False,0,[],False), epark="不明")
    params.update(overrides)
    return rank_lead(**params)


def test_four_patterns_and_ranks():
    assert decide().rank == "A"
    s = Succession(False,True,70,[],True)
    assert decide(succession=s, epark="課金済み").rank == "S"
    assert decide(succession=s, recent=False).include
    assert decide(succession=Succession(False,False,0,[],False), recent=False, epark="課金済み").include
    p4 = decide(succession=Succession(False,False,35,[],True))
    assert p4.status == "営業対象（要確認）" and p4.include
    assert decide(succession=s, epark="無課金").rank == "B"


def test_age_exclusion_has_all_three_conditions():
    assert decide(young=False,recent=False).status == "除外"
    assert decide(young=False,recent=True).status == "要確認"
    assert decide(young=False,recent=False,succession=Succession(False,True,80,[],True)).status == "要確認"


def test_missing_never_becomes_age_exclusion():
    assert decide(young=None, recent=False).status == "要確認"
    assert decide(young=None, options={"require_young":False}).status == "要確認"
    assert decide(eligible=None, gate_reasons=["未一致"]).status == "要確認"


def test_review_default_and_opt_in():
    assert not decide(young=None).include
    assert decide(young=None, options={"include_review_in_final":True}).include


def test_closed_always_excluded_even_when_paid():
    d = decide(eligible=False,gate_reasons=["休止"],epark="課金済み",options={"include_review_in_final":True})
    assert not d.include and d.status == "除外"


def test_condition_switches():
    assert decide(young=False,recent=False,options={"require_recent":False,"require_young":False}).include
    assert decide(succession=Succession(False,True,90,[],True),recent=False,options={"include_succession":False}).status == "要確認"
