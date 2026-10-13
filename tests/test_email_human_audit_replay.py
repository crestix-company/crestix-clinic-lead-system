import csv
import json

from scripts import email_human_audit_replay as replay_module


def test_offline_replay_emits_identity_labels_and_explicit_uuid_candidate_keys(tmp_path, monkeypatch):
    audit_root = tmp_path / "audit"
    source = audit_root / "labeled.csv"
    source.parent.mkdir()
    with source.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "clinic_id", "email", "source_type", "source_url", "verification_status",
            "role", "ownership", "clinic_name_match", "prefecture_match", "city_match",
            "human_verdict", "human_comment",
        ])
        writer.writeheader()
        writer.writerow({
            "clinic_id": "42", "email": "info@clinic.jp", "source_type": "OFFICIAL_HP",
            "source_url": "https://clinic.jp/contact", "verification_status": "VERIFIED_EMAIL",
            "role": "GENERAL", "ownership": "CLINIC_OWNED", "clinic_name_match": "true",
            "prefecture_match": "true", "city_match": "true", "human_verdict": "CORRECT_PASS",
            "human_comment": "not an input to classifier",
        })
    monkeypatch.setattr(replay_module, "DEFAULT_FILES", ("labeled.csv",))
    output = tmp_path / "result"
    summary = replay_module.replay(audit_root, output, filenames=("labeled.csv",))
    row = next(csv.DictReader((output / "all_audit_rows.csv").open(encoding="utf-8-sig")))
    assert row["clinic_id"] == "42"
    assert row["clinic_uuid"] == "legacy-id:42"
    assert row["clinic_uuid_kind"] == "MISSING"
    assert row["candidate_id_kind"] == "OFFLINE_SYNTHETIC"
    assert row["auto_decision"] == "AUTO_TRUE"
    assert summary["confusion_matrix"]["AUTO_TRUE"]["TRUE"] == 1
    assert json.loads((output / "summary.json").read_text())["production_db_access"] == 0


def test_latest_human_label_conflict_is_quarantined_not_arbitrarily_resolved():
    base = {"clinic_id": "7", "normalized_email": "info@clinic.jp", "checked_at": "2026-10-09",
            "clinic_uuid": "legacy-id:7", "candidate_id": "offline:7:abc", "audit_source_file": "a.csv",
            "human_identity_label": "TRUE"}
    other = {**base, "audit_source_file": "b.csv", "human_identity_label": "FALSE"}
    selected, conflicts = replay_module.latest_unique_candidates([base, other])
    assert selected == []
    assert len(conflicts) == 1
