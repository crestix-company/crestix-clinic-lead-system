"""第一営業部向けローカルアプリ。importだけでは画面/DB/ネットを動かさない。"""
from pathlib import Path
from dataclasses import asdict, replace
import json
import os
import sqlite3
import pandas as pd
import streamlit as st
from src.utils.config import ROOT,read_config
from src.utils.date_utils import today_japan,within_years
from src.io.input_loader import load_table,sheet_names
from src.master.comdesk import COMDESK_HEADERS, infer_comdesk_columns
from src.master.fixed_export import fixed_row
from src.enrichment.kouseikyoku_source import load_master
from src.enrichment.doctor_license import DoctorLicenseCache,parse_license_html
from src.enrichment.profiles import estimate_profile_age
from src.enrichment.search_provider import TavilySearchProvider,SearchError,CachedSearch
from src.enrichment.safe_web import WebError
from src.normalizer.departments import DEPARTMENTS
from src.master.store import ClinicStore,mhlw_sidecar_available,mhlw_official_department_options,MhlwSidecarUnavailableError
from src.master.research_sidecar import research_sidecar_available,clinic_research_status_available,treatment_research_category_options,ResearchSidecarUnavailableError,RESEARCH_STATUS_UI_OPTIONS
from src.master.filters import Filters
from src.master.scope import SCOPE_ALL,SCOPE_LEGACY_PRE_NATIONAL,SCOPE_LABELS
from src.master.jobs import JobRunner,create_job,job_status,recent_jobs,pause_job,reset_job,job_limit
from src.master.samples import load_demo
from src.scoring.research_scoring import SIGNAL_NAMES,AD_SIGNAL_LABELS
from src.master.filters import AD_COUNT_SQL
from src.master.sales_treatments import sales_treatment_master
from src.enrichment.treatment_taxonomy import treatment_category_names
from src.master.sales_classification import (
    sales_classification_summary,
    SalesClassificationUnavailableError,
)
from src.master.hp_batch_metrics import web_research_metrics, BatchMetricInvariantError
from src.master.hp_site_type import SITE_TYPE_LABELS, SITE_OFFICIAL, SITE_PORTAL, SITE_OTHER
from src.master.data_paths import production_db_path

NAV = ["かんたん操作","営業対象・出力","詳細設定"]
HP_LABELS = {"UNRESEARCHED":"未調査","VERIFIED":"HP確認済み","REVIEW":"要確認","NOT_FOUND":"HP未発見","ERROR":"取得エラー"}
CONTRACT_LABELS = {"UNKNOWN":"不明","PAID":"課金済み","FREE":"無課金"}
ADS_LABELS = {"UNKNOWN":"不明","CONFIRMED":"確認済み","NOT_CONFIRMED":"未確認"}
JOB_LABELS = {"RUNNING":"実行中","PAUSED":"一時停止","COMPLETED":"完了","BUDGET":"検索上限で停止","RESET":"リセット済み"}
# 正式Sales Tier分類(SSOT)のプリセット。旧hp_rank(HPランクA/B)とは別軸で、営業対象判定の主軸はこちら。
SALES_TIER_PRESETS = [
    ("指定なし（Sales Tier不問）", []),
    ("A VERIFIED", ["A"]),
    ("B LIKELY", ["B"]),
    ("C SPECIALTY", ["C"]),
    ("A+B", ["A", "B"]),
    ("A+B+C 営業対象", ["A", "B", "C"]),
    ("D UNKNOWN", ["D"]),
]
SALES_TIER_PRESET_DEFAULT_INDEX = next(i for i, (label, _) in enumerate(SALES_TIER_PRESETS) if label == "A+B+C 営業対象")
SALES_TIER_PRESET_UNRESTRICTED_INDEX = next(i for i, (label, _) in enumerate(SALES_TIER_PRESETS) if label == "指定なし（Sales Tier不問）")
# 正式採用モデルv2-precision-firstのeffective_hp_rankを通常営業UIの主軸にする。
# Production hp_rankは旧値として保持し、v2 sidecarとの合成はruntimeでのみ行う。
HP_RANK_PRESETS = [
    ("指定なし", []),
    ("A+B", ["A", "B"]),
    ("A", ["A"]),
    ("B", ["B"]),
    ("C", ["C"]),
    ("D", ["D"]),
]
HP_RANK_PRESET_DEFAULT_INDEX = next(i for i, (label, _) in enumerate(HP_RANK_PRESETS) if label == "A+B")
HP_RANK_PRESET_UNRESTRICTED_INDEX = next(i for i, (label, _) in enumerate(HP_RANK_PRESETS) if label == "指定なし")
TREATMENT_STATUS_DISPLAY_LABELS = {
    "FETCHED": "治療カテゴリ検出あり",
    "DONE_NO_CATEGORY": "治療カテゴリ検出なし",
    "FETCH_FAILED": "Webサイト調査失敗",
    "NOT_RESEARCHED": "Webサイト未調査",
}
@st.cache_resource
def store_for(path):
    return ClinicStore(path)


def show_web_research_metrics(store):
    """通常UIのWebサイト調査状況SSOT。HPの公式性は断定しない。"""
    try:
        counts = web_research_metrics(store.path)
    except BatchMetricInvariantError as exc:
        st.error(f"Webサイト調査件数を表示できません：{exc}")
        return
    if counts is None:
        st.warning("HP research batch sidecarがないため、Webサイト調査状況を表示できません。")
        return
    definitions = [
        ("WebサイトURL取得済み", "url_acquired", "医院データにWebサイトURLが登録されている医院数です。"),
        ("Webサイト調査完了", "researched", "登録されたWebサイトの取得・解析を完了し、HP ABC判定と治療カテゴリ判定まで完了した医院数です。"),
        ("Webサイト調査失敗", "failed", "Webサイトの取得または解析を正常完了できなかった医院数です。"),
        ("Webサイト未調査", "not_researched", "WebサイトURLは登録されていますが、まだ自動調査が完了していない医院数です。"),
        ("治療カテゴリ検出あり", "treatment_detected", "Webサイトから対象の治療・検査・施術カテゴリが1種類以上確認された医院数です。診療科の件数ではありません。"),
        ("治療カテゴリ検出なし", "treatment_not_detected", "Webサイト調査は完了していますが、現在定義している治療・検査・施術カテゴリが確認されなかった医院数です。"),
    ]
    for start in range(0, len(definitions), 3):
        columns = st.columns(3)
        for column, (label, key, help_text) in zip(columns, definitions[start:start + 3]):
            column.metric(label, f"{counts[key]:,}件", help=help_text)


def resolve_production_db_path():
    """Production DBの絶対パスを検証する。存在しない/開けない場合は自動生成せずエラー文を返す。

    CLINIC_DB_PATH未設定時にrepo内の古いDBへ黙ってfallbackし、
    利用者が気づかないまま古いデータを見てしまう事故を防ぐ。
    """
    path = production_db_path()
    if not path.exists():
        return None, f"CLINIC_DB_PATHで指定されたファイルが見つかりません： {path}"
    if not path.is_file():
        return None, f"CLINIC_DB_PATHはファイルではありません（ディレクトリ等が指定されています）： {path}"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return None, f"CLINIC_DB_PATHをSQLiteとして開けません： {path}（{exc}）"
    if "clinics" not in tables:
        return None, f"CLINIC_DB_PATHにclinicsテーブルがありません： {path}"
    return path, None


@st.cache_resource
def runner_for(path):
    return JobRunner()


def ad_count_options(store):
    # 施策数の選択肢は実データの最大施策数から作る（固定の大きな数字は並べない）。
    with store.connect() as c:
        top = c.execute("SELECT MAX("+AD_COUNT_SQL+") FROM clinics").fetchone()[0] or 0
    return [0]+list(range(2,top+1))


def ad_count_label(n):
    return "指定なし" if n==0 else f"{n}施策以上"


