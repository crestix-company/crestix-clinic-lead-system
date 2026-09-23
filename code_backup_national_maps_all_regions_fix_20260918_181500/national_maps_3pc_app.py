from __future__ import annotations

from datetime import datetime
from pathlib import Path
import hashlib
import json

import pandas as pd
import streamlit as st

from src.master.national_maps_queue import (
    OFFICIAL_SOURCES, PREFECTURES, build_package, collect_all_official,
    load_completed_maps_ids_readonly, sha256_file, source_registry_frame,
)

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "clinics.sqlite3"
STAGING = ROOT / "national_maps_staging"
OUTPUT = ROOT / "national_maps_output"

st.set_page_config(page_title="全国Maps 3台収集キュー", page_icon="🗺️", layout="wide")
st.title("全国 Google Maps 3台収集キュー作成")
st.caption("HP内容調査とは分離して、全国の厚生局データ → Maps Collector v5.7.0用CSVを3台分に均等分割します。")

st.info("この画面は clinics.sqlite3 を更新しません。既にGoogle Maps調査が完了している医院を除外するため、DBを読み取り専用で参照するだけです。")

if not DB.exists():
    st.error(f"DBが見つかりません: {DB}")
    st.stop()

version = (ROOT / "VERSION_RESEARCH").read_text(encoding="utf-8").strip() if (ROOT / "VERSION_RESEARCH").exists() else "不明"
db_hash = sha256_file(DB)
col1, col2, col3 = st.columns(3)
col1.metric("HP調査ロジック", version)
col2.metric("公式ソース", f"{len(OFFICIAL_SOURCES)}ファイル")
col3.metric("対象範囲", "47都道府県")
st.code(f"SQLite SHA256（開始時）: {db_hash}", language=None)

with st.expander("今回使う厚生局の公式ソースを確認"):
    st.dataframe(source_registry_frame(), use_container_width=True, hide_index=True)
    st.caption("基準日は2026-09-01。公式サイト側のURLが変わった場合は、自動取得を停止してエラーを表示します。")

st.subheader("1. 全国データを取得して3台分のキューを作る")
st.write("病院・名称に「センター」を含む施設・休止/廃止/辞退/取消を除外し、既にMaps調査完了済みの医院も除外します。ERROR/AMBIGUOUSは再試行対象として残します。")
force = st.checkbox("公式ファイルを再ダウンロードする（通常はOFF）", value=False)

if "national_maps_manual_payloads" not in st.session_state:
    st.session_state["national_maps_manual_payloads"] = {}
with st.expander("自動取得に失敗した地域だけ手動ファイルで補う（通常は使いません）"):
    source_by_label = {f"{s.bureau}｜{s.label}": s for s in OFFICIAL_SOURCES}
    selected_label = st.selectbox("失敗したソース", list(source_by_label))
    selected_source = source_by_label[selected_label]
    uploaded = st.file_uploader(
        f"公式ページから取得した {selected_source.kind.upper()} を選択",
        type=["zip", "xlsx"], key=f"manual_{selected_source.source_id}",
    )
    c1, c2 = st.columns(2)
    if c1.button("この公式ファイルを一時登録", use_container_width=True, disabled=uploaded is None):
        st.session_state["national_maps_manual_payloads"][selected_source.source_id] = uploaded.getvalue()
        st.success(f"{selected_source.label} を手動ファイルで補います。")
    if c2.button("手動登録をすべて解除", use_container_width=True):
        st.session_state["national_maps_manual_payloads"] = {}
        st.rerun()
    registered = sorted(st.session_state["national_maps_manual_payloads"].keys())
    st.caption("手動登録済み: " + (", ".join(registered) if registered else "なし"))
    st.caption("手動ファイルも解析・都道府県カバレッジ検証を通らなければ停止します。DBには書き込みません。")

if st.button("全国47都道府県 → 3台分を作成", type="primary", use_container_width=True):
    before_hash = sha256_file(DB)
    progress = st.progress(0.0, text="準備中")
    log = st.empty()

    def update(done, total, message):
        progress.progress(done / total if total else 0.0, text=message)
        log.caption(message)

    try:
        cache = STAGING / "downloads" / "20260901"
        frame, reports = collect_all_official(
            cache, progress=update, force=force,
            manual_payloads=st.session_state.get("national_maps_manual_payloads", {}),
        )
        completed_ids, db_info = load_completed_maps_ids_readonly(DB)
        after_read_hash = sha256_file(DB)
        if before_hash != after_read_hash:
            raise RuntimeError("DB SHA256が変化しました。安全のため出力を停止しました。")
        package, manifest, files = build_package(frame, completed_ids, reports, db_info)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        outdir = OUTPUT / stamp
        outdir.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (outdir / name).write_bytes(data)
        zip_path = OUTPUT / f"national_maps_3pc_{stamp}.zip"
        zip_path.write_bytes(package)
        final_hash = sha256_file(DB)
        if before_hash != final_hash:
            raise RuntimeError("出力後にDB SHA256が変化しました。結果を使用せず停止してください。")
        st.session_state["national_maps_package"] = package
        st.session_state["national_maps_manifest"] = manifest
        st.session_state["national_maps_zip_name"] = zip_path.name
        st.session_state["national_maps_outdir"] = str(outdir)
        st.session_state["national_maps_db_hash"] = final_hash
        progress.progress(1.0, text="全国3台分の作成完了")
        st.success("全国3台分のMaps収集キューを作成しました。SQLite DBは変更されていません。")
    except Exception as exc:
        progress.empty()
        st.error(f"作成を停止しました: {exc}")
        st.warning("一部地域だけで黙って続行はしません。エラー画面をそのままChatGPTへ送ってください。")

manifest = st.session_state.get("national_maps_manifest")
if manifest:
    st.subheader("2. 3台へ配布")
    a, b, c, d = st.columns(4)
    a.metric("全対象", f"{manifest['target_rows']:,}件")
    b.metric("PC1", f"{manifest['pc_counts']['pc1']:,}件")
    c.metric("PC2", f"{manifest['pc_counts']['pc2']:,}件")
    d.metric("PC3", f"{manifest['pc_counts']['pc3']:,}件")
    st.write(f"保存先: `{st.session_state['national_maps_outdir']}`")
    st.write(f"SQLite SHA256（完了時）: `{st.session_state['national_maps_db_hash']}`")
    st.download_button(
        "3台分まとめてZIPをダウンロード",
        data=st.session_state["national_maps_package"],
        file_name=st.session_state["national_maps_zip_name"],
        mime="application/zip",
        type="primary",
        use_container_width=True,
    )
    st.caption("ZIP内の maps_queue_pc1.csv / pc2.csv / pc3.csv を、それぞれ別PCのGoogle Maps Clinic Collector v5.7.0へ渡してください。")
    pref = pd.DataFrame(list(manifest["per_prefecture_target"].items()), columns=["都道府県", "Maps収集対象件数"])
    st.dataframe(pref, use_container_width=True, hide_index=True)
    with st.expander("除外件数・安全確認"):
        st.json({
            "filter_counts": manifest["filter_counts"],
            "existing_db_readonly": manifest["existing_db_readonly"],
            "database_writes": manifest["database_writes"],
        })

st.subheader("3. 3台で収集した後")
st.write("各PCのCollector結果CSVは別名で保存し、メインPCへ戻してください。厚生局マスター → Maps結果の順で統合し、その後にHP内容調査を行います。3台から同じSQLiteを直接触る運用はしません。")
