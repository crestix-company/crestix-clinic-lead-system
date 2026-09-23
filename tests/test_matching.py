import pandas as pd
from src.matching.kouseikyoku_matcher import KouseikyokuMatcher


def matcher(config, records=None):
    return KouseikyokuMatcher(pd.DataFrame(records or [
        {"clinic_id":"1","clinic_name":"架空医院","phone":"0300000001","address":"東京都架空区1-1"},
        {"clinic_id":"2","clinic_name":"別の医院","phone":"0300000002","address":"東京都架空区1-1"},
    ]),config["matching"])


def test_phone_exact(config):
    assert matcher(config).match("03-0000-0001","架空医院","東京都架空区1-1").record["clinic_id"]=="1"


def test_shared_building_does_not_merge_wrong_clinic(config):
    result=matcher(config).match("","無関係な整形外科","東京都架空区1-1")
    assert result.record is None


def test_duplicate_phone_is_not_first_match(config):
    records=[{"clinic_id":str(i),"clinic_name":n,"phone":"0300000001","address":"東京都架空区1-1"}
             for i,n in enumerate(["架空医院","別医院"])]
    result=matcher(config,records).match("0300000001","","")
    assert result.record is None and len(result.candidates)==2
    assert matcher(config,records).match("0300000001","架空医院","東京都架空区1-1").record["clinic_id"]=="0"


def test_same_name_other_location_is_review(config):
    assert matcher(config).match("","架空医院","北海道札幌市中央区100-50").record is None


def test_fuzzy_without_address_stays_review(config):
    assert matcher(config).match("0300000003","架空クリニック","").record is None
