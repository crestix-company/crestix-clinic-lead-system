"""架空の施設・医師だけを使う再現可能なサンプル。"""
from pathlib import Path
import sys
from datetime import date
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
from src.io.input_loader import load_table, infer_columns
from src.io.output_writer import csv_bytes, xlsx_bytes, build_outputs, write_outputs
from src.enrichment.doctor_license import DoctorLicenseCache, LICENSE_COLUMNS
from src.enrichment.epark_checker import EparkChecker, EPARK_COLUMNS
from src.utils.config import settings, ROOT
from src.pipeline import run_pipeline


def generate():
    target = ROOT / "samples"
    target.mkdir(exist_ok=True)
    names = ["青空内視鏡クリニック", "若葉眼科医院", "さくら二代目診療所", "ひなた皮膚科",
             "古町内科", "欠年糖尿病クリニック", "休止眼科医院", "東京サンプル病院",
             "県外サンプル医院", "未照合クリニック", "同名確認医院", "電話未登録医院",
             "院長交代クリニック", "住所ゆらぎクリニック", "日付未取得医院", "姓だけ同じ医院"]
    doctors = ["見本 青一", "見本 葉二", "架空 桜三", "見本 陽四", "見本 古五", "見本 欠六",
               "見本 休七", "見本 病八", "見本 県九", "見本 未十", "架空 同名", "見本 電話",
               "見本 新郎", "見本 住所", "見本 日付", "架空 太郎"]
    rows, masters, licenses, extra = [], [], [], []
    for i, (name, doctor) in enumerate(zip(names, doctors)):
        n = i+1
        phone = f"03-0000-{n:04d}"  # 架空番号
        address = f"東京都千代田区架空町{n}-1-1"
        rows.append([phone, name, address, f"https://clinic{n}.example.invalid", "内視鏡" if i%2==0 else "白内障", f"テスト{i+1:03d}", "NA" if i==0 else ""])
        masters.append(dict(clinic_id=f"demo-{n:03d}", clinic_name=name, phone=phone, address=address,
                            prefecture="東京都", medical_type="医科", facility_type="診療所",
                            owner_name=doctor, manager_name=doctor, designation_date="2020-04-01",
                            registration_reason="新規", status="現存", as_of="2026-09-01",
                            source_url="架空サンプル（実在施設ではありません）"))
        licenses.append(dict(doctor_name=doctor, registration_year="2000", source="架空サンプル",
                             checked_at="2026-09-01", confidence="1.0", note="テスト用", profession="医師",
                             candidate_count="1"))
    masters[1].update(owner_name="見本 葉父", registration_reason="継承")
    masters[2].update(owner_name="架空 桜父", registration_reason="継承", designation_date="1980-04-01")
    masters[3].update(owner_name="別人 法人", designation_date="2000-04-01")
    masters[4].update(designation_date="1980-04-01"); licenses[4]["registration_year"] = "1975"
    licenses[5]["registration_year"] = ""
    masters[6]["status"] = "休止"
    masters[7]["facility_type"] = "病院"
    masters[8].update(prefecture="神奈川県", address="神奈川県横浜市架空町1-1")
    rows[8][2] = "神奈川県横浜市架空町1-1"
    masters[10].update(owner_name="架空 同名")
    licenses[10]["candidate_count"] = "2"
    licenses.append({**licenses[10], "registration_year": "1970"})
    rows[11][0] = ""
    masters[12].update(owner_name="別姓 前任", registration_reason="管理者変更")
    rows[13][1] = "医療法人社団 試験会 住所ゆらぎ医院"
    rows[13][2] = "東京都千代田区架空町十四丁目1番1号"
    masters[14]["designation_date"] = ""
    masters[15].update(owner_name="架空 次郎", designation_date="1980-04-01")
    masters.pop(9)
    extra = [
        {"clinic_id": "demo-002", "epark_url": "https://epark.jp/shopinfo/hpl900000002/", "profile_text": "父から承継し二代目として院長就任", "profile_source": "架空サンプル", "other_media": ""},
        {"clinic_id": "demo-004", "epark_url": "https://epark.jp/shopinfo/hpl900000004/", "profile_text": "", "profile_source": "", "other_media": ""},
    ]
    paid = [{"url": f"https://epark.jp/shopinfo/hpl90000000{i}/", "listing": "あり", "status": "課金済み",
             "reason": "デモ用の架空確認情報", "source": "架空サンプル", "checked_at": "2026-09-01", "verified": "true"}
            for i in [2, 4]]
    header = ["電話番号", "医院名", "住所", "URL", "治療名", "管理ID", "その他列"]
    (target / "sample_comdesk.csv").write_bytes(csv_bytes(header, rows))
    (target / "sample_comdesk_cp932.csv").write_bytes(csv_bytes(header, rows, "cp932"))
    (target / "sample_comdesk.xlsx").write_bytes(xlsx_bytes({"営業リスト": pd.DataFrame(rows, columns=header), "説明": pd.DataFrame([["架空データ", "営業リストシートを選択してください"]], columns=["項目","内容"])}))
    pd.DataFrame(masters).to_csv(target/"sample_kouseikyoku.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(licenses).reindex(columns=LICENSE_COLUMNS, fill_value="").to_csv(target/"sample_doctor_license.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(extra).to_csv(target/"sample_enrichment.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(paid).reindex(columns=EPARK_COLUMNS, fill_value="").to_csv(target/"sample_epark_confirmed.csv", index=False, encoding="utf-8-sig")
    c = settings()
    table = load_table((target/"sample_comdesk.csv").read_bytes(), "sample.csv")
    cache = DoctorLicenseCache(frame=pd.DataFrame(licenses))
    epark = EparkChecker(c["epark"], frame=pd.DataFrame(paid), as_of=date(2026,9,10))
    result = run_pipeline(table, infer_columns(table), pd.DataFrame(masters), c, cache, epark,
                          as_of=date(2026,9,10), extra=pd.DataFrame(extra))
    write_outputs(build_outputs(table, result), target/"outputs")
    print(result.metrics)
    print(result.judgments[["入力行番号","営業対象判定","営業優先度"]].to_string(index=False))


if __name__ == "__main__":
    generate()
