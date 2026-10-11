import pytest

from src.repository.email_cohort import load_cohort, load_cohort_from_env


def _write(tmp_path, body):
    path = tmp_path / "cohort.csv"
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_valid_cohort_loads(tmp_path):
    path = _write(tmp_path, "clinic_id,clinic_name\n1,A\n2,B\n3,C\n")
    assert load_cohort(path) == frozenset({1, 2, 3})


def test_expected_count_mismatch_fails_closed(tmp_path):
    path = _write(tmp_path, "clinic_id\n1\n2\n")
    with pytest.raises(ValueError, match="100件"):
        load_cohort(path, expected_count=100)


def test_expected_count_match_passes(tmp_path):
    rows = "\n".join(str(i) for i in range(1, 101))
    path = _write(tmp_path, "clinic_id\n" + rows + "\n")
    assert len(load_cohort(path, expected_count=100)) == 100


def test_missing_file_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="見つかりません"):
        load_cohort(str(tmp_path / "does_not_exist.csv"))


def test_empty_file_fails_closed(tmp_path):
    path = _write(tmp_path, "clinic_id\n")
    with pytest.raises(ValueError, match="行がありません"):
        load_cohort(path)


def test_missing_column_fails_closed(tmp_path):
    path = _write(tmp_path, "id,clinic_name\n1,A\n")
    with pytest.raises(ValueError, match="clinic_id列"):
        load_cohort(path)


def test_duplicate_clinic_id_fails_closed(tmp_path):
    path = _write(tmp_path, "clinic_id\n1\n2\n1\n")
    with pytest.raises(ValueError, match="重複"):
        load_cohort(path)


def test_non_integer_clinic_id_fails_closed(tmp_path):
    path = _write(tmp_path, "clinic_id\n1\nabc\n")
    with pytest.raises(ValueError, match="整数ではありません"):
        load_cohort(path)


def test_blank_clinic_id_fails_closed(tmp_path):
    # A trailing blank *line* is swallowed by csv and never becomes a row at all; a
    # genuinely blank clinic_id field (second column non-empty) is what must be rejected.
    path = _write(tmp_path, "clinic_id,note\n1,x\n,y\n")
    with pytest.raises(ValueError, match="空です"):
        load_cohort(path)


def test_env_unset_returns_none_canary_mode_inactive():
    assert load_cohort_from_env({}) is None
    assert load_cohort_from_env({"EMAIL_RESEARCH_COHORT_FILE": ""}) is None


def test_env_set_but_invalid_raises_no_fallback_to_none(tmp_path):
    """Canary mode=true (env var set) with a bad file must raise, never resolve to None
    (which would mean "no cohort restriction" -- the one outcome this must never produce)."""
    path = _write(tmp_path, "clinic_id\n1\n1\n")
    with pytest.raises(ValueError):
        load_cohort_from_env({"EMAIL_RESEARCH_COHORT_FILE": path})


def test_env_set_and_valid_loads(tmp_path):
    path = _write(tmp_path, "clinic_id\n1\n2\n")
    assert load_cohort_from_env({"EMAIL_RESEARCH_COHORT_FILE": path}) == frozenset({1, 2})
