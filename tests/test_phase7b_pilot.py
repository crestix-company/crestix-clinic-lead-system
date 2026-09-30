import csv
import io

from src.enrichment.hp_analysis import is_official_candidate, sanitize_text
from src.enrichment.treatment_taxonomy import evaluate_treatment_evidence
from scripts.phase7b_pilot import build_sample, choose_identity_url, _read_existing_results


def test_sampling_candidate_buckets_are_strata_and_sample_is_repeatable():
    categories = {"test treatment": {"status": "ACTIVE", "crestix_departments": ["眼科"]}}
    records = [
        {"clinic_id": i, "clinic_name": f"clinic-{i}", "crestix_departments": ["眼科"],
         "selected_identity_url": f"https://clinic{i}.example/", "url_source": "MAPS_OFFICIAL_WEBSITE",
         "legacy_signal": {"test treatment": i < 3}, "eligible_categories": ["test treatment"]}
        for i in range(1, 7)
    ]
    first, summary = build_sample(records, categories, target=6)
    second, _ = build_sample(records, categories, target=6)
    assert first == second
    assert summary["unique_clinics"] == 6
    assert sum(row["candidate_bucket"] == "POSITIVE_CANDIDATE" for row in first) == 2
    assert sum(row["candidate_bucket"] == "NEGATIVE_CANDIDATE" for row in first) == 4
    assert {row["candidate_bucket"] for row in first} <= {"POSITIVE_CANDIDATE", "NEGATIVE_CANDIDATE"}


def test_url_priority_uses_maps_then_saved_hp_and_rejects_portal():
    row = {"maps_presence_status": "MAPS_MATCHED_WEBSITE", "maps_website_url": "https://maps.google.com/clinic",
           "hp_url": "https://official-clinic.example/"}
    url, source = choose_identity_url(row)
    assert (url, source) == ("https://official-clinic.example/", "SAVED_HP_URL")
    row["maps_website_url"] = "https://clinic.example/"
    url, source = choose_identity_url(row)
    assert (url, source) == ("https://clinic.example/", "MAPS_OFFICIAL_WEBSITE")
    assert not is_official_candidate("https://www.caloo.jp/hospitals/clinic")


def test_sanitize_text_removes_nul_but_keeps_japanese_text_and_newlines():
    dirty = "尾山台・等々力\x00\x00エリアの眼科\nコンタクト処方\t受付中。"
    clean = sanitize_text(dirty)
    assert "\x00" not in clean
    assert clean == "尾山台・等々力エリアの眼科\nコンタクト処方\t受付中。"


def test_sanitize_text_is_noop_on_normal_snippet():
    normal = "当院では白内障手術を実施しています。ご相談ください。\n受付時間：9:00-18:00"
    assert sanitize_text(normal) == normal


def test_sanitize_text_preserves_evidence_context_needed_for_judgement():
    dirty = "当院では\x00オルソケラトロジーを実施しています。"
    clean = sanitize_text(dirty)
    assert clean == "当院ではオルソケラトロジーを実施しています。"


def test_sanitize_before_and_after_does_not_change_treatment_judgement():
    category = "オルソケラトロジー"
    dirty_text = "尾山台・等々力\x00\x00エリアの眼科 当院ではオルソケラトロジーを実施しています。"
    evidence_dirty = [{"url": "https://www.feyeclinic.com/", "text": dirty_text,
                        "page_title": "F. EYE CLINIC", "source_type": "OFFICIAL_HP"}]
    evidence_clean = [{"url": "https://www.feyeclinic.com/", "text": sanitize_text(dirty_text),
                        "page_title": "F. EYE CLINIC", "source_type": "OFFICIAL_HP"}]
    before = evaluate_treatment_evidence(category, evidence_dirty, clinic_id=5826, checked_at="2026-09-29T00:00:00Z")
    after = evaluate_treatment_evidence(category, evidence_clean, clinic_id=5826, checked_at="2026-09-29T00:00:00Z")
    assert before["status"] == after["status"] == "CONFIRMED"
    assert before["matched_alias"] == after["matched_alias"]
    assert before["reason"] == after["reason"]


def test_a_snippet_with_nul_bytes_can_be_written_and_read_back_from_csv(tmp_path):
    fields = ["clinic_id", "evidence_text"]
    rows = [{"clinic_id": 1, "evidence_text": "尾山台\x00エリアの眼科\x00\x00コンタクト処方"}]
    path = tmp_path / "phase7b_results.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["clinic_id", "clinic_name", "crestix_department",
            "treatment_category", "candidate_bucket", "research_status", "evidence_text", "evidence_url",
            "evidence_page_title", "evidence_source_type", "matched_alias", "negative_context", "rule_version",
            "researched_at", "selected_identity_url", "final_url", "fetch_status", "fetch_attempts",
            "fetch_successes", "processing_seconds", "error"])
        writer.writeheader()
        writer.writerow({
            "clinic_id": 1, "clinic_name": "test clinic", "crestix_department": "眼科",
            "treatment_category": "オルソケラトロジー", "candidate_bucket": "POSITIVE_CANDIDATE",
            "research_status": "CONFIRMED", "evidence_text": rows[0]["evidence_text"],
            "evidence_url": "https://clinic.example/", "evidence_page_title": "",
            "evidence_source_type": "OFFICIAL_HP", "matched_alias": "オルソケラトロジー",
            "negative_context": "", "rule_version": "7A-v2", "researched_at": "2026-09-29T00:00:00Z",
            "selected_identity_url": "https://clinic.example/", "final_url": "https://clinic.example/",
            "fetch_status": "OK", "fetch_attempts": 2, "fetch_successes": 2,
            "processing_seconds": 1.5, "error": "",
        })
    # Raw NUL bytes are present on disk, confirming this fixture reproduces the corruption.
    assert b"\x00" in path.read_bytes()
    recovered = _read_existing_results(path)
    assert len(recovered) == 1
    assert "\x00" not in recovered[0]["evidence_text"]
    assert recovered[0]["research_status"] == "CONFIRMED"
    assert recovered[0]["clinic_id"] == 1