def filters_ui(store,prefix="sales",defaults=None):
    defaults = defaults or Filters()
    with store.connect() as c:
        prefs = [r[0] for r in c.execute("SELECT DISTINCT prefecture FROM clinics WHERE prefecture<>'' ORDER BY prefecture")]
        municipality_options = [r[0] for r in c.execute("SELECT DISTINCT municipality_of(address) FROM clinics WHERE municipality_of(address)<>'' ORDER BY 1")]
    treatment_names = treatment_category_names()
    def key(name):
        return prefix+"_"+name
    scope_options = [SCOPE_LEGACY_PRE_NATIONAL, SCOPE_ALL]
    scope = st.selectbox("対象データ", scope_options,
                          index=scope_options.index(defaults.scope) if defaults.scope in scope_options else 0,
                          format_func=lambda s: SCOPE_LABELS[s], key=key("scope"))
    cols = st.columns(3)
    with cols[0]:
        active = st.checkbox("現存クリニックのみ",value=defaults.active_only,key=key("active"))
        hp = st.checkbox("HP確認済みのみ",value=defaults.hp_only,key=key("hp"))
        recent = st.checkbox("開業10年以内（指定年月日基準）",value=defaults.recent_only,key=key("recent"))
        age_on = st.checkbox("59歳以下確率で絞る",value=defaults.age_min is not None,key=key("age_on"))
        age = st.slider("59歳以下確率の下限（%）",0,100,int((defaults.age_min if defaults.age_min is not None else .5)*100),key=key("age")) if age_on else None
        uid = st.selectbox("既存UUID",["指定なし","あり","なし"],index=["指定なし","あり","なし"].index(defaults.uuid_mode),key=key("uuid"))
        maps_confirmed = st.checkbox("Google Maps掲載確認済みのみ",value=defaults.maps_confirmed_only,key=key("maps_confirmed"))
    with cols[1]:
        medical = st.multiselect("医科・歯科",["医科","歯科"],default=defaults.medical_types,key=key("medical"))
        pref = st.multiselect("都道府県",prefs,default=[x for x in defaults.prefectures if x in prefs],key=key("pref"))
        muni = st.multiselect("市区町村",municipality_options,default=[x for x in defaults.municipalities if x in municipality_options],key=key("municipalities"))
        deps = st.multiselect("診療科",DEPARTMENTS,default=defaults.departments,key=key("departments"))
        if mhlw_sidecar_available():
            try:
                official_options = mhlw_official_department_options()
            except MhlwSidecarUnavailableError as exc:
                official_options = []
                st.warning(str(exc))
            mhlw_official_deps = st.multiselect("ナビイ正式診療科",official_options,
                default=[x for x in defaults.mhlw_official_departments if x in official_options],key=key("mhlw_official_departments"),
                help="厚生労働省 医療情報ネット（ナビイ）の正式診療科名で完全一致します。既存の厚生局由来「診療科」とは別軸です。")
            crestix_options = list(sales_treatment_master())
            legacy_crestix_defaults = defaults.crestix_sales_departments or defaults.mhlw_departments
            crestix_deps = st.multiselect("Crestix営業カテゴリ",crestix_options,
                default=[x for x in legacy_crestix_defaults if x in crestix_options],key=key("crestix_sales_departments"),
                help="ナビイ正式診療科から明示mappingした営業用カテゴリです。正式診療科名を上書きしません。")
        else:
            mhlw_official_deps, crestix_deps = [], []
            st.caption("ナビイ診療科sidecarがないため、ナビイ正式診療科・Crestix営業カテゴリfilterは利用できません。既存filterは通常どおり利用できます。")
        ranks = st.multiselect("HP ABC判定",["A","B","C","D"],default=[x for x in defaults.ranks if x in {"A","B","C","D"}],key=key("ranks"))
        equal = st.selectbox("開設者＝管理者",["指定なし","一致のみ","不一致のみ"],index=["指定なし","一致のみ","不一致のみ"].index(defaults.owner_equal),key=key("owner"))
    with cols[2]:
        treatments = st.multiselect("治療カテゴリ",treatment_names,default=defaults.treatments,key=key("treatments"))
        if research_sidecar_available():
            try:
                hp_treatment_options = treatment_research_category_options()
            except ResearchSidecarUnavailableError as exc:
                hp_treatment_options = []
                st.warning(str(exc))
            hp_treatments = st.multiselect("HP治療カテゴリ（Treatment Research・CONFIRMEDのみ）",hp_treatment_options,
                default=[x for x in defaults.hp_treatment_categories if x in hp_treatment_options],key=key("hp_treatment_categories"),
                help="公式HP上でCONFIRMED（提供確認済み）の治療カテゴリのみ営業対象にします。既存「治療カテゴリ」（厚生局treatments_json由来）とは別軸です。")
        else:
            hp_treatments = []
            st.caption("Treatment Research sidecarがないため、HP治療カテゴリfilterは利用できません。既存filterは通常どおり利用できます。")
        if clinic_research_status_available():
            research_status = st.multiselect("Research Status",RESEARCH_STATUS_UI_OPTIONS,
                default=[x for x in defaults.research_status if x in RESEARCH_STATUS_UI_OPTIONS],key=key("research_status"),
                help="医院単位の調査状態（DONE/FETCH_FAILED/NOT_RESEARCHED）で絞り込みます。NOT_RESEARCHEDは該当医院の調査行が1件もない状態です。HP治療カテゴリfilterとは独立した軸です。")
        else:
            research_status = []
            st.caption("Treatment Research sidecar（clinic_research_status）がないため、Research Status filterは利用できません。既存filterは通常どおり利用できます。")
        ad_options = ad_count_options(store)
        ad_min = st.selectbox("広告・集客施策数",ad_options,index=ad_options.index(defaults.ad_min) if defaults.ad_min in ad_options else 0,format_func=ad_count_label,key=key("ad_min"))
        new = st.checkbox("前回の厚生局更新から追加された医院",value=defaults.new_only,key=key("new"))
        opening = st.checkbox("新規開業候補（1年以内・新規指定）",value=defaults.recent_opening,key=key("opening"))
    signals = st.multiselect("集客投資シグナル（いずれか）",SIGNAL_NAMES,default=defaults.signals,key=key("signals"))
    keyword = st.text_input("医院名・電話番号・UUIDで検索",value=defaults.keyword,key=key("keyword"))
    st.caption("条件同士はAND。診療科・治療カテゴリなど同じ項目の複数選択はORです。HP未発見は「存在しない」と断定した状態ではありません。")
    return Filters(active_only=active,hp_only=hp,recent_only=recent,age_min=age/100 if age is not None else None,
                   medical_types=medical,prefectures=pref,municipalities=muni,departments=deps,mhlw_official_departments=mhlw_official_deps,
                   crestix_sales_departments=crestix_deps,ranks=ranks,treatments=treatments,
                   hp_treatment_categories=hp_treatments,research_status=research_status,
                   ad_min=ad_min,owner_equal=equal,uuid_mode=uid,new_only=new,recent_opening=opening,maps_confirmed_only=maps_confirmed,signals=signals,keyword=keyword,
                   scope=scope)


def show_mhlw_join_status(store):
    """正式採用したfinal sidecarの連携状況を簡潔に表示する。"""
    with store.connect() as c:
        attached = {r[1] for r in c.execute("PRAGMA database_list")}
        if "mhlwdb" not in attached:
            st.info("ナビイ診療科sidecarが見つかりません。既存filterのみ利用できます。")
            return
        matched = c.execute("SELECT count(DISTINCT clinic_id) FROM mhlwdb.clinic_mhlw_departments_final").fetchone()[0]
        total = c.execute("SELECT count(*) FROM clinics WHERE merged_into IS NULL").fetchone()[0]
    st.caption(f"ナビイ診療科連携：{matched:,} / {total:,}医院（{matched/total:.2%}）｜未MATCHED/REVIEW：{total-matched:,}医院")


def show_funnel(store,filters):
    steps = store.funnel(filters)
    frame = pd.DataFrame(steps,columns=["条件を順に適用","件数"])
    st.dataframe(frame,hide_index=True,width="stretch")


