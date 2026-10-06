import sqlite3

from src.master.filters import Filters
from src.master.hp_effective_rank import HP_BATCH_ENV_VAR
from src.master.hp_site_type import (
    SITE_OFFICIAL, SITE_OTHER, SITE_PORTAL, classify_site, portal_name_for_url,
)
from src.master.samples import sample_records
from src.master.store import ClinicStore


def test_portal_domains_and_subdomains_are_offline_classified():
    assert portal_name_for_url("https://doctorsfile.jp/h/1") == "Doctors File"
    assert portal_name_for_url("https://www.hospita.jp/hospital/1") == "Hospita"
    assert portal_name_for_url("https://clinic.byoinnavi.jp/x") == "病院なび"
    assert portal_name_for_url("https://www.tokyo-doctors.com/clinicList/1") == "東京ドクターズ"
    assert portal_name_for_url("https://www.iryou.teikyouseido.mhlw.go.jp/znk-web/") == "医療情報ネット"
    assert portal_name_for_url("https://not-byoinnavi.jp/") == ""


def test_non_portal_is_not_inferred_as_official():
    assert classify_site("UNRESEARCHED", "", "https://clinic.example/") == (SITE_OTHER, "")
    assert classify_site("VERIFIED", "https://clinic.example/") == (SITE_OFFICIAL, "")
    assert classify_site("VERIFIED", "https://clinic.example/", final_url="https://doctorsfile.jp/h/1") == (SITE_PORTAL, "Doctors File")


def test_site_type_filter_is_independent_from_hp_abc(tmp_path, monkeypatch):
    store = ClinicStore(tmp_path / "clinics.sqlite3")
    store.import_master(sample_records())
    rows = store.query(Filters(active_only=False, hp_only=False), limit=10)
    ids = [row["id"] for row in rows]
    with store.connect() as conn:
        conn.execute("UPDATE clinics SET hp_status='VERIFIED',hp_url='https://official.example/' WHERE id=?", (ids[0],))
        conn.execute("UPDATE clinics SET hp_status='UNRESEARCHED',hp_url='',maps_website_url='https://hospita.jp/hospital/1' WHERE id=?", (ids[1],))
        conn.execute("UPDATE clinics SET hp_status='UNRESEARCHED',hp_url='',maps_website_url='https://other.example/' WHERE id=?", (ids[2],))
    batch = tmp_path / "batch.sqlite3"
    with sqlite3.connect(batch) as conn:
        conn.execute("CREATE TABLE hp_research_batch_results(clinic_id INTEGER PRIMARY KEY,hp_url TEXT,final_url TEXT,fetch_status TEXT,hp_abc_candidate TEXT,treatment_categories TEXT)")
        conn.executemany("INSERT INTO hp_research_batch_results VALUES(?,?,?,?,?,?)", [
            (ids[0], "https://official.example/", "https://official.example/", "OK", "A", '["内視鏡"]'),
            (ids[1], "https://hospita.jp/hospital/1", "https://hospita.jp/hospital/1", "OK", "B", '[]'),
            (ids[2], "https://other.example/", "https://other.example/", "OK", "A", '[]'),
        ])
    monkeypatch.setenv(HP_BATCH_ENV_VAR, str(batch))

    base = dict(active_only=False, hp_only=False, effective_ranks=["A", "B"])
    assert store.count(Filters(**base, site_types=[SITE_OFFICIAL])) == 1
    assert store.count(Filters(**base, site_types=[SITE_PORTAL])) == 1
    assert store.count(Filters(**base, site_types=[SITE_OTHER])) == 1
    portal = store.query(Filters(**base, site_types=[SITE_PORTAL]))[0]
    assert portal["site_type"] == SITE_PORTAL and portal["portal_name"] == "Hospita"
    official = store.query(Filters(**base, site_types=[SITE_OFFICIAL]))[0]
    assert official["website_treatment_categories"] == ["内視鏡"]
