"""scripts/reproject_legacy_departments.py のdry-run/apply(未実行)ロジック（synthetic fixture）。

Production DBは使わない。ここでのapply呼び出しはすべてtmp_path上の使い捨てDBに対してのみ行う。
"""
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.master.filters import Filters
from src.master.samples import sample_records
from src.master.scope import LEGACY_PRE_NATIONAL_CUTOFF
from src.master.store import ClinicStore
from src.normalizer.departments import normalize_departments

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.reproject_legacy_departments import (  # noqa: E402
    ApplyAborted, classify, fetch_legacy_rows, main, parse_args, run_apply, run_dry_run, write_artifacts,
)

ALL = dict(active_only=False, hp_only=False)
CUTOFF_DT = datetime.fromisoformat(LEGACY_PRE_NATIONAL_CUTOFF)


# ---- classify(): pure logic, hand-crafted rows -------------------------------
def test_classify_counts_stale_only_as_candidate():
    rows = [
        (1, json.dumps({"departments": "眼科"}), json.dumps([])),  # stale: stored=[] fresh=[眼科]
        (2, json.dumps({"departments": "眼科"}), json.dumps(["眼科"])),  # equal
    ]
    result = classify(rows)
    assert result.candidate_updates == 1
    assert result.stored_fresh_equal == 1
    assert [c[0] for c in result.candidates] == [1]


def test_classify_skips_raw_missing_from_candidates():
    rows = [(3, json.dumps({"departments": ""}), json.dumps([]))]
    result = classify(rows)
    assert result.raw_empty == 1
    assert result.raw_nonempty == 0
    assert result.candidate_updates == 0
    assert result.candidates == []


def test_classify_separates_normalizer_unresolved_from_equal_and_candidate():
    fresh_unresolved = normalize_departments("謎の科")
    assert fresh_unresolved == ["その他"]
    rows = [
        (4, json.dumps({"departments": "謎の科"}), json.dumps(fresh_unresolved)),  # stored==fresh==[その他]
    ]
    result = classify(rows)
    assert result.normalizer_unresolved == 1
    assert result.stored_fresh_equal == 0
    assert result.candidate_updates == 0


def test_classify_beauty_union_counts_per_clinic_not_summed_naively():
    rows = [
        (5, json.dumps({"departments": "美容外科"}), json.dumps([])),  # candidate: stored=[] fresh=[美容外科]
        (6, json.dumps({"departments": "美容整形外科 美容外科"}), json.dumps(["美容整形外科", "美容外科"])),  # equal, both members
    ]
    result = classify(rows)
    assert result.beauty_union_after == 2  # クリニック5,6のどちらもunion対象（union、和ではない）
    assert result.beauty_union_before == 1  # クリニック6のみ（stored側）


def test_classify_matches_known_production_baseline_totals_shape():
    # 監査済みbaseline（legacy_total=13970, raw_nonempty=13954, raw_empty=16,
    # stored_fresh_equal=5431, normalizer_unresolved=437, candidate_updates=8086）と
    # 同じ分類ロジックで合計が矛盾しないことだけを確認する（実データ検証はProduction dry-runで行う）。
    rows = (
        [(i, json.dumps({"departments": ""}), json.dumps([])) for i in range(16)]
        + [(100 + i, json.dumps({"departments": "謎の科"}), json.dumps(["その他"])) for i in range(437)]
        + [(200 + i, json.dumps({"departments": "眼科"}), json.dumps(["眼科"])) for i in range(5431)]
        + [(300 + i, json.dumps({"departments": "眼科"}), json.dumps([])) for i in range(8086)]
    )
    result = classify(rows)
    assert result.legacy_total == 13970
    assert result.raw_empty == 16
    assert result.raw_nonempty == 13954
    assert result.stored_fresh_equal == 5431
    assert result.normalizer_unresolved == 437
    assert result.candidate_updates == 8086


# ---- write_artifacts ----------------------------------------------------------
def test_write_artifacts_produces_expected_files(tmp_path):
    rows = [
        (1, json.dumps({"departments": "眼科"}), json.dumps([])),
        (2, json.dumps({"departments": ""}), json.dumps([])),
    ]
    result = classify(rows)
    out = tmp_path / "artifacts"
    summary = write_artifacts(out, result, {"sha256": "x"}, {"sha256": "x"}, expected_candidates=1)
    assert (out / "summary.json").exists()
    assert (out / "candidate_updates.csv").exists()
    assert (out / "department_counts_before_after.csv").exists()
    assert summary["candidate_match"] is True
    csv_text = (out / "candidate_updates.csv").read_text(encoding="utf-8-sig")
    assert "clinic_id,raw_departments,stored_departments,fresh_departments" in csv_text
    assert "眼科" in csv_text
    dept_text = (out / "department_counts_before_after.csv").read_text(encoding="utf-8-sig")
    assert "美容外科系" in dept_text


