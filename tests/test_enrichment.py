from io import BytesIO
from datetime import date
import pandas as pd
import pytest
from openpyxl import Workbook
from src.enrichment.doctor_license import DoctorLicenseCache, parse_license_html
from src.enrichment.epark_checker import EparkChecker, canonical_epark_url
from src.enrichment.kouseikyoku_source import parse_kanto_excel
from src.utils.date_utils import today_japan


def license_row(**kwargs):
    return {"doctor_name":"架空 太郎","registration_year":"2000","source":"手動","checked_at":"2026-09-01","confidence":"1",**kwargs}


def test_name_collision_requires_verified_facility_scope(tmp_path):
    frame=pd.DataFrame([license_row(candidate_count="2"),license_row(registration_year="1980",candidate_count="2")])
    cache=DoctorLicenseCache(tmp_path/"license.csv")
    cache.import_records(frame)
    assert cache.lookup("架空　太郎").year is None
    cache.import_records(pd.DataFrame([license_row(clinic_id="facility-A",verified="true")]))
    assert cache.lookup("架空 太郎","facility-A").year==2000
    assert cache.lookup("架空 太郎","facility-B").year is None
    assert DoctorLicenseCache(tmp_path/"license.csv").lookup("架空 太郎","facility-A").year==2000


def test_same_registration_year_can_still_be_two_people():
    cache=DoctorLicenseCache(frame=pd.DataFrame([license_row(candidate_count="2")]))
    assert cache.lookup("架空 太郎").status=="要確認"


def test_conflicting_years_are_not_overwritten():
    frame=pd.DataFrame([license_row(),license_row(registration_year="1990")])
    assert DoctorLicenseCache(frame=frame).lookup("架空 太郎").year is None


def test_manual_license_html():
    html="<table><tr><th>氏名</th><th>職種</th><th>性別</th><th>登録年</th></tr><tr><td>架空 太郎</td><td>医師</td><td>男性</td><td>平成12年</td></tr></table>"
    result=parse_license_html(html)
    assert DoctorLicenseCache(frame=result).lookup("架空 太郎").year==2000
    with pytest.raises(ValueError):
        parse_license_html("<html>受付中</html>")


def test_epark_reservation_does_not_prove_payment(config):
    url="https://epark.jp/shopinfo/hpl123/"
    html=f'<link rel="canonical" href="{url}"><h1>EPARK</h1><a href="/reserve">ネット予約</a><img src="p.jpg">'
    result=EparkChecker(config["epark"]).check(url,html=html)
    assert result.status=="不明"
    assert result.listing=="あり"
    assert '"image_count": 1' in result.features_json


def test_no_reservation_does_not_prove_free(config):
    result=EparkChecker(config["epark"]).check("https://epark.jp/shopinfo/hpl123/",html="<html>院名</html>")
    assert result.status=="不明"


def test_wrong_saved_html_is_not_used(config):
    result=EparkChecker(config["epark"]).check("https://epark.jp/shopinfo/hpl123/",
           html='<link rel="canonical" href="https://epark.jp/shopinfo/hpl999/">')
    assert "不一致" in result.reason


@pytest.mark.parametrize("url",["http://127.0.0.1/shopinfo/hpl123/","https://epark.jp.evil.test/shopinfo/hpl123/",
                                    "file:///etc/passwd","https://user:password@epark.jp/shopinfo/hpl123/"])
def test_only_epark_host_allowed(url):
    assert canonical_epark_url(url)==""


def test_403_stops_further_requests_and_is_cached(config,tmp_path):
    class Response:
        status_code=403
        def __enter__(self):return self
        def __exit__(self,*args):pass
    class Session:
        calls=0
        def get(self,*args,**kwargs):
            self.calls+=1
            assert kwargs["allow_redirects"] is False
            return Response()
    session=Session()
    checker=EparkChecker(config["epark"],path=tmp_path/"cache.csv",session=session)
    url="https://epark.jp/shopinfo/hpl123/"
    assert "403" in checker.check(url,allow_network=True).reason
    assert checker.check(url,allow_network=True).status=="不明"
    assert checker.check("https://epark.jp/shopinfo/hpl124/",allow_network=True).status=="不明"
    assert session.calls==1
    other=EparkChecker(config["epark"],path=tmp_path/"cache.csv",session=session)
    other.check(url,allow_network=True)
    assert session.calls==1


def test_confirmed_epark_master_is_respected(config):
    frame=pd.DataFrame([{"url":"https://epark.jp/shopinfo/hpl123/","status":"課金済み","listing":"あり","source":"手動確認",
                         "verified":"true","reason":"確認済み","checked_at":today_japan().isoformat()}])
    assert EparkChecker(config["epark"],frame=frame).check(frame.iloc[0]["url"]).status=="課金済み"


def test_kanto_designation_is_not_renewal():
    book=Workbook();ws=book.active
    ws.append(["コード内容別医療機関一覧表"])
    ws.append(["[令和 8年 9月 1日現在　医科　現存/休止]"])
    ws.append(["1","01,1234,5","架空医院","〒100-0000千代田区架空町1","03-0000-0001","架空 太郎","架空 太郎","昭60. 1. 1","","診療所"])
    ws.append(["","","","","","","","交代","","休止"])
    ws.append(["","","","","","","","令8. 1. 1","",""])
    raw=BytesIO();book.save(raw)
    frame=parse_kanto_excel(raw.getvalue())
    row=frame.iloc[0]
    assert row["designation_date"]=="1985-01-01"
    assert row["designation_period_start"]=="2026-01-01"
    assert row["registration_reason"]=="交代"
    assert row["status"]=="休止"
    assert row["address"].startswith("東京都")