def display_rows(records):
    rows = []
    for r in records:
        prob = r.get("age_probability_under_59")
        rows.append({"管理番号":r["id"],"UUID":r.get("uuid",""),"医院名":r.get("clinic_name",""),"電話番号":r.get("phone",""),
            "住所":r.get("address",""),"医科/歯科":r.get("medical_type",""),"診療科":r.get("departments",""),
            "開設者":r.get("owner_name",""),"管理者":r.get("manager_name",""),
            "開設者＝管理者":"不明" if r.get("owner_manager_equal") is None else "一致" if r["owner_manager_equal"] else "不一致",
            "指定年月日":r.get("designation_date",""),"開業10年以内":{True:"該当",False:"非該当",None:"不明"}[within_years(r.get("designation_date",""))],
            "59歳以下確率":prob,"Maps掲載":r.get("maps_presence_status",""),"Maps HP":r.get("maps_website_url",""),"HP URL":r.get("hp_url",""),"HP状態":HP_LABELS.get(r.get("hp_status"),"不明"),
            "HP ABC":r.get("effective_hp_rank", "D"),
            "サイト種別":SITE_TYPE_LABELS.get(r.get("site_type"), "その他・未確認"),
            "ポータル名":r.get("portal_name", ""),
            "UUID有無":"あり" if r.get("uuid") else "なし",
            "HP ABC判定理由":r.get("effective_rank_reason", ""),
            "EPARK URL":r.get("epark_url",""),"EPARK契約":CONTRACT_LABELS.get(r.get("epark_contract"),"不明"),
            "治療カテゴリ":" / ".join(r.get("website_treatment_categories") or r.get("treatment_categories",[])),
            "治療カテゴリ状態":TREATMENT_STATUS_DISPLAY_LABELS.get(r.get("treatment_status"),"不明"),
            "集客投資シグナル数":r.get("marketing_signal_count",0),
            "アツさ":r.get("hot_status","通常"),"シグナル一覧":" / ".join(s["name"] for s in r.get("marketing_signals",[]))})
    return pd.DataFrame(rows)


def details(store,cid):
    r = store.get(cid)
    st.subheader(r.get("clinic_name","医院詳細"))
    if r.get("maps_profile_url"):
        st.link_button("Google Mapsを開く",r["maps_profile_url"])
    if r.get("maps_website_url"):
        st.link_button("Google Maps登録HPを開く",r["maps_website_url"])
    if r.get("hp_url"):
        st.link_button("公式HPを開く",r["hp_url"])
    if r.get("epark_url"):
        st.link_button("EPARKを開く",r["epark_url"])
    st.write("HP確認："+" / ".join(r.get("hp_match_reason",[]) or [HP_LABELS.get(r.get("hp_status"),"未調査")]))
    st.write(f"HP ABC：{r.get('effective_hp_rank','D')}（{r.get('effective_rank_reason','')}）")
    reasons = r.get("hp_rank_reasons",[])
    if reasons:
        st.write("HPランクの理由："+" / ".join(f"{p['feature']}（{p['points']}点）" for p in reasons))
    if r.get("age_probability_under_59") is not None:
        st.write(f"59歳以下である確率：{r['age_probability_under_59']:.1%}")
    st.caption("年齢の根拠："+str(r.get("age_estimation_source","未取得"))+"　"+str(r.get("age_estimation_reason","")))
    if r.get("graduation_evidence"):
        st.dataframe(pd.DataFrame(r["graduation_evidence"]).rename(columns={"year":"卒業年","url":"根拠URL","evidence":"記載","doctor_name":"医師名"}),hide_index=True)
    if r.get("treatment_evidence"):
        st.write("治療カテゴリの根拠")
        st.dataframe(pd.DataFrame(r["treatment_evidence"]).rename(columns={"category":"治療","url":"根拠URL","keyword":"キーワード","confidence":"確度","reason":"理由"}),hide_index=True,
                     column_config={"根拠URL":st.column_config.LinkColumn()})
    if r.get("marketing_signals"):
        st.write(f"集客投資シグナル {r['marketing_signal_count']}個 → {r['hot_status']}")
        frame = pd.DataFrame(r["marketing_signals"])
        if "status" in frame:
            frame["status"] = frame["status"].replace({"CONFIRMED":"確認済み","UNKNOWN":"不明"})
        st.dataframe(frame.rename(columns={"name":"施策","evidence_url":"根拠URL","evidence_type":"確認方法","evidence":"根拠","service_name":"サービス名","status":"確認状態"}),hide_index=True,
                     column_config={"根拠URL":st.column_config.LinkColumn()})
    for label,key in [("HP候補・本人確認","hp_candidates"),("取得できなかったページ","crawl_errors"),("EPARKの確認事項","epark_issues")]:
        if r.get(key):
            with st.expander(label):
                st.dataframe(pd.DataFrame(r[key]).rename(columns={"url":"URL","reason":"理由","reasons":"確認理由","score":"本人確認点数","verified":"本人確認済み","phone_match":"電話一致","name_match":"医院名一致","address_match":"住所一致"}),hide_index=True)
    if r.get("epark_candidates"):
        st.write("EPARK候補（未確認）：")
        for url in r["epark_candidates"]:
            st.link_button(url,url)
    st.caption("自動判定は取得できた公開ページに基づきます。HPランクは機能・コンテンツの評価です。")
    with st.expander("この医院の判定を手動で修正"):
        manual_form(store,r)
    with st.expander("変更履歴"):
        history = store.history(cid)
        if history:
            frame = pd.DataFrame(history).rename(columns={"action":"操作","note":"メモ","created_at":"更新日時","before_json":"変更前","after_json":"変更後"})
            st.dataframe(frame[["更新日時","操作","メモ","変更前","変更後"]],hide_index=True)


def manual_form(store,r):
    fields = {"HP URL":"hp_url","HP確認状態":"hp_status","HPランク":"hp_rank","EPARK URL":"epark_url",
              "EPARK契約":"epark_contract","Googleスポンサー広告":"google_ads_status","治療カテゴリ":"treatment_categories","集客投資シグナル":"marketing_signals"}
    cid = r["id"]
    field_label = st.selectbox("修正する項目",list(fields),key=f"manual_field_{cid}")
    field = fields[field_label]
    key = f"manual_value_{cid}_{field}"
    enums = {"hp_status":HP_LABELS,"epark_contract":CONTRACT_LABELS,"google_ads_status":ADS_LABELS,
             "hp_rank":{x:x for x in ["UNKNOWN","A","B","C","D","NO_HP"]}}
    if field in enums:
        options = list(enums[field]); old = r.get(field)
        value = st.selectbox("修正後の値",options,index=options.index(old) if old in options else 0,format_func=lambda v:enums[field][v],key=key)
    elif field in {"treatment_categories","marketing_signals"}:
        options = treatment_category_names() if field=="treatment_categories" else [s for s in SIGNAL_NAMES if s not in {"Googleスポンサー広告確認済み","EPARK課金済み確認"}]
        selected = r.get(field,[]) if field=="treatment_categories" else [s["name"] for s in r.get(field,[])]
        value = st.multiselect("確認できた項目",options,default=[s for s in selected if s in options],key=key)
        if field=="marketing_signals":
            st.caption("Google広告とEPARK課金は、それぞれの専用項目で修正してください。")
    else:
        value = st.text_input("修正後のURL",value=r.get(field,"") or "",key=key)
    note = st.text_input("根拠URL・確認メモ",key=f"manual_note_{cid}")
    cols = st.columns(2)
    if cols[0].button("手動修正を保存",key=f"manual_save_{cid}"):
        if not note.strip():
            st.warning("確認した根拠URLや理由をメモに入力してください。")
        else:
            store.override(cid,field,value,note)
            st.success("手動値を保存しました。再調査しても保持されます。")
            st.rerun()
    if field in r.get("manual_fields",[]) and cols[1].button("この項目を自動値に戻す",key=f"manual_reset_{cid}"):
        store.override(cid,field,None,note)
        st.rerun()


