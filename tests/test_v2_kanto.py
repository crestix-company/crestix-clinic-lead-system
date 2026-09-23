from io import BytesIO
from openpyxl import Workbook
from src.enrichment.kouseikyoku_source import parse_kanto_excel
from src.normalizer.departments import normalize_departments


def test_kanto_departments_both_single_row_and_continuations_and_dental():
    book=Workbook();ws=book.active
    ws.append(["コード内容別医療機関一覧表"])
    ws.append(["[令和 8年 9月 1日現在　歯科　現存/休止]"])
    ws.append(["1","01,1234,5","見本歯科","東京都見本区1","03-0000-0001","見本 院長","見本 院長","令2.1.1","歯","診療所"])
    ws.append(["","","","","","","","新規","","現存"])
    ws.append(["2","01,1234,6","見本医院","東京都見本区2","03-0000-0002","見本 院長","見本 院長","令2.1.1","一般 12","診療所"])
    ws.append(["","","","","","","","新規","内 消 眼 糖尿病内科","現存"])
    raw=BytesIO();book.save(raw)
    frame=parse_kanto_excel(raw.getvalue())
    assert frame.iloc[0]["departments"]=="歯"
    assert frame.iloc[0]["medical_type"]=="歯科"
    assert normalize_departments(frame.iloc[1]["departments"])==["内科","消化器内科","糖尿病内科","眼科"]
