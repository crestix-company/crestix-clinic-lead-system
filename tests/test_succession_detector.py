from src.scoring.age_estimator import AgeEstimator
from src.scoring.succession_detector import detect_succession


def test_same_surname_alone_is_not_succession(config):
    result = detect_succession({"owner_name":"架空 太郎", "manager_name":"架空 次郎"},
                              AgeEstimator().estimate(2000,2026), config["succession"])
    assert result.owner_manager_equal is False
    assert result.candidate is False


def test_explicit_inheritance_survives_corporate_owner(config):
    result = detect_succession({"owner_name":"医療法人社団 空想会", "manager_name":"架空 次郎", "registration_reason":"継承"},
                              AgeEstimator().estimate(2000,2026), config["succession"])
    assert result.candidate
    assert result.owner_manager_equal is None


def test_chairperson_extraction(config):
    result = detect_succession({"owner_name":"医療法人 空想会 理事長 架空 太郎", "manager_name":"架空 太郎"},
                              AgeEstimator().estimate(2000,2026), config["succession"])
    assert result.owner_manager_equal is True


def test_negative_profile_is_not_evidence(config):
    result = detect_succession({"owner_name":"架空 太郎", "manager_name":"別名 次郎", "profile_text":"親子ではない。継承していない。"},
                              AgeEstimator().estimate(2000,2026), config["succession"])
    assert result.candidate is False


def test_profile_succession(config):
    result = detect_succession({"owner_name":"架空 太郎", "manager_name":"架空 次郎", "profile_text":"父から承継し、二代目院長に就任。"},
                              AgeEstimator().estimate(2000,2026), config["succession"])
    assert result.candidate