def listing(store,filters,prefix="list"):
    count = store.count(filters)
    st.write(f"対象：{count:,}件")
    size = st.selectbox("1ページの表示件数",[25,50,100,200],index=1,key=prefix+"_size")
    pages = max(1,(count+size-1)//size)
    if st.session_state.get(prefix+"_page",1)>pages:
        st.session_state[prefix+"_page"] = 1
    number = st.number_input("ページ",1,pages,1,key=prefix+"_page")
    records = store.query(filters,limit=size,offset=(number-1)*size)
    if records:
        statuses = store.treatment_status_for_ids([r["id"] for r in records])
        for r in records:
            r["treatment_status"] = statuses.get(r["id"])
        st.dataframe(display_rows(records),hide_index=True,width="stretch",column_config={"Maps HP":st.column_config.LinkColumn(),"HP URL":st.column_config.LinkColumn(),"EPARK URL":st.column_config.LinkColumn(),"59歳以下確率":st.column_config.NumberColumn(format="percent")})
        choice = st.selectbox("詳細を確認する医院",[None]+[r["id"] for r in records],format_func=lambda cid:"選択してください" if cid is None else next(f"{r['id']}｜{r.get('clinic_name','')}" for r in records if r["id"]==cid),key=prefix+"_detail")
        if choice:
            details(store,choice)


def upload_table(upload,prefix):
    names = sheet_names(upload.getvalue(),upload.name)
    selected = st.selectbox("読み込むシート",names,key=prefix+"_sheet") if names else None
    return load_table(upload.getvalue(),upload.name,sheet=selected)


def import_ui(store,demo):
    st.subheader("1. 既存コムデスクを取り込む")
    st.write("厚生局データ・既存コムデスクは、どちらを先に登録しても使えます。両方を登録後、「保存済みデータを再統合」で既存UUIDへ紐づけられます。")
    st.caption("クリニック名だけでも取り込めます。C列「名前」はクリニック名、AA列「院長名」は先生のお名前です。出力は毎回A〜AB列の28項目になり、不明な項目は空欄です。")
    upload = st.file_uploader("コムデスクCSV・Excel",type=["csv","xlsx","xls"],key="comdesk_upload",disabled=demo)
    if upload:
        table = upload_table(upload,"comdesk")
        inferred = infer_comdesk_columns(table)
        mapping = {}
        labels = {"uuid":"UUID・案件ID","clinic_name":"医院名","phone":"電話番号（Tel1）", "prefecture":"都道府県",
                  "address":"住所・住所１","address2":"住所２・建物名","postal_code":"郵便番号",
                  "manager_name":"院長名","url":"HP URL","epark_url":"EPARK URL"}
        with st.expander("列の対応を確認",expanded=True):
            st.caption("28列形式の「名前」「Tel1」「住所１・住所２」も自動対応します。分割住所は照合時に結合し、出力用の元の行は保持します。")
            for field,label in labels.items():
                choices = [None]+list(range(len(table.headers)))
                mapping[field] = st.selectbox(label+"の列",choices,index=choices.index(inferred.get(field)),format_func=lambda i:"未設定" if i is None else f"{i+1}列目：{table.headers[i]}",key="comdesk_map_"+field)
            preview = table.data.head(5).copy()
            preview.columns = [f"{i+1}｜{v}" for i,v in enumerate(table.headers)]
            st.dataframe(preview,hide_index=True)
            st.caption("出力プレビュー（A〜AB列・28項目）")
            st.dataframe(pd.DataFrame([fixed_row({},table.headers,mapping,row) for row in table.data.head(5).values.tolist()],columns=COMDESK_HEADERS),hide_index=True,width="stretch")
        if st.button("既存案件を登録",type="primary"):
            with st.spinner("既存案件を登録しています…"):
                result = store.import_comdesk(table,mapping)
            report_import(result)
    st.subheader("2. 厚生局データを統合する")
    if st.button("同梱の東京都マスターを取り込む",disabled=demo):
        with st.spinner("東京都の全施設を統合しています…"):
            frame = pd.read_csv(ROOT/"data/master/tokyo_current.csv",dtype=str,keep_default_na=False)
            result = store.import_master(frame)
        report_import(result)
    st.caption("同梱データは2026-09-01基準の東京都医科一覧です。病院・休止施設も保持し、営業対象フィルターで現存クリニックを選びます。歯科一覧は別途取り込んでください。")
    st.write("取り込み済みのデータを再統合する")
    st.caption("先頭0・ハイフンの違いを統合判定用のキーで吸収します。元のTel1・UUIDは保持します。全レコードを保存し、曖昧な重複は確認待ちとして残します。")
    result_key = "reintegration_result_" + str(store.path)
    if st.button("保存済みデータを再統合",disabled=demo):
        with st.spinner("バックアップを作成し、保存済みの厚生局データを再照合しています…"):
            st.session_state[result_key] = store.reintegrate_existing()
    if result_key in st.session_state:
        re_result = st.session_state[result_key]
        report_import(re_result)
        backup_path = Path(re_result["backup_path"])
        if backup_path.is_file():
            st.download_button("再統合前のバックアップをダウンロード",backup_path.read_bytes(),backup_path.name,key="download_reintegration_backup")
    upload = st.file_uploader("更新用の厚生局CSV・Excel",type=["csv","xlsx","xls"],key="master_upload",disabled=demo)
    if upload:
        mode = st.selectbox("データ形式",["標準CSV/Excel","関東信越厚生局の帳票Excel"])
        pref = st.text_input("都道府県（帳票Excel用）","東京都")
        names = sheet_names(upload.getvalue(),upload.name)
        selected = st.selectbox("厚生局シート",names) if names else None
        if st.button("厚生局データを統合"):
            with st.spinner("読込・統合中…"):
                frame = load_master(upload.getvalue(),upload.name,"kanto_excel" if "帳票" in mode else "standard",pref,selected)
                result = store.import_master(frame)
            report_import(result)
    st.subheader("3. Google Maps収集アプリへ渡す")
    st.caption("厚生局を母集団にした調査キューです。病院・センターは収集アプリ側で検索前に除外されます。")
    if st.download_button("Google Maps調査キューCSVをダウンロード",store.google_maps_queue_csv(),"google_maps_research_queue.csv",disabled=demo,key="maps_queue_download"):
        pass
    st.subheader("4. Google Maps取得結果を取り込む")
    maps_upload = st.file_uploader("Google Maps収集結果CSV",type=["csv"],key="maps_results_upload",disabled=demo)
    if maps_upload:
        maps_table = load_table(maps_upload.getvalue(),maps_upload.name)
        maps_frame = maps_table.data.copy()
        maps_frame.columns = maps_table.headers
        st.dataframe(maps_frame.head(10),hide_index=True,width="stretch")
        if st.button("Google Maps取得結果を取り込む",type="primary",key="maps_import_button"):
            with st.spinner("Google Maps結果をマスターへ紐付けています…"):
                result = store.import_google_maps(maps_frame)
            if result.get("already_imported"):
                st.info("このGoogle Maps結果は取込済みです。重複登録していません。")
            st.success(f"取込 {result.get('TOTAL',0):,}件／自動紐付け {result.get('MATCHED',0):,}件／HP取得 {result.get('WEBSITE',0):,}件／HPなし {result.get('NO_WEBSITE',0):,}件／Maps未発見 {result.get('NOT_FOUND',0):,}件／要確認 {result.get('AMBIGUOUS',0):,}件／除外 {result.get('EXCLUDED',0):,}件／エラー {result.get('ERROR',0):,}件")
    st.download_button("管理用フルCSVをダウンロード",store.export_management_csv(),"clinic_master_full.csv",disabled=demo,key="full_master_download")

    with st.expander("5. 医籍登録年を補完する"):
        upload = st.file_uploader("医籍CSV・Excel・保存HTML",type=["csv","xlsx","xls","html","htm"],key="license_upload",disabled=demo)
        if upload:
            frame = parse_license_html(upload.getvalue()) if Path(upload.name).suffix.lower() in {".html",".htm"} else upload_table(upload,"license").frame()
            if st.button("医籍情報を反映"):
                cache = DoctorLicenseCache(ROOT/"data/cache/doctor_license_cache.csv")
                cache.import_records(frame)
                updated = 0
                for record in store.query(limit=100000):
                    result = cache.lookup(record.get("manager_name",""),record.get("clinic_id",""),record.get("phone",""))
                    if result.year:
                        age = estimate_profile_age({**record,"license_registration_year":result.year,"license_source":result.source})
                        store.save_research(record["id"],{**age,"license_source":result.source})
                        updated += 1
                st.success(f"{updated:,}医院の年齢推定を更新しました。")
        st.caption("同姓同名は自動で一人に決めません。公式HPの卒業年も自動調査で補完できます。")
    with st.expander("マスター一覧"):
        listing(store,Filters(active_only=False,hp_only=False),"master")


def report_import(result):
    if result.get("already_imported"):
        st.info("このファイルは取込済みです。重複登録していません。")
    else:
        st.success(f"統合 {result['MATCHED']:,}件／新規追加 {result['NEW']:,}件／要確認重複 {result['AMBIGUOUS']:,}件")
    if result.get("reintegrated"):
        st.caption(f"保存済み厚生局データ {result['source_records']:,}件の再判定結果です。「新規追加」は、既存コムデスクに一致せず独立して残る医院を含みます。今回まとめた重複：{result['merged_clinics']:,}組。")




def _create_maps_hp_job(store, prefecture, limit, force=False, medical_types=None):
    """Google MapsでHP URL取得済み医院だけを対象にHP調査ジョブを作る。"""
    import uuid as _uuid
    from src.master.store import now as _now, dumps as _dumps

    conditions = [
        "merged_into IS NULL",
        "merge_hold=0",
        "active=1",
        "maps_presence_status='MAPS_MATCHED_WEBSITE'",
        "maps_website_url<>''",
    ]
    args = []
    if prefecture:
        conditions.append("prefecture=?")
        args.append(prefecture)
    if medical_types:
        placeholders = ",".join("?" for _ in medical_types)
        conditions.append(f"medical_type IN ({placeholders})")
        args.extend(medical_types)
    if not force:
        conditions.append("hp_status='UNRESEARCHED'")

    jid = _uuid.uuid4().hex
    with store.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        ids = [r[0] for r in c.execute(
            "SELECT id FROM clinics WHERE " + " AND ".join(conditions) +
            " ORDER BY (uuid<>'') DESC,is_new DESC,id LIMIT ?",
            (*args, min(500, max(1, int(limit))))
        )]
        if not ids:
            raise ValueError("現在の条件でHP調査できる医院がありません。")
        c.execute(
            "INSERT INTO research_jobs(id,kind,options_json,max_searches,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (jid, "hp", _dumps({"force": bool(force), "max_pages": 20}), 0, _now(), _now())
        )
        c.executemany(
            "INSERT INTO research_job_items(job_id,clinic_id) VALUES(?,?)",
            [(jid, i) for i in ids]
        )
    return jid


def _maps_hp_available_count(store, prefecture="", force=False, medical_types=None):
    conditions = [
        "merged_into IS NULL",
        "merge_hold=0",
        "active=1",
        "maps_presence_status='MAPS_MATCHED_WEBSITE'",
        "maps_website_url<>''",
    ]
    args = []
    if prefecture:
        conditions.append("prefecture=?")
        args.append(prefecture)
    if medical_types:
        placeholders = ",".join("?" for _ in medical_types)
        conditions.append(f"medical_type IN ({placeholders})")
        args.extend(medical_types)
    if not force:
        conditions.append("hp_status='UNRESEARCHED'")
    with store.connect() as c:
        return c.execute(
            "SELECT count(*) FROM clinics WHERE " + " AND ".join(conditions),
            args
        ).fetchone()[0]


def simple_workflow_ui(store, demo):
    metrics = store.metrics()

    st.header("上から順に進めるだけ")
    st.caption("普段使う操作だけを4ステップにまとめました。細かい設定・手動修正は「詳細設定」にあります。")

    st.subheader("現在のデータ状況")
    cols = st.columns(2)
    cols[0].metric("厚生局データ", f"{metrics.get('厚生局取込レコード',0):,}件")
    cols[1].metric("既存UUIDあり", f"{metrics.get('既存UUIDあり',0):,}件", help="既存Comdeskデータと紐付いている医院数です。")
    show_web_research_metrics(store)

    if metrics.get("厚生局取込レコード", 0) == 0:
        st.warning("厚生局データがまだありません。最初に東京都マスターを取り込んでください。")
        if st.button("東京都マスターを取り込む", type="primary", disabled=demo, key="simple_import_tokyo", use_container_width=True):
            with st.spinner("東京都マスターを取り込んでいます…"):
                frame = pd.read_csv(ROOT/"data/master/tokyo_current.csv", dtype=str, keep_default_na=False)
                result = store.import_master(frame)
            report_import(result)
            st.rerun()

    st.subheader("1. 厚生局データを取り込む")

    st.subheader("2. 別途の拡張機能（Google Maps Collector）で、1.で取り込んだデータを入れる（手動）")

    st.subheader("3. 拡張機能で抽出したデータを戻す")

    maps_upload = st.file_uploader(
        "③ Google Maps収集結果CSVをアップロード",
        type=["csv"],
        key="simple_maps_results",
        disabled=demo,
    )
    if maps_upload:
        maps_table = load_table(maps_upload.getvalue(), maps_upload.name)
        maps_frame = maps_table.data.copy()
        maps_frame.columns = maps_table.headers

        if st.button("Google Maps結果を取り込む", type="primary", key="simple_maps_import", use_container_width=True):
            with st.spinner("Google Maps結果を取り込んでいます…"):
                result = store.import_google_maps(maps_frame)
            st.success(
                f"取込 {result.get('TOTAL',0):,}件／HP取得 {result.get('WEBSITE',0):,}件／"
                f"HPなし {result.get('NO_WEBSITE',0):,}件／要確認 {result.get('AMBIGUOUS',0):,}件／"
                f"エラー {result.get('ERROR',0):,}件"
            )
            st.rerun()

    st.subheader("4. HP内容を自動調査")
    with store.connect() as c:
        prefs = [r[0] for r in c.execute(
            "SELECT DISTINCT prefecture FROM clinics WHERE prefecture<>'' ORDER BY prefecture"
        )]
    pref_options = ["すべて"] + prefs
    default_pref = pref_options.index("東京都") if "東京都" in pref_options else 0
    pref_label = st.selectbox("都道府県", pref_options, index=default_pref, key="simple_research_pref")
    pref = "" if pref_label == "すべて" else pref_label
    med_label = st.selectbox("医科・歯科", ["医科", "歯科", "両方"], index=0, key="simple_research_medical_type")
    research_medical_types = ["医科", "歯科"] if med_label == "両方" else [med_label]

    force = st.checkbox("調査済みもやり直す", value=False, key="simple_force")
    available = _maps_hp_available_count(store, pref, force, research_medical_types)

    st.info(f"現在の条件でHP調査できる医院：{available:,}件")

    default_count = min(50, available) if available > 0 else 50
    count = st.number_input(
        "最大調査件数",
        min_value=1,
        max_value=500,
        value=max(1, default_count),
        key="simple_count",
    )
    actual = min(int(count), available)
    st.caption(
        f"今回実際に調査する件数：{actual:,}件。"
        "設定が50件でも対象が34件なら34件だけ調査します。"
    )

    runner = runner_for(str(store.path))
    jobs = recent_jobs(store)
    current = job_status(store, jobs[0]["id"]) if jobs else None

    if st.button(
        "HP自動調査を開始",
        type="primary",
        disabled=demo or runner.running() or actual == 0,
        key="simple_start",
        use_container_width=True,
    ):
        provider = TavilySearchProvider("")
        jid = _create_maps_hp_job(store, pref, int(count), force, research_medical_types)
        st.session_state["active_job"] = jid
        runner.start(store, jid, provider)
        st.rerun()

    if current:
        st.write("現在の調査")
        st.caption(f"この調査は開始時点で {current['total']:,}件に固定されています。")
        controls = st.columns(2)

        can_pause = runner.running() or current["status"] == "RUNNING"
        if controls[0].button("一時停止", disabled=not can_pause, key="simple_pause", use_container_width=True):
            pause_job(store, current["id"])
            st.rerun()

        can_reset = not runner.running() and current["status"] != "RUNNING"
        if controls[1].button("この調査をリセット", disabled=not can_reset, key="simple_reset", use_container_width=True):
            try:
                reset_job(store, current["id"])
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))

        show_job_progress(store, current["id"])

    with st.expander("HP調査の詳細（通常は触らなくてOK）", expanded=False):
        st.write("Google MapsでHP取得済み医院だけを直接調査します。")
        st.write("最大HPページ数：20ページ")
        st.write("HP未取得医院の検索、EPARK、外部媒体の調査は「詳細設定」から行えます。")

    st.subheader("5. Comdesk形式で出力")
    st.caption("HP確認済みで、まだUUIDが付いていない医院を、営業条件で絞らずComdesk形式で出力します。")

    step5_filters = Filters(active_only=False, hp_only=True, uuid_mode="なし")
    step5_count = store.count(step5_filters)
    st.write(f"対象：{step5_count:,}件")

    step5_signature = json.dumps(
        [str(store.path), asdict(step5_filters), COMDESK_HEADERS, store.revision()],
        ensure_ascii=False,
        sort_keys=True,
    )
    if st.button("CSV・Excelを作成", type="primary", key="simple_step5_export", disabled=step5_count == 0, use_container_width=True):
        st.session_state["simple_step5_export_files"] = {
            "signature": step5_signature,
            "files": store.export(step5_filters),
        }

    step5_output = st.session_state.get("simple_step5_export_files")
    if step5_output and step5_output["signature"] == step5_signature:
        for name, content in step5_output["files"].items():
            st.download_button(
                "Excelをダウンロード" if name.endswith("xlsx") else "CSVをダウンロード",
                content,
                name,
                key="simple_step5_download_" + name,
                use_container_width=True,
            )

    st.subheader("6. 5.で出したデータを手動でComdeskに入れる（＝UUIDを付与）")

    st.subheader("7. 6.でUUIDが付与されたデータをこのアプリに取り込む")

    step7_upload = st.file_uploader("コムデスクCSV・Excel", type=["csv", "xlsx", "xls"], key="simple_comdesk_upload", disabled=demo)
    if step7_upload:
        table = upload_table(step7_upload, "simple_comdesk")
        inferred = infer_comdesk_columns(table)
        mapping = {}
        labels = {"uuid":"UUID・案件ID","clinic_name":"医院名","phone":"電話番号（Tel1）", "prefecture":"都道府県",
                  "address":"住所・住所１","address2":"住所２・建物名","postal_code":"郵便番号",
                  "manager_name":"院長名","url":"HP URL","epark_url":"EPARK URL"}
        with st.expander("列の対応を確認", expanded=True):
            st.caption("28列形式の「名前」「Tel1」「住所１・住所２」も自動対応します。分割住所は照合時に結合し、出力用の元の行は保持します。")
            for field, label in labels.items():
                choices = [None] + list(range(len(table.headers)))
                mapping[field] = st.selectbox(label+"の列", choices, index=choices.index(inferred.get(field)), format_func=lambda i:"未設定" if i is None else f"{i+1}列目：{table.headers[i]}", key="simple_comdesk_map_"+field)
            preview = table.data.head(5).copy()
            preview.columns = [f"{i+1}｜{v}" for i,v in enumerate(table.headers)]
            st.dataframe(preview, hide_index=True)
            st.caption("出力プレビュー（A〜AB列・28項目）")
            st.dataframe(pd.DataFrame([fixed_row({},table.headers,mapping,row) for row in table.data.head(5).values.tolist()],columns=COMDESK_HEADERS), hide_index=True, width="stretch")
        if st.button("既存案件を登録", type="primary", key="simple_comdesk_import"):
            with st.spinner("既存案件を登録しています…"):
                result = store.import_comdesk(table, mapping)
            report_import(result)


