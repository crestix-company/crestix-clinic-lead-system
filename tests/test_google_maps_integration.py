from io import BytesIO
import pandas as pd
from src.master.store import ClinicStore
from src.master.samples import sample_records, SampleFetcher
from src.master.google_maps import MAPS_RESULT_HEADERS, excluded_reason
from src.master.filters import Filters
from src.master.comdesk import COMDESK_HEADERS
from src.io.input_loader import load_table
from src.io.output_writer import csv_bytes
from src.enrichment.researcher import Researcher

ALL = Filters(active_only=False, hp_only=False)

def maps_frame(record, **overrides):
    row={h:"" for h in MAPS_RESULT_HEADERS}
    row.update({
        "internal_clinic_id":"", "medical_institution_number":record.get("clinic_id",""),
        "source_clinic_name":record["clinic_name"], "source_phone":record["phone"],
        "source_address":record["address"], "source_prefecture":record["prefecture"],
        "maps_match_status":"MAPS_MATCHED_WEBSITE", "maps_match_method":"phone",
        "maps_name":record["clinic_name"], "maps_phone":record["phone"], "maps_address":record["address"],
        "maps_profile_url":"https://www.google.com/maps/place/demo", "maps_website_url":"https://demo1.example/",
        "website_status":"MAPS_WEBSITE", "phone_match":"1", "name_match":"1", "address_match":"1",
        "scrape_status":"DONE", "scraped_at":"2026-09-15T00:00:00+00:00",
    })
    row.update(overrides)
    return pd.DataFrame([row],columns=MAPS_RESULT_HEADERS)

def test_exclusion_hospital_center_and_normal():
    assert excluded_reason({"clinic_name":"架空病院","facility_type":"診療所"}) == "hospital"
    assert excluded_reason({"clinic_name":"架空医療センター","facility_type":"診療所"}) == "center"
    assert excluded_reason({"clinic_name":"架空内科クリニック","facility_type":"診療所"}) == ""

def test_maps_import_internal_or_medical_id_is_idempotent_and_preserves_uuid(tmp_path):
    store=ClinicStore(tmp_path/"m.db"); r=sample_records()[0]
    store.import_master([r])
    cid=store.query(ALL)[0]["id"]
    # UUID付きComdeskを後から照合
    store.import_comdesk(load_table(csv_bytes(["UUID","名前","Tel1"],[["KEEP-UUID",r["clinic_name"],r["phone"]]]),"c.csv"))
    frame=maps_frame(r,internal_clinic_id=str(cid))
    a=store.import_google_maps(frame); b=store.import_google_maps(frame)
    assert a["MATCHED"]==1 and b["already_imported"] is True
    got=store.get(cid)
    assert got["uuid"]=="KEEP-UUID"
    assert got["maps_presence_status"]=="MAPS_MATCHED_WEBSITE"
    assert got["maps_website_url"]=="https://demo1.example/"
    with store.connect() as c:
        assert c.execute("select count(*) from google_maps_results").fetchone()[0] == 1

def test_maps_no_website_and_not_found_are_distinct(tmp_path):
    store=ClinicStore(tmp_path/"m.db"); records=sample_records()[:2]; store.import_master(records)
    rows=[]
    for rec,status in zip(records,["MAPS_MATCHED_NO_WEBSITE","MAPS_NOT_FOUND"]):
        df=maps_frame(rec,maps_match_status=status,maps_website_url="",website_status="NO_WEBSITE" if "MATCHED" in status else "")
        rows.append(df.iloc[0].to_dict())
    result=store.import_google_maps(pd.DataFrame(rows,columns=MAPS_RESULT_HEADERS))
    assert result["NO_WEBSITE"]==1 and result["NOT_FOUND"]==1
    statuses={r["clinic_name"]:r["maps_presence_status"] for r in store.query(ALL)}
    assert statuses[records[0]["clinic_name"]]=="MAPS_MATCHED_NO_WEBSITE"
    assert statuses[records[1]["clinic_name"]]=="MAPS_NOT_FOUND"

def test_comdesk_existing_url_preserved_blank_url_supplemented_and_28_columns(tmp_path):
    store=ClinicStore(tmp_path/"m.db"); r=sample_records()[0]
    # first record: nonblank URL must survive Maps
    old=[""]*28; old[0]="U1"; old[2]=r["clinic_name"]; old[9]=r["phone"]; old[14]="https://old.example/"
    store.import_comdesk(load_table(csv_bytes(COMDESK_HEADERS,[old]),"c.csv")); store.import_master([r])
    cid=store.query(ALL)[0]["id"]
    store.import_google_maps(maps_frame(r,internal_clinic_id=str(cid),maps_website_url="https://maps.example/"))
    exported=load_table(store.export(ALL)["final_comdesk_import.csv"],"x.csv")
    assert exported.headers==COMDESK_HEADERS and len(exported.headers)==28
    assert exported.data.iloc[0,14]=="https://old.example/"
    assert "internal_clinic_id" not in exported.headers and "maps_profile_url" not in exported.headers

    # second independent DB: blank old URL gets confirmed Maps website only
    store2=ClinicStore(tmp_path/"m2.db"); old2=old.copy(); old2[14]=""
    store2.import_comdesk(load_table(csv_bytes(COMDESK_HEADERS,[old2]),"c2.csv")); store2.import_master([r])
    cid2=store2.query(ALL)[0]["id"]
    store2.import_google_maps(maps_frame(r,internal_clinic_id=str(cid2),maps_website_url="https://maps.example/"))
    out2=load_table(store2.export(ALL)["final_comdesk_import.csv"],"x2.csv")
    assert out2.data.iloc[0,14]=="https://maps.example/"