# ---- DB-backed fixtures -------------------------------------------------------
def _clinic(i, name, departments=""):
    row = dict(sample_records()[0])
    row.update({"clinic_id": f"reproj-{i}", "clinic_name": name, "phone": f"03-6666-{i:04d}",
                "address": f"東京都千代田区reproj町{i}-1-1", "departments": departments})
    return row


def _set_first_seen(store, clinic_id, iso_value):
    with store.connect() as c:
        c.execute("UPDATE clinics SET first_seen_at=? WHERE id=?", (iso_value, clinic_id))


def _force_departments_json(store, clinic_id, value):
    with store.connect() as c:
        c.execute("UPDATE clinics SET departments_json=? WHERE id=?", (json.dumps(value, ensure_ascii=False), clinic_id))


@pytest.fixture
def reproject_db(tmp_path):
    db_path = tmp_path / "reproject.db"
    store = ClinicStore(db_path)
    specs = [
        ("legacy_stale", "眼科"),       # legacy, raw nonempty, will be forced stale
        ("legacy_equal", "皮膚科"),     # legacy, raw nonempty, stored already fresh
        ("legacy_raw_empty", ""),      # legacy, raw empty
        ("national_stale_like", "眼科"),  # national append: must NOT be touched even if stored is stale
    ]
    store.import_master([_clinic(i, n, d) for i, (n, d) in enumerate(specs)])
    ids = {r["clinic_name"]: r["id"] for r in store.query(Filters(**ALL), limit=100)}
    for n in ("legacy_stale", "legacy_equal", "legacy_raw_empty"):
        _set_first_seen(store, ids[n], (CUTOFF_DT - timedelta(days=1)).isoformat())
    _set_first_seen(store, ids["national_stale_like"], (CUTOFF_DT + timedelta(days=1)).isoformat())
    # legacy_stale・national_stale_like をわざと古い（空の）departments_jsonにして「stale」を作る。
    _force_departments_json(store, ids["legacy_stale"], [])
    _force_departments_json(store, ids["national_stale_like"], [])
    return store, ids


def test_fetch_legacy_rows_excludes_national_append(reproject_db):
    store, ids = reproject_db
    conn = sqlite3.connect(store.path)
    try:
        rows = fetch_legacy_rows(conn)
    finally:
        conn.close()
    fetched_ids = {r[0] for r in rows}
    assert ids["national_stale_like"] not in fetched_ids
    assert {ids["legacy_stale"], ids["legacy_equal"], ids["legacy_raw_empty"]} <= fetched_ids


def test_classify_on_real_db_finds_only_legacy_candidate(reproject_db):
    store, ids = reproject_db
    conn = sqlite3.connect(store.path)
    try:
        rows = fetch_legacy_rows(conn)
    finally:
        conn.close()
    result = classify(rows)
    assert [c[0] for c in result.candidates] == [ids["legacy_stale"]]
    assert result.raw_empty == 1
    assert result.stored_fresh_equal == 1  # legacy_equal


def test_dry_run_does_not_mutate_production_like_db(reproject_db, tmp_path):
    store, ids = reproject_db
    before_bytes = store.path.read_bytes()
    summary, classification = run_dry_run(store.path, tmp_path / "out", expected_candidates=1)
    after_bytes = store.path.read_bytes()
    assert before_bytes == after_bytes
    assert summary["db_unchanged"] is True
    assert summary["candidate_updates"] == 1
    assert summary["candidate_match"] is True


def test_dry_run_reports_mismatch_without_raising(reproject_db, tmp_path):
    store, ids = reproject_db
    summary, classification = run_dry_run(store.path, tmp_path / "out", expected_candidates=999)
    assert summary["candidate_match"] is False
    assert summary["candidate_updates"] == 1  # 実際の値は変わらず正しく報告される