def simple_sales_ui(store, demo=False):
    st.header("営業対象・Comdesk出力")
    st.caption("普段使う営業条件だけを表示しています。")
    st.subheader("Webサイト調査状況")
    show_web_research_metrics(store)

    scope_options = [SCOPE_LEGACY_PRE_NATIONAL, SCOPE_ALL]
    # 2026-10-05: 正式Sales Tier分類(SSOT)のcohortを旧13,970件のlegacy scopeへ
    # 取りこぼさないよう、既定値だけを全国Clinic Masterへ変更（表示順・選択肢はそのまま）。
    scope = st.selectbox("対象データ", scope_options, index=scope_options.index(SCOPE_ALL), format_func=lambda s: SCOPE_LABELS[s], key="simple_sales_scope")
    with store.connect() as c:
        prefs = [r[0] for r in c.execute(
            "SELECT DISTINCT prefecture FROM clinics WHERE prefecture<>'' ORDER BY prefecture"
        )]
    pref_default = ["東京都"] if "東京都" in prefs else []
    med_label = st.selectbox("医科・歯科", ["医科", "歯科", "両方"], index=0, key="simple_sales_medical_type")
    sales_medical_types = ["医科", "歯科"] if med_label == "両方" else [med_label]
    pref = st.multiselect("都道府県", prefs, default=pref_default, key="simple_sales_pref")
    deps = st.multiselect("診療科", DEPARTMENTS, key="simple_sales_departments")
    sales_master = sales_treatment_master()
    previous_deps = set(st.session_state.get("simple_sales_previous_departments", ()))
    removed_deps = previous_deps - set(deps)
    for department in removed_deps:
        st.session_state["simple_sales_pair_" + department] = []
    st.session_state["simple_sales_previous_departments"] = list(deps)
    sales_pairs = []
    if deps:
        st.caption("選択した標榜診療科ごとに、HPで確認済みの治療を絞り込みます（既存機能）。")
    for department in deps:
        items = sales_master.get(department, ())
        if not items:
            continue
        st.markdown(f"**【{department}】**")
        labels = [item.treatment for item in items]
        selected = st.multiselect(
            f"{department}の治療",
            labels,
            key="simple_sales_pair_" + department,
            label_visibility="collapsed",
            format_func=lambda label, by_label={item.treatment:item for item in items}:
                label + ("（現Research未対応）" if by_label[label].support_status == "MISSING" else ""),
        )
        sales_pairs.extend((department, treatment) for treatment in selected)

    status_preview_filters = Filters(
        active_only=True, hp_only=False, medical_types=sales_medical_types,
        prefectures=pref, departments=deps, sales_pairs=sales_pairs, scope=scope,
    )
    ads = st.multiselect("広告・集客施策", list(AD_SIGNAL_LABELS), format_func=AD_SIGNAL_LABELS.get, key="simple_sales_ads")
    ad_min = st.selectbox("広告・集客施策数", ad_count_options(store), format_func=ad_count_label, key="simple_sales_ad_min")
    companies = st.multiselect("HP制作会社", list(read_config(ROOT/"config/production_companies.yml")), key="simple_sales_companies")

    cols = st.columns(4)
    recent = cols[0].checkbox("開業10年以内", key="simple_sales_recent")
    age = cols[1].checkbox("59歳以下 50%以上", key="simple_sales_age")
    # 通常営業UIはeffective_hp_rankを使用。Treatment状態はこの判定に影響しない。
    rank_labels = [label for label, _ in HP_RANK_PRESETS]
    default_rank_index = HP_RANK_PRESET_UNRESTRICTED_INDEX if demo else HP_RANK_PRESET_DEFAULT_INDEX
    rank_choice = cols[2].selectbox("HP ABC判定", rank_labels, index=default_rank_index, key="simple_sales_hp_rank_preset")
    hp_ranks = dict(HP_RANK_PRESETS)[rank_choice]
    site_type_label = cols[3].selectbox(
        "サイト種別", ["指定なし", "公式HP確認済み", "ポータルサイト", "その他・未確認"],
        key="simple_sales_site_type",
        help="HP ABC判定・治療カテゴリとは独立した分類です。公式HP確認済みは既存の公式確認根拠がある医院だけです。",
    )
    site_types = {
        "指定なし": [], "公式HP確認済み": [SITE_OFFICIAL],
        "ポータルサイト": [SITE_PORTAL], "その他・未確認": [SITE_OTHER],
    }[site_type_label]
    # 「既存UUID」は日常運用では営業担当が意識する必要がないため、通常画面からは外している
    # （詳細設定の営業対象フィルター（詳細）には残している）。UUIDあり/なしは両方営業対象になり得る
    # （UUIDなし＝新規案件）。出力時に強制的にUUIDありへ絞り込むことはしない。

    keyword = st.text_input("医院名・電話番号で検索", key="simple_sales_keyword")

    filters = replace(
        status_preview_filters,
        recent_only=recent,
        age_min=.5 if age else None,
        effective_ranks=hp_ranks,
        site_types=site_types,
        signals=ads,
        ad_min=ad_min,
        production_companies=companies,
        keyword=keyword,
    )

    # 「指定なし」でも営業対象の定義は常にeffective A+B。C/Dは一覧監査には使えるが
    # 営業対象メトリクスとComdeskには入れない。
    export_ranks = [r for r in (hp_ranks or ["A", "B"]) if r in ("A", "B")]
    non_ab_only_selected = bool(hp_ranks) and not export_ranks
    sales_filters = replace(filters, effective_ranks=export_ranks)
    try:
        count = 0 if non_ab_only_selected else store.count(sales_filters)
    except SalesClassificationUnavailableError as exc:
        st.error(str(exc))
        return
    st.metric("営業対象", f"{count:,}件")
    summary = sales_classification_summary()
    if summary:
        st.caption(
            f"参考：Sales Tier分類（{summary['source_path'].name}、HP ABC判定とは別軸）："
            f"A={summary['by_tier']['A']:,}　B={summary['by_tier']['B']:,}　"
            f"C={summary['by_tier']['C']:,}　D={summary['by_tier']['D']:,}（営業対象外）　"
            f"｜営業利用可能(A+B+C)：{summary['sales_usable']:,}件"
        )

    with st.expander("対象医院を確認", expanded=False):
        listing(store, filters, "simple_sales_results")

    st.subheader("Comdesk形式で出力")
    st.caption("A〜ABの28列固定。診療時間は HH:MM 形式で出力します。HP ABC判定がA・B以外の医院は出力しません。")

    # 営業対象はHP ABC判定のA+Bのみ。選択内容に関わらずComdesk出力はA/Bに限定する
    # （C/D/UNKNOWN/NO_HPを選んで一覧確認はできるが、出力対象にはならない）。
    # UUIDの有無では絞り込まない。UUIDなし（新規案件）もUUIDあり（既存案件）と同様に出力する。
    export_ranks = [r for r in (hp_ranks or ["A", "B"]) if r in ("A", "B")]
    # A+B以外だけを選択した場合、export_ranksが空になりranks未指定(無制限)と区別できなくなる。
    # 空=無制限ではなく「0件」として扱い、意図せずC/D/UNKNOWN/NO_HPを出力しない。
    non_ab_only_selected = bool(hp_ranks) and not export_ranks
    if non_ab_only_selected:
        st.caption(f"選択されたHP ABC判定「{rank_choice}」は営業対象外のため、Comdesk出力はできません。")
    export_filters = sales_filters

    try:
        export_count = 0 if non_ab_only_selected else store.count(export_filters)
        uuid_yes_count = 0 if non_ab_only_selected else store.count(replace(export_filters, uuid_mode="あり"))
        uuid_no_count = 0 if non_ab_only_selected else store.count(replace(export_filters, uuid_mode="なし"))
    except SalesClassificationUnavailableError as exc:
        st.error(str(exc))
        return

    export_cols = st.columns(3)
    export_cols[0].metric("Comdesk出力対象", f"{export_count:,}件")
    export_cols[1].metric("既存案件(UUIDあり)", f"{uuid_yes_count:,}件")
    export_cols[2].metric("新規案件(UUIDなし)", f"{uuid_no_count:,}件")
    if not non_ab_only_selected and set(hp_ranks or ["A", "B"]) <= {"A", "B"} and export_count != count:
        st.caption(
            f"注意：営業対象（{count:,}件）とComdesk出力対象（{export_count:,}件）が一致していません。"
            "絞り込み条件がA+B以外を含んでいないか確認してください。"
        )

    signature = json.dumps(
        [str(store.path), asdict(export_filters), COMDESK_HEADERS, store.revision()],
        ensure_ascii=False,
        sort_keys=True,
    )
    export_disabled = non_ab_only_selected or export_count == 0
    if st.button("CSV・Excelを作成", type="primary", key="simple_export", disabled=export_disabled, use_container_width=True) and not non_ab_only_selected:
        st.session_state["simple_export_files"] = {
            "signature": signature,
            "files": store.export(export_filters),
        }

    output = st.session_state.get("simple_export_files")
    if output and output["signature"] == signature:
        for name, content in output["files"].items():
            st.download_button(
                "Excelをダウンロード" if name.endswith("xlsx") else "CSVをダウンロード",
                content,
                name,
                key="simple_download_" + name,
                use_container_width=True,
            )


