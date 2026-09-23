from pathlib import Path
from io import BytesIO
from datetime import date
import hashlib
import json
import re
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
from src.utils.config import ROOT, settings
from src.utils.date_utils import today_japan
from src.utils.cache import merge_cache
from src.io.input_loader import load_table, sheet_names, infer_columns
from src.io.output_writer import build_outputs, zip_outputs
from src.enrichment.kouseikyoku_source import load_master, download_tokyo_master
from src.enrichment.doctor_license import DoctorLicenseCache, parse_license_html, LICENSE_COLUMNS
from src.enrichment.epark_checker import EparkChecker, canonical_epark_url, EPARK_COLUMNS
from src.scoring.age_estimator import AgeEstimator
from src.pipeline import run_pipeline

st.set_page_config(page_title="クリニック営業リスト選別ツール", page_icon="📋", layout="wide")


def uploaded_table(upload, key):
    sheets = sheet_names(upload.getvalue(), upload.name)
    sheet = st.selectbox("読み込むシート", sheets, key=key+"_sheet") if len(sheets) > 1 else (sheets[0] if sheets else None)
    return load_table(upload.getvalue(), upload.name, sheet=sheet)


def standard_frame(upload, key):
    if upload is None:
        return None
    table = uploaded_table(upload, key)
    if len(set(table.headers)) != len(table.headers):
        raise ValueError("マスタの見出しが重複しています。")
    return table.frame().fillna("").astype(str)


@st.cache_data(show_spinner=False)
def cached_master(path, modified):
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")


