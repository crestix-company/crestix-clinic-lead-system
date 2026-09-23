"""第一営業部向けローカルアプリ。importだけでは画面/DB/ネットを動かさない。"""
from pathlib import Path
from dataclasses import asdict
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
from src.master.store import ClinicStore
from src.master.filters import Filters
from src.master.jobs import JobRunner,create_job,job_status,recent_jobs,pause_job,job_limit
from src.master.samples import load_demo
from src.scoring.research_scoring import SIGNAL_NAMES

NAV = ["ダッシュボード","マスター管理","自動情報収集","営業対象フィルター","要確認・設定"]
HP_LABELS = {"UNRESEARCHED":"未調査","VERIFIED":"HP確認済み","REVIEW":"要確認","NOT_FOUND":"HP未発見","ERROR":"取得エラー"}
CONTRACT_LABELS = {"UNKNOWN":"不明","PAID":"課金済み","FREE":"無課金"}
ADS_LABELS = {"UNKNOWN":"不明","CONFIRMED":"確認済み","NOT_CONFIRMED":"未確認"}
JOB_LABELS = {"RUNNING":"実行中","PAUSED":"一時停止","COMPLETED":"完了","BUDGET":"検索上限で停止"}


@st.cache_resource
def store_for(path):
    return ClinicStore(path)


@st.cache_resource
def runner_for(path):
    return JobRunner()


def filters_ui(store,prefix="sales",defaults=None):
    defaults = defaults or Filters()
    with store.connect() as c:
        prefs = [r[0] for r in c.execute("SELECT DISTINCT prefecture FROM clinics WHERE prefecture<>'' ORDER BY prefecture")]
    treatment_names = list(read_config(ROOT/"config/treatment_keywords.yml"))
    cols = st.columns(3)
    def key(name):
        return prefix+"_"+name
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
        deps = st.multiselect("診療科",DEPARTMENTS,default=defaults.departments,key=key("departments"))
        ranks = st.multiselect("HPランク",["A","B","C","D","NO_HP","UNKNOWN"],default=defaults.ranks,key=key("ranks"),format_func=lambda x:{"NO_HP":"HPなし","UNKNOWN":"未評価"}.get(x,x))
        equal = st.selectbox("開設者＝管理者",["指定なし","一致のみ","不一致のみ"],index=["指定なし","一致のみ","不一致のみ"].index(defaults.owner_equal),key=key("owner"))
    with cols[2]:
        treatments = st.multiselect("治療カテゴリ",treatment_names,default=defaults.treatments,key=key("treatments"))
        minimum = st.number_input("集客投資シグナル数の下限",0,len(SIGNAL_NAMES),defaults.signal_min,key=key("min"))
        hot = st.multiselect("アツさ",["通常","集客投資シグナルあり","アツい","かなりアツい"],default=defaults.hot,key=key("hot"))
        new = st.checkbox("前回の厚生局更新から追加された医院",value=defaults.new_only,key=key("new"))
        opening = st.checkbox("新規開業候補（1年以内・新規指定）",value=defaults.recent_opening,key=key("opening"))
    signals = st.multiselect("必須の集客投資シグナル（選んだものすべて）",SIGNAL_NAMES,default=defaults.signals,key=key("signals"))
    keyword = st.text_input("医院名・電話番号・UUIDで検索",value=defaults.keyword,key=key("keyword"))
    st.caption("条件同士はAND。診療科・治療カテゴリなど同じ項目の複数選択はORです。HP未発見は「存在しない」と断定した状態ではありません。")
    return Filters(active_only=active,hp_only=hp,recent_only=recent,age_min=age/100 if age is not None else None,
                   medical_types=medical,prefectures=pref,departments=deps,ranks=ranks,treatments=treatments,
                   signal_min=minimum,hot=hot,owner_equal=equal,uuid_mode=uid,new_only=new,recent_opening=opening,maps_confirmed_only=maps_confirmed,signals=signals,keyword=keyword)


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
            "HPランク":{"NO_HP":"HPなし","UNKNOWN":"未評価"}.get(r.get("hp_rank"),r.get("hp_rank","未評価")),
            "EPARK URL":r.get("epark_url",""),"EPARK契約":CONTRACT_LABELS.get(r.get("epark_contract"),"不明"),
            "治療カテゴリ":" / ".join(r.get("treatment_categories",[])),"集客投資シグナル数":r.get("marketing_signal_count",0),
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
        options = list(read_config(ROOT/"config/treatment_keywords.yml")) if field=="treatment_categories" else [s for s in SIGNAL_NAMES if s not in {"Googleスポンサー広告確認済み","EPARK課金済み確認"}]
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


