from pathlib import Path
from datetime import datetime
import re
import shutil
import py_compile

ROOT = Path.cwd()
APP = ROOT / "app_v2.py"

if not APP.exists():
    print("エラー：clinic-list-filter-complete フォルダで実行してください。")
    raise SystemExit(1)

text = APP.read_text(encoding="utf-8")
stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
backup = ROOT / ("code_backup_simple_ui_" + stamp)
backup.mkdir(parents=True, exist_ok=False)
shutil.copy2(APP, backup / "app_v2.py")

NEW_FUNCTIONS = '\ndef _create_maps_hp_job(store, prefecture, limit, force=False):\n    """Google MapsでHP URL取得済み医院だけを対象にHP調査ジョブを作る。"""\n    import uuid as _uuid\n    from src.master.store import now as _now, dumps as _dumps\n\n    conditions = [\n        "merged_into IS NULL",\n        "merge_hold=0",\n        "active=1",\n        "maps_presence_status=\'MAPS_MATCHED_WEBSITE\'",\n        "maps_website_url<>\'\'",\n    ]\n    args = []\n    if prefecture:\n        conditions.append("prefecture=?")\n        args.append(prefecture)\n    if not force:\n        conditions.append("hp_status=\'UNRESEARCHED\'")\n\n    jid = _uuid.uuid4().hex\n    with store.connect() as c:\n        c.execute("BEGIN IMMEDIATE")\n        ids = [r[0] for r in c.execute(\n            "SELECT id FROM clinics WHERE " + " AND ".join(conditions) +\n            " ORDER BY (uuid<>\'\') DESC,is_new DESC,id LIMIT ?",\n            (*args, min(500, max(1, int(limit))))\n        )]\n        if not ids:\n            raise ValueError("現在の条件でHP調査できる医院がありません。")\n        c.execute(\n            "INSERT INTO research_jobs(id,kind,options_json,max_searches,created_at,updated_at) "\n            "VALUES(?,?,?,?,?,?)",\n            (jid, "hp", _dumps({"force": bool(force), "max_pages": 20}), 0, _now(), _now())\n        )\n        c.executemany(\n            "INSERT INTO research_job_items(job_id,clinic_id) VALUES(?,?)",\n            [(jid, i) for i in ids]\n        )\n    return jid\n\n\ndef _maps_hp_available_count(store, prefecture="", force=False):\n    conditions = [\n        "merged_into IS NULL",\n        "merge_hold=0",\n        "active=1",\n        "maps_presence_status=\'MAPS_MATCHED_WEBSITE\'",\n        "maps_website_url<>\'\'",\n    ]\n    args = []\n    if prefecture:\n        conditions.append("prefecture=?")\n        args.append(prefecture)\n    if not force:\n        conditions.append("hp_status=\'UNRESEARCHED\'")\n    with store.connect() as c:\n        return c.execute(\n            "SELECT count(*) FROM clinics WHERE " + " AND ".join(conditions),\n            args\n        ).fetchone()[0]\n\n\ndef simple_workflow_ui(store, demo):\n    metrics = store.metrics()\n\n    st.header("上から順に進めるだけ")\n    st.caption("普段使う操作だけを4ステップにまとめました。細かい設定・手動修正は「詳細設定」にあります。")\n\n    st.subheader("1. 基本データを準備")\n    cols = st.columns(3)\n    cols[0].metric("厚生局データ", f"{metrics.get(\'厚生局取込レコード\',0):,}件")\n    cols[1].metric("Google Maps HP取得", f"{metrics.get(\'Google Maps HP取得\',0):,}件")\n    cols[2].metric("HP確認済み", f"{metrics.get(\'HP確認済み\',0):,}件")\n\n    if metrics.get("厚生局取込レコード", 0) == 0:\n        st.warning("厚生局データがまだありません。最初に東京都マスターを取り込んでください。")\n        if st.button("東京都マスターを取り込む", type="primary", disabled=demo, key="simple_import_tokyo", use_container_width=True):\n            with st.spinner("東京都マスターを取り込んでいます…"):\n                frame = pd.read_csv(ROOT/"data/master/tokyo_current.csv", dtype=str, keep_default_na=False)\n                result = store.import_master(frame)\n            report_import(result)\n            st.rerun()\n    else:\n        st.success("基本データは準備済みです。")\n    st.caption("既存Comdeskの取込、全国の厚生局更新、再統合は「詳細設定」で行えます。")\n\n    st.subheader("2. Google MapsでHPを集める")\n    st.write("① キューをダウンロード → ② Google Maps Collectorで調査 → ③ 結果CSVをここへ戻します。")\n\n    st.download_button(\n        "① Google Maps調査キューCSVをダウンロード",\n        store.google_maps_queue_csv(),\n        "google_maps_research_queue.csv",\n        disabled=demo,\n        key="simple_maps_queue",\n        use_container_width=True,\n    )\n\n    maps_upload = st.file_uploader(\n        "③ Google Maps収集結果CSVをアップロード",\n        type=["csv"],\n        key="simple_maps_results",\n        disabled=demo,\n    )\n    if maps_upload:\n        maps_table = load_table(maps_upload.getvalue(), maps_upload.name)\n        maps_frame = maps_table.data.copy()\n        maps_frame.columns = maps_table.headers\n\n        if st.button("Google Maps結果を取り込む", type="primary", key="simple_maps_import", use_container_width=True):\n            with st.spinner("Google Maps結果を取り込んでいます…"):\n                result = store.import_google_maps(maps_frame)\n            st.success(\n                f"取込 {result.get(\'TOTAL\',0):,}件／HP取得 {result.get(\'WEBSITE\',0):,}件／"\n                f"HPなし {result.get(\'NO_WEBSITE\',0):,}件／要確認 {result.get(\'AMBIGUOUS\',0):,}件／"\n                f"エラー {result.get(\'ERROR\',0):,}件"\n            )\n            st.rerun()\n\n    st.subheader("3. HP内容を自動調査")\n    with store.connect() as c:\n        prefs = [r[0] for r in c.execute(\n            "SELECT DISTINCT prefecture FROM clinics WHERE prefecture<>\'\' ORDER BY prefecture"\n        )]\n    pref_options = ["すべて"] + prefs\n    default_pref = pref_options.index("東京都") if "東京都" in pref_options else 0\n    pref_label = st.selectbox("都道府県", pref_options, index=default_pref, key="simple_research_pref")\n    pref = "" if pref_label == "すべて" else pref_label\n\n    force = st.checkbox("調査済みもやり直す", value=False, key="simple_force")\n    available = _maps_hp_available_count(store, pref, force)\n\n    st.info(f"現在の条件でHP調査できる医院：{available:,}件")\n\n    default_count = min(50, available) if available > 0 else 50\n    count = st.number_input(\n        "最大調査件数",\n        min_value=1,\n        max_value=500,\n        value=max(1, default_count),\n        key="simple_count",\n    )\n    actual = min(int(count), available)\n    st.caption(\n        f"今回実際に調査する件数：{actual:,}件。"\n        "設定が50件でも対象が34件なら34件だけ調査します。"\n    )\n\n    runner = runner_for(str(store.path))\n    jobs = recent_jobs(store)\n    current = job_status(store, jobs[0]["id"]) if jobs else None\n\n    if st.button(\n        "HP自動調査を開始",\n        type="primary",\n        disabled=demo or runner.running() or actual == 0,\n        key="simple_start",\n        use_container_width=True,\n    ):\n        provider = TavilySearchProvider("")\n        jid = _create_maps_hp_job(store, pref, int(count), force)\n        st.session_state["active_job"] = jid\n        runner.start(store, jid, provider)\n        st.rerun()\n\n    if current:\n        st.write("現在の調査")\n        st.caption(f"この調査は開始時点で {current[\'total\']:,}件に固定されています。")\n        controls = st.columns(2)\n\n        can_pause = runner.running() or current["status"] == "RUNNING"\n        if controls[0].button("一時停止", disabled=not can_pause, key="simple_pause", use_container_width=True):\n            pause_job(store, current["id"])\n            st.rerun()\n\n        can_reset = not runner.running() and current["status"] != "RUNNING"\n        if controls[1].button("この調査をリセット", disabled=not can_reset, key="simple_reset", use_container_width=True):\n            try:\n                reset_job(store, current["id"])\n                st.rerun()\n            except ValueError as exc:\n                st.error(str(exc))\n\n        show_job_progress(store, current["id"])\n\n    with st.expander("HP調査の詳細（通常は触らなくてOK）", expanded=False):\n        st.write("Google MapsでHP取得済み医院だけを直接調査します。")\n        st.write("Tavily検索：0回 / 最大HPページ数：20ページ")\n        st.write("HP未取得医院の検索、EPARK、外部媒体の調査は「詳細設定」から行えます。")\n\n    st.subheader("4. 営業対象を絞ってComdesk形式で出力")\n    st.write("HP調査が終わったら、営業条件を選んでA〜ABの28列固定で出力します。")\n    if st.button("営業対象・出力へ進む", type="primary", key="simple_to_sales", use_container_width=True):\n        st.session_state["navigation"] = "営業対象・出力"\n        st.rerun()\n\n\ndef simple_sales_ui(store):\n    st.header("営業対象・Comdesk出力")\n    st.caption("普段使う営業条件だけを表示しています。")\n\n    with store.connect() as c:\n        prefs = [r[0] for r in c.execute(\n            "SELECT DISTINCT prefecture FROM clinics WHERE prefecture<>\'\' ORDER BY prefecture"\n        )]\n    treatment_names = list(read_config(ROOT/"config/treatment_keywords.yml"))\n\n    pref_default = ["東京都"] if "東京都" in prefs else []\n    pref = st.multiselect("都道府県", prefs, default=pref_default, key="simple_sales_pref")\n    treatments = st.multiselect("治療カテゴリ", treatment_names, key="simple_sales_treatments")\n\n    cols = st.columns(4)\n    recent = cols[0].checkbox("開業10年以内", key="simple_sales_recent")\n    age = cols[1].checkbox("59歳以下 50%以上", key="simple_sales_age")\n    rank_ab = cols[2].checkbox("HPランク A/B", key="simple_sales_rank")\n    hot = cols[3].checkbox("アツい", help="集客シグナル2個以上", key="simple_sales_hot")\n\n    keyword = st.text_input("医院名・電話番号で検索", key="simple_sales_keyword")\n\n    filters = Filters(\n        active_only=True,\n        hp_only=True,\n        recent_only=recent,\n        age_min=.5 if age else None,\n        prefectures=pref,\n        treatments=treatments,\n        ranks=["A","B"] if rank_ab else [],\n        signal_min=2 if hot else 0,\n        keyword=keyword,\n    )\n\n    count = store.count(filters)\n    st.metric("営業対象", f"{count:,}件")\n\n    with st.expander("対象医院を確認", expanded=False):\n        listing(store, filters, "simple_sales_results")\n\n    st.subheader("Comdesk形式で出力")\n    st.caption("A〜ABの28列固定。診療時間は HH:MM 形式で出力します。")\n\n    signature = json.dumps(\n        [str(store.path), asdict(filters), COMDESK_HEADERS, store.revision()],\n        ensure_ascii=False,\n        sort_keys=True,\n    )\n    if st.button("CSV・Excelを作成", type="primary", key="simple_export", disabled=count==0, use_container_width=True):\n        st.session_state["simple_export_files"] = {\n            "signature": signature,\n            "files": store.export(filters),\n        }\n\n    output = st.session_state.get("simple_export_files")\n    if output and output["signature"] == signature:\n        for name, content in output["files"].items():\n            st.download_button(\n                "Excelをダウンロード" if name.endswith("xlsx") else "CSVをダウンロード",\n                content,\n                name,\n                key="simple_download_" + name,\n                use_container_width=True,\n            )\n\n\ndef advanced_ui(store, demo):\n    st.header("詳細設定")\n    st.caption("普段は使わない設定・確認機能です。")\n    section = st.selectbox(\n        "開く画面",\n        ["マスター管理", "自動情報収集（詳細）", "営業対象フィルター（詳細）", "要確認・設定", "ダッシュボード"],\n    )\n\n    if section == "マスター管理":\n        import_ui(store, demo)\n    elif section == "自動情報収集（詳細）":\n        research_ui(store, demo)\n    elif section == "営業対象フィルター（詳細）":\n        sales_ui(store)\n    elif section == "要確認・設定":\n        settings_ui(store)\n    else:\n        cols = st.columns(5)\n        for i, (label, value) in enumerate(store.metrics().items()):\n            cols[i % 5].metric(label, f"{value:,}")\n'
NEW_MAIN = 'def main():\n    st.set_page_config(page_title="クリニック営業マスター", page_icon="📋", layout="wide")\n    st.title("クリニック営業マスター")\n    st.caption("①データ準備 → ②Google Maps → ③HP調査 → ④営業対象・Comdesk出力")\n\n    with st.sidebar:\n        nav = st.radio("メニュー", NAV, key="navigation")\n        with st.expander("開発・確認用", expanded=False):\n            demo = st.toggle("サンプルモード", key="sample_mode")\n        st.caption("普段は「かんたん操作」を上から順に進めればOKです。")\n\n    path = Path(os.getenv("CLINIC_DEMO_DB_PATH", str(ROOT/"data/demo.sqlite3"))) if demo else Path(\n        os.getenv("CLINIC_DB_PATH", str(ROOT/"data/clinics.sqlite3"))\n    )\n    store = store_for(str(path))\n    store.refresh_age_model()\n\n    if demo:\n        load_demo(store)\n        st.info("架空サンプル4医院を表示しています。実データとは別に保存されます。")\n\n    if nav == "かんたん操作":\n        simple_workflow_ui(store, demo)\n    elif nav == "営業対象・出力":\n        simple_sales_ui(store)\n    else:\n        advanced_ui(store, demo)\n'

