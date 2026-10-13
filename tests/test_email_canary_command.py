import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts.clinic_email_canary import (
    CANDIDATE_FIELDS, CLINIC_RESULT_FIELDS, SchemaDriftError, finalize_output, main,
    make_raw_record, prepare_cohort, summarize_records, write_csv,
)


def write(path, ids):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["clinic_id", "clinic_name", "address"])
        writer.writeheader()
        for cid in ids:
            writer.writerow({"clinic_id": cid, "clinic_name": f"Clinic {cid}", "address": "東京都港区"})


def test_canary_cohort_is_non_overlapping_and_plan_does_not_create_output(tmp_path, capsys):
    source, previous = tmp_path / "source.csv", tmp_path / "previous.csv"
    write(source, range(1, 105)); write(previous, range(1, 5))
    cohort = prepare_cohort(source, [previous], 100)
    assert len(cohort) == 100 and {c["clinic_id"] for c in cohort}.isdisjoint(range(1, 5))
    assert cohort[0]["clinic_uuid"] == "legacy-id:5"
    assert cohort[0]["clinic_uuid_kind"] == "MISSING_UUID"
    output = tmp_path / "new-artifact"
    assert main(["--input", str(source), "--exclude", str(previous), "--output", str(output)]) == 0
    assert not output.exists()
    assert '"production_db_write": false' in capsys.readouterr().out


def test_canary_never_overwrites_existing_artifact(tmp_path):
    source, output = tmp_path / "source.csv", tmp_path / "existing"
    write(source, range(1, 101)); output.mkdir()
    with pytest.raises(SystemExit, match="never overwritten"):
        main(["--input", str(source), "--output", str(output)])


def test_fixed_candidate_schema_ignores_first_row_shape_and_blanks_optional_fields(tmp_path):
    path = tmp_path / "candidates.csv"
    rows = [{"clinic_id": 1, "email": "a@example.jp"},
            {"clinic_id": 2, "email": "b@example.jp", "rule_version": "email-v2",
             "auto_identity_decision": "TRUE", "clinic_name_match": True}]
    # Optional schema fields may be absent, while later rows can carry other known fields.
    write_csv(path, rows, CANDIDATE_FIELDS)
    with path.open(newline="", encoding="utf-8-sig") as handle:
        saved = list(csv.DictReader(handle))
    assert saved[0]["rule_version"] == ""
    assert saved[1]["rule_version"] == "email-v2"
    assert saved[1]["auto_identity_decision"] == "TRUE"


def test_unknown_field_is_reported_as_schema_drift(tmp_path):
    with pytest.raises(SchemaDriftError, match="schema drift.*unmodeled_field"):
        write_csv(tmp_path / "drift.csv", [{"clinic_id": 1}, {"unmodeled_field": "x"}], CANDIDATE_FIELDS)
    assert not (tmp_path / "drift.csv").exists()


def _raw(clinic_id, uuid, *, decision="", search_count=0):
    return {
        "clinic_id": clinic_id, "clinic_uuid": uuid, "clinic_uuid_kind": "REAL_UUID",
        "clinic_name": f"Clinic {clinic_id}", "research_status": "NO_EMAIL_FOUND",
        "candidate_found_count": 0, "valid_candidate_count": 0,
        "auto_true_count": int(decision == "AUTO_TRUE"), "auto_false_count": int(decision == "AUTO_FALSE"),
        "review_count": int(decision == "REVIEW"), "primary_count": 0,
        "search_query_count": search_count, "query1_called": search_count >= 1, "query2_called": search_count >= 2,
        "official_hp_checked": True, "official_hp_candidate_count": 0, "navii_checked": False,
        "navii_candidate_count": 0, "http_attempt_count": 2, "search_http_attempt_count": 1,
        "web_fetch_attempt_count": 1, "retry_count": 0, "timeout_count": 0, "elapsed_seconds": 1.25,
        "error": "", "source_candidate_counts": {"OFFICIAL_HP": 0, "NAVII": 0, "SEARCH_QUERY_1": 0, "SEARCH_QUERY_2": 0},
        "candidates": [], "worker_duplicate": False, "search_query_over_2": search_count > 2,
    }