def advanced_ui(store, demo):
    st.header("詳細設定")
    st.caption("普段は使わない設定・確認機能です。")
    section = st.selectbox(
        "開く画面",
        ["マスター管理", "自動情報収集（詳細）", "営業対象フィルター（詳細）", "要確認・設定", "ダッシュボード"],
    )

    if section == "マスター管理":
        import_ui(store, demo)
    elif section == "自動情報収集（詳細）":
        research_ui(store, demo)
    elif section == "営業対象フィルター（詳細）":
        sales_ui(store)
    elif section == "要確認・設定":
        settings_ui(store)
    else:
        cols = st.columns(5)
        for i, (label, value) in enumerate(store.metrics().items()):
            cols[i % 5].metric(label, f"{value:,}")

def research_ui(store,demo):
    reset_message = st.session_state.pop("research_reset_message", None)
    if reset_message:
        st.success(reset_message)
    st.write("HP・EPARK・外部媒体を分けて調査します。")
    api_key = ""
    if demo:
        st.info("サンプルは調査済みです。営業対象フィルターから出力を試してください。")
    with st.expander("調査対象を絞る",expanded=False):
        f = filters_ui(store,"research",Filters(active_only=True,hp_only=False))
    kind_label = st.selectbox("調査内容",["公式HPの発見・内容調査","EPARK掲載ページ調査","外部媒体調査"])
    kind = {"公式HPの発見・内容調査":"hp","EPARK掲載ページ調査":"epark","外部媒体調査":"media"}[kind_label]
    cols = st.columns(2)
    count = cols[0].number_input("今回の医院数",1,500,100)
    max_search = 100
    max_pages = cols[1].number_input("1医院の最大HPページ数",1,30,20)
    force = st.checkbox("強制再調査（調査済みも対象・新しい検索を行う）")
    st.caption("「今回の医院数」は新しい調査を開始した時点で固定されます。実行中の30件を50件に変更しても、その調査は30件のままです。やり直す場合は『一時停止 → この調査をリセット → 医院数を設定 → 新規開始』の順です。")
    runner = runner_for(str(store.path))
    if st.button("自動調査を開始",type="primary",disabled=demo or runner.running()):
        provider = TavilySearchProvider(api_key)
        jid = create_job(store,f,kind,count,max_search,force,max_pages)
        st.session_state["active_job"] = jid
        runner.start(store,jid,provider)
    jobs = recent_jobs(store)
    if jobs:
        options = [j["id"] for j in jobs]
        selected = st.selectbox("調査履歴・再開する調査",options,format_func=lambda jid:next(f"{j['created_at'][:16]}｜{dict(hp='HP',epark='EPARK',media='外部媒体')[j['kind']]}｜{JOB_LABELS.get(j['status'],j['status'])}" for j in jobs if j["id"]==jid))
        current = job_status(store,selected)
        st.caption(f"この調査は開始時点で {current['total']} 医院に固定されています。上の『今回の医院数』を変更しても、この調査の件数は変わりません。")
        increased = int(current["max_searches"])
        cols = st.columns(3)
        if cols[0].button("続きから再開",disabled=runner.running() or current["status"]=="COMPLETED"):
            provider = TavilySearchProvider(api_key)
            job_limit(store,selected,increased)
            runner.start(store,selected,provider)
        if cols[1].button("一時停止",disabled=not runner.running()):
            pause_job(store,selected)
        reset_disabled = runner.running() or current["status"] == "RUNNING"
        if cols[2].button("この調査をリセット",disabled=reset_disabled,help="この調査の進捗履歴だけをリセットします。医院の調査結果、Google Maps取込結果、UUID、月間検索使用数は削除しません。"):
            try:
                reset_job(store,selected)
                if st.session_state.get("active_job") == selected:
                    st.session_state.pop("active_job",None)
                st.session_state["research_reset_message"] = "調査の進捗をリセットしました。保存済みの医院調査結果は削除していません。新しい医院数を設定して『自動調査を開始』してください。"
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
        show_job_progress(store,selected)


