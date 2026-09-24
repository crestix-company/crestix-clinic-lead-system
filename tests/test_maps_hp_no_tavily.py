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