def test_new_master_uuid_blank_and_maps_website_in_url(tmp_path):
    store=ClinicStore(tmp_path/"m.db"); r=sample_records()[0]; store.import_master([r]); cid=store.query(ALL)[0]["id"]
    store.import_google_maps(maps_frame(r,internal_clinic_id=str(cid),maps_website_url="https://maps.example/", **{"休診日":"日","診療日":"月・火","午前始":"9:00","午前終":"12:00","午後始":"15:00","午後終":"18:00"}))
    out=load_table(store.export(ALL)["final_comdesk_import.csv"],"x.csv")
    assert out.data.iloc[0,0]=="" and out.data.iloc[0,14]=="https://maps.example/"
    assert out.data.iloc[0,20:26].tolist()==["日","月・火","09:00","12:00","15:00","18:00"]

def test_excluded_center_not_in_standard_sales_export(tmp_path):
    store=ClinicStore(tmp_path/"m.db"); r={**sample_records()[0],"clinic_name":"架空健診センター"}; store.import_master([r])
    out=load_table(store.export(ALL)["final_comdesk_import.csv"],"x.csv")
    assert len(out.data)==0 and out.headers==COMDESK_HEADERS

def test_maps_filter_only_confirmed(tmp_path):
    store=ClinicStore(tmp_path/"m.db"); recs=sample_records()[:2]; store.import_master(recs)
    cid=next(x for x in store.query(ALL) if x["clinic_name"]==recs[0]["clinic_name"])["id"]
    store.import_google_maps(maps_frame(recs[0],internal_clinic_id=str(cid)))
    found=store.query(Filters(active_only=False,hp_only=False,maps_confirmed_only=True))
    assert [x["clinic_name"] for x in found]==[recs[0]["clinic_name"]]

def test_researcher_uses_maps_website_without_search_api():
    r={**sample_records()[0],"maps_presence_status":"MAPS_MATCHED_WEBSITE","maps_website_url":"https://demo1.example/"}
    class NeverSearch:
        calls=0
        def search(self,*args,**kwargs):
            self.calls += 1
            raise AssertionError("Maps websiteがあるのに検索APIを呼んだ")
    search=NeverSearch(); result,_=Researcher(search,SampleFetcher(),max_pages=1).hp(r,force=True)
    assert result["hp_status"]=="VERIFIED" and result["hp_url"]=="https://demo1.example/" and search.calls==0

def test_confirmed_maps_website_is_not_downgraded_by_later_ambiguous_or_blank(tmp_path):
    store=ClinicStore(tmp_path/"m.db"); r=sample_records()[0]; store.import_master([r]); cid=store.query(ALL)[0]["id"]
    first=maps_frame(
        r,
        internal_clinic_id=str(cid),
        maps_match_status="MAPS_MATCHED_WEBSITE",
        maps_website_url="http://sakuraganka.jp/",
        website_status="MAPS_WEBSITE_CONFIRMED",
        maps_profile_url="https://www.google.com/maps/place/confirmed",
        scraped_at="2026-09-15T00:00:00+00:00",
    )
    store.import_google_maps(first)

    second=maps_frame(
        r,
        internal_clinic_id=str(cid),
        maps_match_status="MAPS_MATCHED_NO_WEBSITE",
        maps_website_url="",
        website_status="MAPS_WEBSITE_AMBIGUOUS",
        maps_profile_url="https://www.google.com/maps/place/later",
        scraped_at="2026-09-16T00:00:00+00:00",
    )
    result=store.import_google_maps(second)
    got=store.get(cid)
    assert result["PRESERVED_WEBSITE"] == 1
    assert got["maps_presence_status"] == "MAPS_MATCHED_WEBSITE"
    assert got["maps_website_url"] == "http://sakuraganka.jp/"
    assert got["maps_profile_url"] == "https://www.google.com/maps/place/confirmed"

    # 後続バッチそのものは監査用に保存する。
    with store.connect() as c:
        rows=c.execute("select maps_match_status,maps_website_url from google_maps_results order by id").fetchall()
    assert len(rows)==2
    assert rows[1][0] == "MAPS_MATCHED_NO_WEBSITE"
    assert rows[1][1] == ""


def test_later_confirmed_maps_website_can_update_existing_confirmed_url(tmp_path):
    store=ClinicStore(tmp_path/"m.db"); r=sample_records()[0]; store.import_master([r]); cid=store.query(ALL)[0]["id"]
    store.import_google_maps(maps_frame(
        r, internal_clinic_id=str(cid), maps_website_url="https://old.example/",
        website_status="MAPS_WEBSITE_CONFIRMED", scraped_at="2026-09-15T00:00:00+00:00"
    ))
    result=store.import_google_maps(maps_frame(
        r, internal_clinic_id=str(cid), maps_website_url="https://new.example/",
        website_status="MAPS_WEBSITE_CONFIRMED", scraped_at="2026-09-16T00:00:00+00:00"
    ))
    got=store.get(cid)
    assert result["PRESERVED_WEBSITE"] == 0
    assert got["maps_presence_status"] == "MAPS_MATCHED_WEBSITE"
    assert got["maps_website_url"] == "https://new.example/"