@st.fragment(run_every=2)
def show_job_progress(store,jid):
    job = job_status(store,jid)
    done = job["counts"].get("DONE",0)
    st.progress(done/max(1,job["total"]),text=f"完了 {done} / {job['total']}件　｜　{JOB_LABELS.get(job['status'],job['status'])}")
    result = job["results"]
    st.write(f"調査成功 {result.get('SUCCESS',0)} ／ 未発見 {result.get('NOT_FOUND',0)} ／ 要確認 {result.get('REVIEW',0)} ／ エラー {result.get('ERROR',0)}")
    st.caption("停止はページ取得の区切りで反映されます。画面を更新しても完了済みの医院は再処理しません。")


def sales_ui(store):
    saved_filters = st.session_state.get("selected_sales_filters")
    if saved_filters is None:
        with store.connect() as c:
            has_maps = c.execute("SELECT 1 FROM clinics WHERE maps_presence_status<>'' LIMIT 1").fetchone() is not None
        defaults = Filters(maps_confirmed_only=has_maps, scope=SCOPE_LEGACY_PRE_NATIONAL)
    else:
        defaults = Filters(**saved_filters)
    filters = filters_ui(store,"sales",defaults)
    st.session_state["selected_sales_filters"] = asdict(filters)
    with st.expander("ナビイ診療科連携状況",expanded=False):
        show_mhlw_join_status(store)
    with st.expander("選択条件での絞り込み件数",expanded=True):
        show_funnel(store,filters)
    listing(store,filters,"sales_results")
    st.subheader("コムデスク形式で出力する")
    st.caption("出力形式：A〜AB列の28項目（固定）。C列「名前」にクリニック名、AA列「院長名」に先生のお名前を出力します。入力にない項目は確認できた情報を補い、不明な項目は空欄にします。")
    with st.expander("毎回1行目に出力する28項目",expanded=True):
        st.dataframe(pd.DataFrame({"列":[chr(65+i) if i<26 else "A"+chr(65+i-26) for i in range(28)],"1行目の項目名":COMDESK_HEADERS}),hide_index=True,width="stretch")
    signature = json.dumps([str(store.path),asdict(filters),COMDESK_HEADERS,store.revision()],ensure_ascii=False,sort_keys=True)
    if st.button("この条件でExcel・CSVを作成",type="primary"):
        files = store.export(filters)
        st.session_state["export_v2"] = {"signature":signature,"files":files}
    output = st.session_state.get("export_v2")
    if output and output["signature"]==signature:
        for name,content in output["files"].items():
            st.download_button("営業対象Excel" if name.endswith("xlsx") else "営業対象CSV",content,name,key="download_"+name)