def test_100_raw_results_rebuild_csv_and_summary(tmp_path):
    output = tmp_path / "retry"
    output.mkdir()
    cohort = [{"clinic_id": n, "clinic_uuid": f"uuid-{n}", "clinic_uuid_kind": "REAL_UUID",
               "clinic_name": f"Clinic {n}", "prefecture": "Tokyo", "city": "Minato",
               "address": "", "phone": "", "hp_url": ""} for n in range(1, 101)]
    write_csv(output / "cohort.csv", cohort, ("clinic_id", "clinic_uuid", "clinic_uuid_kind", "clinic_name",
              "prefecture", "city", "address", "phone", "hp_url"))
    records = [_raw(n, f"uuid-{n}", search_count=2 if n == 1 else 0) for n in range(1, 101)]
    with (output / "raw_results.jsonl").open("x", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    summary = finalize_output(output, cohort, records, 12.5)
    assert summary["processed"] == 100
    assert summary["search_query_count"] == 2
    assert summary["search_query_over_2_clinic_count"] == 0
    assert summary["clinic_uuid_missing_count"] == 0
    with (output / "clinic_results.csv").open(newline="", encoding="utf-8-sig") as handle:
        saved = list(csv.DictReader(handle))
    assert len(saved) == 100
    assert saved[0]["query1_called"] == "True" and saved[0]["query2_called"] == "True"
    assert (output / "summary.json").is_file()


def test_raw_survives_summary_generation_failure(tmp_path, monkeypatch):
    output = tmp_path / "artifact"
    output.mkdir()
    raw_path = output / "raw_results.jsonl"
    raw_path.write_text('{"clinic_id": 1}\n', encoding="utf-8")
    import scripts.clinic_email_canary as canary
    original = canary.summarize_records
    monkeypatch.setattr(canary, "summarize_records", lambda *args: (_ for _ in ()).throw(RuntimeError("summary failed")))
    with pytest.raises(RuntimeError, match="summary failed"):
        canary.finalize_output(output, [], [], 0)
    assert raw_path.read_text(encoding="utf-8") == '{"clinic_id": 1}\n'
    monkeypatch.setattr(canary, "summarize_records", original)


def test_summary_detects_query_over_two_uuid_missing_and_duplicate():
    cohort = [
        {"clinic_id": 1, "clinic_uuid": "dup", "clinic_uuid_kind": "REAL_UUID"},
        {"clinic_id": 2, "clinic_uuid": "dup", "clinic_uuid_kind": "REAL_UUID"},
        {"clinic_id": 3, "clinic_uuid": "", "clinic_uuid_kind": "MISSING_UUID"},
    ]
    records = [_raw(1, "dup", search_count=3), _raw(2, "dup"), _raw(3, "")]
    summary = summarize_records(cohort, records, 3)
    assert summary["search_query_over_2_clinic_count"] == 1
    assert summary["clinic_uuid_missing_count"] == 1
    assert summary["duplicate_uuid_count"] == 1
    assert summary["clinic_uuid_legacy_id_count"] == 0


def test_clinic_result_schema_has_required_telemetry():
    required = {"search_query_count", "query1_called", "query2_called", "official_hp_checked",
                "official_hp_candidate_count", "navii_checked", "navii_candidate_count",
                "http_attempt_count", "retry_count", "timeout_count", "elapsed_seconds"}
    assert required <= set(CLINIC_RESULT_FIELDS)


def test_email_decision_and_discovery_rules_remain_frozen():
    from src.master.email_sales_eligibility import (
        HARD_BLOCK_ROLES, PRIMARY_ROLES, PRIMARY_OWNERSHIP,
        TRUSTED_PUBLIC_DIRECTORY_HOSTS,
    )
    from src.enrichment.email_research.formal_pipeline import research_clinic
    assert HARD_BLOCK_ROLES == {"RESERVATION", "RECRUIT", "RECRUITING", "MEDICAL_COOPERATION"}
    assert PRIMARY_ROLES == {"GENERAL", "OFFICE"}
    assert PRIMARY_OWNERSHIP == {"CLINIC_OWNED", "SAME_MEDICAL_CORPORATION"}
    assert TRUSTED_PUBLIC_DIRECTORY_HOSTS == {"iryou.teikyouseido.mhlw.go.jp"}
    assert "exactly two conditional Search API calls" in research_clinic.__doc__
    source = Path(__file__).resolve().parents[1] / "src/enrichment/email_research/formal_pipeline.py"
    text = source.read_text(encoding="utf-8")
    assert "enumerate(queries, start=1):  # invariant: statically at most two" in text
    assert "assert metrics[\"search_query_count\"] <= 2" in text


def test_stage_telemetry_survives_raw_result_and_summary():
    clinic = {"clinic_id": 8, "clinic_uuid": "uuid-8", "clinic_uuid_kind": "REAL_UUID",
              "clinic_name": "Clinic 8", "hp_url": "https://clinic.example/"}
    metrics = {
        "search_query_count": 1, "http_attempts": 3, "web_fetch_attempts": 4,
        "retry_count": 1, "timeout_count": 2, "web_fetch_timeout": 1,
        "stage_elapsed_official_hp": 1.1, "stage_elapsed_query1": 2.2,
        "stage_elapsed_query2": 0.0, "stage_elapsed_verification": 0.8,
        "official_hp_fetch_attempts": 2, "verification_fetch_attempts": 2,
        "search_query1_attempts": 2, "search_query2_attempts": 0,
        "search_timeout": 2,
        "retry_by_stage": {"OFFICIAL_HP": 0, "NAVII": 0, "QUERY_1": 1, "QUERY_2": 0},
        "source_counts": {"OFFICIAL_HP": 1, "NAVII": 0, "SEARCH_QUERY_1": 1, "SEARCH_QUERY_2": 0},
    }
    record = make_raw_record({"clinic_id": 8, "research_status": "REVIEW", "candidates": [],
                              "metrics": metrics}, clinic,
                             SimpleNamespace(primary_minimum_confidence=None))
    assert record["stage_elapsed_query1"] == 2.2
    assert record["search_query1_attempts"] == 2
    assert record["verification_fetch_attempts"] == 2
    assert record["retry_by_stage"]["QUERY_1"] == 1
    assert {"stage_elapsed_query1", "search_query1_attempts", "verification_fetch_attempts",
            "search_timeout", "web_fetch_timeout"} <= set(CLINIC_RESULT_FIELDS)
    summary = summarize_records([clinic], [record], 3.0)
    assert summary["stage_elapsed_query1"] == 2.2
    assert summary["retry_by_stage"]["QUERY_1"] == 1
