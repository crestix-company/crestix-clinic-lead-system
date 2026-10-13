from scripts.prepare_email_canary_cohort import artifact_exclusions, make_cohort


def test_canary_cohort_is_seeded_uuid_unique_and_prefers_real_uuids():
    clinics = [
        {"clinic_id": i, "uuid": f"00000000-0000-0000-0000-{i:012d}", "clinic_name": f"Clinic {i}"}
        for i in range(1, 110)
    ]
    clinics.extend([
        {"clinic_id": 200, "uuid": "", "clinic_name": "Legacy clinic"},
        {"clinic_id": 201, "uuid": "", "clinic_name": "Legacy clinic 2"},
    ])
    first, stats = make_cohort(clinics, excluded_ids={1, 2}, limit=100, seed=20261009)
    second, _ = make_cohort(clinics, excluded_ids={1, 2}, limit=100, seed=20261009)
    assert first == second
    assert len(first) == 100
    assert stats["real_uuid_count"] == 100
    assert stats["legacy_id_count"] == 0
    assert stats["duplicate_uuid_count_in_cohort"] == 0
    assert {row["clinic_id"] for row in first}.isdisjoint({1, 2})


def test_artifact_exclusions_collect_clinic_ids_from_email_canaries(tmp_path):
    root = tmp_path / "artifacts" / "clinic_email_fresh_canary_v1"
    root.mkdir(parents=True)
    (root / "clinics.csv").write_text("id,clinic_name\n5,A\n6,B\n", encoding="utf-8")
    excluded, files = artifact_exclusions(tmp_path / "artifacts")
    assert excluded == {5, 6}
    assert len(files) == 1


def test_maps_website_url_is_reused_only_when_master_hp_status_is_verified():
    clinics = [
        {"clinic_id": 1, "uuid": "00000000-0000-0000-0000-000000000001",
         "clinic_name": "Verified", "hp_url": "", "hp_status": "VERIFIED",
         "maps_website_url": "https://clinic.example/"},
        {"clinic_id": 2, "uuid": "00000000-0000-0000-0000-000000000002",
         "clinic_name": "Unverified", "hp_url": "", "hp_status": "UNRESEARCHED",
         "maps_website_url": "https://unknown.example/"},
    ]
    cohort, _ = make_cohort(clinics, excluded_ids=set(), limit=2, seed=1)
    by_id = {row["clinic_id"]: row for row in cohort}
    assert by_id[1]["hp_url"] == "https://clinic.example/"
    assert by_id[1]["hp_url_source"] == "VERIFIED_MAPS_WEBSITE_URL"
    assert by_id[2]["hp_url"] == ""
    assert by_id[2]["hp_url_source"] == ""