def settings_ui(store):
    st.subheader("要確認重複")
    reviews = store.reviews()
    if not reviews:
        st.success("未処理の重複確認はありません。")
    else:
        st.caption("曖昧な元レコードは保留しています。統合先を確認するまで営業対象の出力には入りません。")
        rid = st.selectbox("確認する重複",[r["id"] for r in reviews],format_func=lambda rid:next(f"{r['id']}｜{json.loads(r['record_json']).get('clinic_name','')}｜{r['match_reason']}" for r in reviews if r["id"]==rid))
        review = next(r for r in reviews if r["id"]==rid)
        record = json.loads(review["record_json"])
        st.write("取込データ：",record.get("clinic_name"),record.get("phone"),record.get("address"),"UUID："+record.get("uuid",""))
        candidates = [store.get(cid) for cid in json.loads(review["candidates_json"])]
        st.dataframe(display_rows(candidates),hide_index=True)
        target = st.selectbox("処理方法",[None]+[r["id"] for r in candidates],format_func=lambda cid:"別の医院として追加" if cid is None else f"管理番号 {cid} に統合")
        note = st.text_input("確認根拠・メモ",key="merge_note")
        if st.button("確認結果を保存"):
            if not note.strip():
                st.warning("確認した根拠をメモに入力してください。")
            else:
                store.resolve_review(rid,target,note)
                st.rerun()
    st.subheader("判定の確認・手動修正")
    keyword = st.text_input("確認する医院名・電話番号・UUID",key="review_search")
    if keyword:
        listing(store,Filters(active_only=False,hp_only=False,keyword=keyword),"review")
    st.subheader("データのバックアップ")
    st.caption("マスター・元のコムデスク行・根拠・手動修正・検索回数をまとめて保存します。")
    if st.button("バックアップを作成"):
        st.session_state["db_backup"] = store.backup_bytes()
    if "db_backup" in st.session_state:
        st.download_button("バックアップをダウンロード",st.session_state["db_backup"],f"clinics_backup_{today_japan().isoformat()}.sqlite3")
    st.write("使用中のデータファイル：",str(store.path))
    st.caption("復元：アプリを終了 → 現在のDBを退避 → バックアップを clinics.sqlite3 に変更して data フォルダーへ配置 → 起動。詳しくはREADME_V2.md。")


def main():
    st.set_page_config(page_title="クリニック営業マスター", page_icon="📋", layout="wide")
    st.title("クリニック営業マスター")
    st.caption("①データ準備 → ②Google Maps → ③HP調査 → ④営業対象・Comdesk出力")

    with st.sidebar:
        nav = st.radio("メニュー", NAV, key="navigation")
        with st.expander("開発・確認用", expanded=False):
            demo = st.toggle("サンプルモード", key="sample_mode")
        st.caption("普段は「かんたん操作」を上から順に進めればOKです。")

    if demo:
        path = Path(os.getenv("CLINIC_DEMO_DB_PATH", str(ROOT/"data/demo.sqlite3")))
    else:
        path, db_error = resolve_production_db_path()
        if db_error:
            st.error(db_error)
            st.stop()
    store = store_for(str(path))
    store.refresh_age_model()

    if demo:
        load_demo(store)
        st.info("架空サンプル4医院を表示しています。実データとは別に保存されます。")

    if nav == "かんたん操作":
        simple_workflow_ui(store, demo)
    elif nav == "営業対象・出力":
        simple_sales_ui(store, demo)
    else:
        advanced_ui(store, demo)


def user_error_text(exc):
    if isinstance(exc,SearchError) or "tavily" in str(exc).lower():
        return "外部検索を実行できませんでした。Google MapsのHP取得状況をご確認ください。"
    return str(exc)


if __name__=="__main__":
    try:
        main()
    except (ValueError,SearchError,WebError) as exc:
        st.error(user_error_text(exc))
    except sqlite3.OperationalError:
        st.error("データファイルが使用中か、保存先に書き込めません。別ウィンドウの処理が終わってから再操作してください。")
    except Exception as exc:
        import traceback
        traceback.print_exc()
        st.error(f"処理を完了できませんでした。ファイル形式・入力列・保存先を確認してください。エラー詳細: {user_error_text(exc)}")