# ---- apply (このセッションでは呼ばないが、コードとしては検証する) ------------------
def test_apply_updates_only_departments_json_of_candidates(reproject_db, tmp_path):
    store, ids = reproject_db
    before_row = dict(zip(
        ["clinic_name", "base_json", "effective_json", "first_seen_at", "hp_status"],
        sqlite3.connect(store.path).execute(
            "SELECT clinic_name,base_json,effective_json,first_seen_at,hp_status FROM clinics WHERE id=?",
            (ids["legacy_stale"],),
        ).fetchone(),
    ))
    classification = run_apply(store.path, tmp_path / "out", expected_candidates=1)
    assert classification.candidate_updates == 1

    conn = sqlite3.connect(store.path)
    after_row = dict(zip(
        ["clinic_name", "base_json", "effective_json", "first_seen_at", "hp_status", "departments_json"],
        conn.execute(
            "SELECT clinic_name,base_json,effective_json,first_seen_at,hp_status,departments_json FROM clinics WHERE id=?",
            (ids["legacy_stale"],),
        ).fetchone(),
    ))
    assert json.loads(after_row["departments_json"]) == ["眼科"]
    for key in ("clinic_name", "base_json", "effective_json", "first_seen_at", "hp_status"):
        assert before_row[key] == after_row[key]

    # national appendの同名stale行は一切変更されない（scope外）。
    national_after = conn.execute(
        "SELECT departments_json FROM clinics WHERE id=?", (ids["national_stale_like"],)
    ).fetchone()[0]
    assert json.loads(national_after) == []
    conn.close()


def test_apply_is_fail_closed_on_candidate_mismatch(reproject_db, tmp_path):
    store, ids = reproject_db
    before_bytes = store.path.read_bytes()
    with pytest.raises(ApplyAborted):
        run_apply(store.path, tmp_path / "out", expected_candidates=999)
    after_bytes = store.path.read_bytes()
    assert before_bytes == after_bytes  # ROLLBACKされ、DBは一切変化しない


def test_apply_is_fail_closed_on_source_sha256_mismatch(reproject_db, tmp_path):
    store, ids = reproject_db
    before_bytes = store.path.read_bytes()
    with pytest.raises(ApplyAborted):
        run_apply(store.path, tmp_path / "out", expected_candidates=1, expected_source_sha256="deadbeef")
    after_bytes = store.path.read_bytes()
    assert before_bytes == after_bytes


def test_apply_is_idempotent_second_pass_has_zero_candidates(reproject_db, tmp_path):
    store, ids = reproject_db
    run_apply(store.path, tmp_path / "out1", expected_candidates=1)
    conn = sqlite3.connect(store.path)
    try:
        rows = fetch_legacy_rows(conn)
    finally:
        conn.close()
    result = classify(rows)
    assert result.candidate_updates == 0


# ---- CLI: --expected-candidates is required whenever --apply is used ----------
def test_parse_args_rejects_apply_without_expected_candidates(reproject_db, tmp_path):
    store, ids = reproject_db
    with pytest.raises(SystemExit) as exc:
        parse_args(["--db", str(store.path), "--apply", "--out", str(tmp_path / "out")])
    assert exc.value.code != 0


def test_main_apply_without_expected_candidates_fails_closed_and_writes_nothing(reproject_db, tmp_path):
    store, ids = reproject_db
    before_bytes = store.path.read_bytes()
    with pytest.raises(SystemExit) as exc:
        main(["--db", str(store.path), "--apply", "--out", str(tmp_path / "out")])
    assert exc.value.code != 0
    assert store.path.read_bytes() == before_bytes


def test_main_apply_with_matching_expected_candidates_proceeds(reproject_db, tmp_path):
    store, ids = reproject_db
    rc = main(["--db", str(store.path), "--apply", "--expected-candidates", "1", "--out", str(tmp_path / "out")])
    assert rc == 0
    conn = sqlite3.connect(store.path)
    try:
        departments = conn.execute(
            "SELECT departments_json FROM clinics WHERE id=?", (ids["legacy_stale"],)
        ).fetchone()[0]
    finally:
        conn.close()
    assert json.loads(departments) == ["眼科"]


def test_main_apply_with_candidate_mismatch_fails_closed_and_writes_nothing(reproject_db, tmp_path):
    store, ids = reproject_db
    before_bytes = store.path.read_bytes()
    rc = main(["--db", str(store.path), "--apply", "--expected-candidates", "999", "--out", str(tmp_path / "out")])
    assert rc == 1
    assert store.path.read_bytes() == before_bytes


def test_main_dry_run_without_expected_candidates_still_defaults_to_8086(reproject_db, tmp_path):
    # --apply を伴わない限り--expected-candidatesの省略は従来どおり許容し、8086がデフォルトのまま使われる。
    store, ids = reproject_db
    args = parse_args(["--db", str(store.path), "--out", str(tmp_path / "out")])
    assert args.expected_candidates == 8086
    before_bytes = store.path.read_bytes()
    rc = main(["--db", str(store.path), "--out", str(tmp_path / "out")])
    # このfixtureの実candidateは1件なので8086とは不一致になり、dry-runは既存仕様どおりFAIL CLOSED(非0)。
    assert rc == 1
    assert store.path.read_bytes() == before_bytes