try:
    # メニューだけ簡素化
    text, n_nav = re.subn(
        r'^NAV\s*=\s*\[[^\n]*\]',
        'NAV = ["かんたん操作","営業対象・出力","詳細設定"]',
        text,
        count=1,
        flags=re.M,
    )
    if n_nav != 1:
        raise RuntimeError("NAV の場所を特定できませんでした。")

    # 既に適用済みなら関数を二重追加しない
    if "def simple_workflow_ui(store, demo):" not in text:
        marker = "\ndef research_ui(store,demo):"
        if marker not in text:
            marker = "\ndef research_ui(store, demo):"
        if marker not in text:
            raise RuntimeError("自動情報収集画面の場所を特定できませんでした。")
        text = text.replace(marker, "\n\n" + NEW_FUNCTIONS + marker, 1)

    # main() だけ置き換え。既存のバックエンド処理・v24判定ロジックは触らない。
    m = re.search(r'(?ms)^def main\(\):.*?(?=\n\nif __name__==["\']__main__["\']:) ', text)
    if m is None:
        # 行末空白なし版
        m = re.search(r'(?ms)^def main\(\):.*?(?=\n\nif __name__==["\']__main__["\']:)'
                      , text)
    if m is None:
        raise RuntimeError("main() の場所を特定できませんでした。")
    text = text[:m.start()] + NEW_MAIN + text[m.end():]

    APP.write_text(text, encoding="utf-8")
    py_compile.compile(str(APP), doraise=True)

except Exception as e:
    shutil.copy2(backup / "app_v2.py", APP)
    print("適用に失敗したため元に戻しました。")
    print("エラー:", repr(e))
    raise SystemExit(1)

print("修正しました。")
print("・通常画面を『かんたん操作 / 営業対象・出力 / 詳細設定』の3つだけにしました。")
print("・かんたん操作は 1.基本データ → 2.Google Maps → 3.HP調査 → 4.Comdesk出力 の順です。")
print("・HP調査前に『現在対象何件 / 今回実際に何件調査するか』を表示します。")
print("・50件と入力して対象34件なら、開始前から『実際に34件』と表示します。")
print("・一時停止 / 調査リセットはHP調査のすぐ下に置きました。")
print("・Tavily、月間上限、細かいフィルター等は通常画面から隠しました。")
print("・既存の詳細機能は『詳細設定』に残しています。")
print("・SQLite、UUID、Maps結果、v24の判定ロジック、Comdeskデータは変更していません。")
print("バックアップ:", backup.name)