def research_ui(store,demo):
    st.write("HP・EPARK・外部媒体を分けて調査します。APIキーはこの画面の利用中だけ保持します。")
    api_key = st.text_input("Tavily APIキー",type="password",key="tavily_api_key",placeholder="環境変数 TAVILY_API_KEY でも設定できます",disabled=demo)
    if demo:
        st.info("サンプルは調査済みです。営業対象フィルターから出力を試してください。")
    with store.connect() as c:
        used = c.execute("SELECT count(*) FROM search_usage WHERE month=?",(today_japan().strftime("%Y-%m"),)).fetchone()[0]
    st.metric("今月の検索試行数",used)
    limit = st.number_input("月間検索上限",0,100000,int(store.setting("monthly_limit",900)),key="monthly_limit_input")
    reserve = st.number_input("他のアプリ等で使用済みの検索数（控除）",0,100000,int(store.setting("external_usage_reserve",0)),key="reserve_input")
    if st.button("月間上限を保存"):
        store.set_setting("monthly_limit",limit);store.set_setting("external_usage_reserve",reserve)
        st.success("上限を保存しました。")
    st.caption("失敗した検索も1回として数えます。このDB以外の使用量は自動取得しないため、上の控除欄に入力してください。")
    with st.expander("調査対象を絞る",expanded=False):
        f = filters_ui(store,"research",Filters(active_only=True,hp_only=False))
    kind_label = st.selectbox("調査内容",["公式HPの発見・内容調査","EPARK掲載ページ調査","外部媒体調査"])
    kind = {"公式HPの発見・内容調査":"hp","EPARK掲載ページ調査":"epark","外部媒体調査":"media"}[kind_label]
    cols = st.columns(3)
    count = cols[0].number_input("今回の医院数",1,500,100)
    max_search = cols[1].number_input("今回の検索上限",0,1000,100)
    max_pages = cols[2].number_input("1医院の最大HPページ数",1,30,20)
    force = st.checkbox("強制再調査（調査済みも対象・新しい検索を行う）")
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
        increased = st.number_input("この調査の検索上限（再開用）",0,10000,int(current["max_searches"]),key="resume_limit_"+selected)
        cols = st.columns(2)
        if cols[0].button("続きから再開",disabled=runner.running() or current["status"]=="COMPLETED"):
            provider = TavilySearchProvider(api_key)
            job_limit(store,selected,increased)
            runner.start(store,selected,provider)
        if cols[1].button("一時停止",disabled=not runner.running()):
            pause_job(store,selected)
        show_job_progress(store,selected)


@st.fragment(run_every=2)
def show_job_progress(store,jid):
    job = job_status(store,jid)
    done = job["counts"].get("DONE",0)
    st.progress(done/max(1,job["total"]),text=f"完了 {done} / {job['total']}件　｜　{JOB_LABELS.get(job['status'],job['status'])}")
    result = job["results"]
    st.write(f"調査成功 {result.get('SUCCESS',0)} ／ 未発見 {result.get('NOT_FOUND',0)} ／ 要確認 {result.get('REVIEW',0)} ／ エラー {result.get('ERROR',0)} ／ Tavily検索 {job['search_count']}回")
    st.caption("停止はページ取得の区切りで反映されます。画面を更新しても完了済みの医院は再処理しません。")


def sales_ui(store):
    saved_filters = st.session_state.get("selected_sales_filters")
    if saved_filters is None:
        with store.connect() as c:
            has_maps = c.execute("SELECT 1 FROM clinics WHERE maps_presence_status<>'' LIMIT 1").fetchone() is not None
        defaults = Filters(maps_confirmed_only=has_maps)
    else:
        defaults = Filters(**saved_filters)
    filters = filters_ui(store,"sales",defaults)
    st.session_state["selected_sales_filters"] = asdict(filters)
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
    st.set_page_config(page_title="クリニック営業マスター",page_icon="📋",layout="wide")
    st.title("クリニック営業マスター")
    st.caption("第一営業部｜マスター統合 → 自動情報収集 → 営業対象の抽出")
    with st.sidebar:
        demo = st.toggle("サンプルモード（APIキー不要）",key="sample_mode")
        nav = st.radio("画面",NAV,key="navigation")
        st.caption("すべてのシグナルは1点。2個でアツい、3個以上でかなりアツい。")
    path = Path(os.getenv("CLINIC_DEMO_DB_PATH",str(ROOT/"data/demo.sqlite3"))) if demo else Path(os.getenv("CLINIC_DB_PATH",str(ROOT/"data/clinics.sqlite3")))
    store = store_for(str(path))
    store.refresh_age_model()
    if demo:
        load_demo(store)
        st.info("架空サンプル4医院を表示しています。実データとは別に保存されます。")
    if nav=="ダッシュボード":
        cols = st.columns(5)
        for i,(label,value) in enumerate(store.metrics().items()):
            cols[i%5].metric(label,f"{value:,}")
        if not store.count():
            st.info("左のサンプルモードで操作を試せます。実データは「マスター管理」から既存コムデスクを取り込んでください。")
        filters = Filters(**st.session_state.get("selected_sales_filters",{}))
        st.subheader("現在の営業対象フィルターによる件数")
        show_funnel(store,filters)
        st.caption("営業対象フィルター画面で条件を変えると、この件数にも反映されます。")
    elif nav=="マスター管理":
        import_ui(store,demo)
    elif nav=="自動情報収集":
        research_ui(store,demo)
    elif nav=="営業対象フィルター":
        sales_ui(store)
    else:
        settings_ui(store)


if __name__=="__main__":
    try:
        main()
    except (ValueError,SearchError,WebError) as exc:
        st.error(str(exc))
    except sqlite3.OperationalError:
        st.error("データファイルが使用中か、保存先に書き込めません。別ウィンドウの処理が終わってから再操作してください。")
    except Exception:
        st.error("処理を完了できませんでした。ファイル形式・入力列・保存先を確認してください。")