def main():
    cfg = settings()
    st.title(cfg["app"]["title"])
    st.caption("第一事業部｜元の列名・列順を保ったまま、営業対象を抽出します。")
    cols = st.columns([3, 1])
    with cols[0]:
        uploaded = st.file_uploader("コムデスクに取り込む営業リスト", type=["csv", "xlsx", "xls"], key="leads")
    with cols[1]:
        st.write("")
        if st.button("サンプルで試す", use_container_width=True):
            st.session_state["demo"] = True
        if st.session_state.get("demo") and st.button("サンプルを終了", use_container_width=True):
            st.session_state["demo"] = False
    demo = st.session_state.get("demo", False) and uploaded is None
    if demo:
        st.info("架空のサンプル16件を使用中です。実データは営業リストのアップロードから開始してください。")
    with st.sidebar:
        st.subheader("判定設定")
        prefecture = st.selectbox("対象都道府県", list(cfg["prefectures"]))
        st.caption("年齢は仮定モデルによる推定です。指定年月日は実際の開院日と異なることがあります。")
        as_of = st.date_input("判定基準日", value=date(2026,9,10) if demo else today_japan(), disabled=demo)
        if demo:
            as_of = date(2026,9,10)
        st.link_button("東京都の公式一覧", cfg["prefectures"][prefecture]["current_list_url"])
        if st.button("東京都マスタを公式データで更新", disabled=demo):
            with st.spinner("公式Excelを取得・変換中…"):
                try:
                    master, raw, name = download_tokyo_master()
                    path = ROOT / cfg["prefectures"][prefecture]["master_path"]
                    tmp = path.with_suffix(".tmp")
                    master.to_csv(tmp, index=False, encoding="utf-8-sig")
                    tmp.replace(path)
                    (ROOT/"data/raw"/Path(name).name).write_bytes(raw)
                    st.success(f"マスタを更新しました：{len(master):,}施設")
                except Exception:
                    st.error("取得できませんでした。公式ページからExcelを保存し、下のマスタ取込をご利用ください。")
        with st.expander("マスタを追加・差し替え"):
            master_upload = st.file_uploader("厚生局マスタ", type=["csv", "xlsx", "xls"], key="master")
            master_format = st.selectbox("厚生局マスタの形式", ["標準CSV/Excel", "関東信越厚生局の帳票Excel"])
            if master_upload:
                names = sheet_names(master_upload.getvalue(), master_upload.name)
                master_sheet = st.selectbox("厚生局のシート", names) if names else None
            else:
                master_sheet = None
            license_upload = st.file_uploader("医籍登録年CSV / Excel / 保存HTML", type=["csv", "xlsx", "xls", "html", "htm"], key="license")
            st.link_button("医師等資格確認検索を開く", cfg["doctor_license"]["search_url"])
            extra_upload = st.file_uploader("EPARK URL・プロフィール等の補足マスタ", type=["csv", "xlsx", "xls"], key="extra")
            epark_upload = st.file_uploader("EPARK課金確認マスタ", type=["csv", "xlsx", "xls"], key="epark")
            html_uploads = st.file_uploader("EPARK保存HTML（hpl番号.html）", type=["html", "htm"], accept_multiple_files=True)
            persist = st.checkbox("取り込んだ医籍・EPARK確認情報を次回も使う", value=True)
        with st.expander("読み込み・出力設定"):
            encoding = st.selectbox("CSVの入力文字コード", ["auto", "utf-8-sig", "utf-8", "cp932", "shift_jis"])
            output_encoding = st.selectbox("CSVの出力文字コード", ["入力に合わせる", "utf-8-sig", "cp932", "shift_jis"])
            include_review = st.checkbox("要確認の行も最終CSVに含める", value=cfg["lead"]["include_review_in_final"])
            allow_network = st.checkbox("EPARK HTMLの自動取得を試す", value=False)
            st.caption("403・CAPTCHA等で停止します。予約や写真だけでは課金済みと判定しません。")
        st.caption("テンプレート・操作説明は同梱のREADMEにあります。")
    st.write("判定に使う条件")
    checks = st.columns(3)
    options = dict(cfg["lead"])
    with checks[0]:
        options["require_recent"] = st.checkbox("通常院長型に10年以内を求める", value=cfg["lead"]["require_recent"])
        options["require_young"] = st.checkbox("推定59歳以下を求める", value=cfg["lead"]["require_young"])
    with checks[1]:
        options["require_owner_manager"] = st.checkbox("通常院長型に開設者＝管理者を求める", value=cfg["lead"]["require_owner_manager"])
        options["include_succession"] = st.checkbox("継承・次世代院長を含める", value=cfg["lead"]["include_succession"])
    with checks[2]:
        options["epark_enabled"] = st.checkbox("EPARK課金情報を判定に使う", value=cfg["lead"]["epark_enabled"])
    options["include_review_in_final"] = include_review
    if uploaded is None and not demo:
        st.info("CSVまたはExcelをアップロードすると、列の確認と判定を開始できます。")
        st.stop()
    raw, filename = ((ROOT/"samples/sample_comdesk.csv").read_bytes(), "sample_comdesk.csv") if demo else (uploaded.getvalue(), uploaded.name)
    names = sheet_names(raw, filename)
    sheet = st.selectbox("営業リストのシート", names) if len(names) > 1 else (names[0] if names else None)
    table = load_table(raw, filename, encoding=encoding, sheet=sheet,
                       max_rows=cfg["app"]["max_rows"], max_bytes=cfg["app"]["max_upload_mb"]*1024**2)
    for warning in table.warnings:
        st.warning(warning)
    mapping = infer_columns(table)
    with st.expander("読み込んだ列を確認", expanded=any(mapping[k] is None for k in ["phone", "clinic_name", "address"])):
        labels = {"phone": "電話番号", "clinic_name": "医院名", "address": "住所", "url": "公式URL", "epark_url": "EPARK URL"}
        for field, label in labels.items():
            choices = [None]+list(range(len(table.headers)))
            mapping[field] = st.selectbox(label+"の列", choices, index=choices.index(mapping[field]),
                                          format_func=lambda x: "未設定" if x is None else f"{x+1}列目：{table.headers[x] or '（空の列名）'}",
                                          key="map_"+field+"_"+hashlib.sha256(raw).hexdigest()[:8])
        preview = table.frame().head(5).copy()
        preview.columns = [f"{i+1}：{h}" for i,h in enumerate(table.headers)]
        st.dataframe(preview, hide_index=True, use_container_width=True)
    if mapping["phone"] is None and mapping["clinic_name"] is None:
        st.error("電話番号または医院名の列を選択してください。")
        st.stop()
    selected_master = ROOT/"samples/sample_kouseikyoku.csv" if demo else ROOT/cfg["prefectures"][prefecture]["master_path"]
    if master_upload and not demo:
        master = load_master(master_upload.getvalue(), master_upload.name,
                             "kanto_excel" if "帳票" in master_format else "standard", prefecture, master_sheet)
    elif selected_master.exists():
        master = cached_master(str(selected_master), selected_master.stat().st_mtime_ns)
    else:
        master = pd.DataFrame(columns=["clinic_name", "phone"])
        st.warning("厚生局マスタがありません。公式更新またはマスタ取込を行うまで全件が要確認になります。")
    st.caption(f"入力 {len(table.data):,}件　｜　厚生局マスタ {len(master):,}施設　｜　基準日 " +
               "、".join(sorted(set(master.get("as_of", pd.Series(dtype=str))))[:160]))
    if demo:
        license_frame = pd.read_csv(ROOT/"samples/sample_doctor_license.csv", dtype=str, keep_default_na=False)
        epark_frame = pd.read_csv(ROOT/"samples/sample_epark_confirmed.csv", dtype=str, keep_default_na=False)
        extra = pd.read_csv(ROOT/"samples/sample_enrichment.csv", dtype=str, keep_default_na=False)
    else:
        if license_upload and Path(license_upload.name).suffix.lower() in {".html", ".htm"}:
            license_frame = parse_license_html(license_upload.getvalue())
        else:
            license_frame = standard_frame(license_upload, "license")
        epark_frame = standard_frame(epark_upload, "epark")
        extra = standard_frame(extra_upload, "extra")
    html_files = {}
    for upload in html_uploads or []:
        html = upload.getvalue()
        soup = BeautifulSoup(html, "html.parser")
        canonical = soup.select_one('link[rel="canonical"]')
        url = canonical_epark_url(canonical.get("href", "")) if canonical else ""
        match = re.search(r"hpl\d+", upload.name)
        url = url or (f"https://epark.jp/shopinfo/{match[0]}/" if match else "")
        if not url:
            st.warning("医院を特定できない保存HTMLがあります。ファイル名をhpl番号.htmlにしてください。")
        elif url in html_files:
            raise ValueError("同じEPARK URLのHTMLが複数あります。1つだけ選択してください。")
        else:
            html_files[url] = html
    signature = hashlib.sha256(raw + str((mapping, options, as_of, prefecture, allow_network, output_encoding, table.sheet_name)).encode() +
                               master.to_csv(index=False).encode() + json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode() +
                               (ROOT/"config/age_model.yml").read_bytes())
    for frame in [license_frame, epark_frame, extra]:
        if frame is not None:
            signature.update(frame.to_csv(index=False).encode())
    for url, html in sorted(html_files.items()):
        signature.update(url.encode()+html)
    def current_fingerprint():
        current = signature.copy()
        if not demo:
            for rel in [cfg["doctor_license"]["cache_path"], cfg["epark"]["cache_path"]]:
                path = ROOT/rel
                current.update(path.read_bytes() if path.exists() else b"")
        return current.hexdigest()
    fingerprint = current_fingerprint()
    if st.button("判定開始", type="primary", use_container_width=True, disabled=table.data.empty):
        licenses = DoctorLicenseCache(None if demo else ROOT/cfg["doctor_license"]["cache_path"],
                                       minimum_confidence=cfg["doctor_license"]["minimum_confidence"])
        if license_frame is not None:
            licenses.import_records(license_frame, persist=persist and not demo)
        if epark_frame is not None:
            if not {"url", "status", "source", "checked_at", "verified"}.issubset(epark_frame.columns):
                raise ValueError("EPARKマスタはurl,status,source,checked_at,verified列が必要です。")
            if persist and not demo:
                merge_cache(ROOT/cfg["epark"]["cache_path"], epark_frame, EPARK_COLUMNS)
        checker = EparkChecker(cfg["epark"], None if demo else ROOT/cfg["epark"]["cache_path"], epark_frame, as_of=as_of)
        bar = st.progress(0, text="判定を開始します")
        def progress(done, total):
            if done % max(1, total//100) == 0 or done == total:
                bar.progress(done/total, text=f"{done:,} / {total:,}件")
        result = run_pipeline(table, mapping, master, cfg, licenses, checker, prefecture=prefecture,
                              as_of=as_of, options=options, extra=extra, html_files=html_files,
                              allow_network=allow_network, progress=progress)
        files = build_outputs(table, result, None if output_encoding == "入力に合わせる" else output_encoding)
        fingerprint = current_fingerprint()
        st.session_state["completed"] = {"fingerprint": fingerprint, "result": result, "files": files}
        bar.empty()
    completed = st.session_state.get("completed")
    if completed and completed["fingerprint"] != fingerprint:
        st.info("入力・条件が変わりました。判定開始を押して結果を更新してください。")
    elif completed:
        result, files = completed["result"], completed["files"]
        st.success(f"最終インポート対象：{len(result.final_indices):,}件")
        metrics = st.columns(3)
        for i, (label, value) in enumerate(result.metrics.items()):
            metrics[i%3].metric(label, f"{value:,}")
        st.caption("要確認には、対象に残したパターン4や補足確認のある行も含むため、一部の件数は重複します。")
        buttons = st.columns(2)
        captions = {"final_comdesk_import.csv": "最終コムデスク形式CSV", "judged_all.xlsx": "判定付き全件Excel",
                    "excluded.csv": "除外理由付きCSV", "needs_review.csv": "要確認リストCSV",
                    "final_comdesk_import.xlsx": "最終コムデスク形式Excel"}
        for i, (name, data) in enumerate(files.items()):
            buttons[i%2].download_button(captions[name], data, name,
                                         mime="text/csv" if name.endswith(".csv") else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                         use_container_width=True)
        st.download_button("結果をまとめてダウンロード", zip_outputs(files), "clinic_filter_results.zip", "application/zip")
        view = result.judgments[["入力行番号", "医院名", "営業対象判定", "営業優先度", "59歳以下確率", "EPARK課金判定", "最終判定理由"]].copy()
        view["59歳以下確率"] = view["59歳以下確率"].map(lambda v: "不明" if pd.isna(v) else f"{v:.1%}")
        st.dataframe(view, hide_index=True, use_container_width=True)


try:
    main()
except ValueError as exc:
    st.error(str(exc))
except Exception as exc:
    st.error(f"処理を完了できませんでした（{type(exc).__name__}）。ファイル形式とマスタ設定を確認してください。")
