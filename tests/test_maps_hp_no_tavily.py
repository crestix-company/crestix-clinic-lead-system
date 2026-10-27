from src.master.store import ClinicStore
from src.master.samples import sample_records, SampleFetcher
from src.master.google_maps import MAPS_RESULT_HEADERS
from src.master.filters import Filters
from src.master.jobs import create_job, run_job, job_status
from src.enrichment.search_provider import TavilySearchProvider

ALL = Filters(active_only=False, hp_only=False)


class NoNetworkSession:
    posts = 0

    def post(self, *args, **kwargs):
        self.posts += 1
        raise AssertionError("Maps HPがあるのにTavilyを呼んだ")


def test_job_with_maps_website_never_calls_tavily(tmp_path):
    store = ClinicStore(tmp_path / "m.db")
    rec = sample_records()[0]
    store.import_master([rec])
    cid = store.query(ALL)[0]["id"]
    row = {h: "" for h in MAPS_RESULT_HEADERS}
    row.update({
        "internal_clinic_id": str(cid), "medical_institution_number": rec.get("clinic_id", ""),
        "source_clinic_name": rec["clinic_name"], "source_phone": rec["phone"],
        "source_address": rec["address"], "source_prefecture": rec["prefecture"],
        "maps_match_status": "MAPS_MATCHED_WEBSITE", "maps_match_method": "phone",
        "maps_website_url": "https://demo1.example/",
    })
    import pandas as pd
    store.import_google_maps(pd.DataFrame([row]))
    session = NoNetworkSession()
    provider = TavilySearchProvider("dummy-key", session)
    jid = create_job(store, ALL, limit=1, max_pages=1)
    run_job(store, jid, provider, SampleFetcher())
    job = job_status(store, jid)
    assert session.posts == 0
    assert job["search_count"] == 0
    assert job["counts"].get("DONE") == 1


def test_maps_hp_job_runs_without_tavily_key_and_missing_url_error_does_not_stop_next_clinic(
    tmp_path, monkeypatch,
):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    store = ClinicStore(tmp_path / "m.db")
    records = sample_records()[:2]
    store.import_master(records)
    clinics = store.query(ALL)
    mapped = next(row for row in clinics if row["clinic_name"] == records[1]["clinic_name"])
    maps_record = records[1]

    row = {header: "" for header in MAPS_RESULT_HEADERS}
    row.update({
        "internal_clinic_id": str(mapped["id"]),
        "medical_institution_number": maps_record.get("clinic_id", ""),
        "source_clinic_name": maps_record["clinic_name"],
        "source_phone": maps_record["phone"],
        "source_address": maps_record["address"],
        "source_prefecture": maps_record["prefecture"],
        "maps_match_status": "MAPS_MATCHED_WEBSITE",
        "maps_match_method": "phone",
        "maps_website_url": "https://demo2.example/",
    })
    import pandas as pd
    store.import_google_maps(pd.DataFrame([row]))

    session = NoNetworkSession()
    provider = TavilySearchProvider(session=session)
    jid = create_job(store, ALL, limit=2, max_pages=1)
    run_job(store, jid, provider, SampleFetcher())

    job = job_status(store, jid)
    assert session.posts == 0
    assert job["status"] == "COMPLETED"
    assert job["counts"].get("DONE") == 2
    assert job["results"] == {"ERROR": 1, "SUCCESS": 1}
