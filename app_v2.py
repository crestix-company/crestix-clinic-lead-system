"""第一営業部向けローカルアプリ。importだけでは画面/DB/ネットを動かさない。"""
from pathlib import Path
from dataclasses import asdict, replace
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import pandas as pd
import streamlit as st
from src.utils.config import ROOT,read_config
from src.utils.date_utils import today_japan,within_years
from src.io.input_loader import load_table,sheet_names
from src.master.comdesk import COMDESK_HEADERS, COMDESK_EXPORT_HEADERS, infer_comdesk_columns, new_export_row
from src.master.clinic_identity import project_export_rows
from src.master.fixed_export import fixed_row
from src.io.output_writer import csv_bytes
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
from src.master.jobs import JobRunner,job_status,recent_jobs
from src.master.hp_auto_run import AutoHpRunner
from src.master.hp_auto_state import controller_lease_health
from src.repository.write_backend import write_repositories_for
from src.cloud_run.hp_executor import (
    hp_executor_mode, is_cloud_run_executor, max_single_job_count,
    allow_unlimited_auto_run, dispatch_hp_job,
)
from src.repository.connection_diagnostics import (
    connection_error_category, is_database_connection_exception, safe_connection_message,
)
from src.hp_human_review_ui import (hp_human_review_card, hp_human_review_page, hp_human_review_sidebar, hp_human_review_dialog)
from src.email_human_review_ui import (email_human_review_card, email_human_review_page, email_human_review_sidebar, email_human_review_dialog)
from src.email_research_ui import (
    email_research_page, email_clinic_status_panel, email_auto_research_card,
    email_research_sidebar_status,
)
from src.dental_sales_tags_ui import dental_sales_tags_page
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

NAV = ["かんたん操作","営業対象・出力","Email調査","メール送付文章","詳細設定"]
READ_ONLY_UI_ENV_VAR = "CLINIC_READ_ONLY_UI"
_STORE_CACHE_SALT = secrets.token_bytes(32)
HP_LABELS = {"UNRESEARCHED":"未調査","VERIFIED":"HP確認済み","REVIEW":"要確認","NOT_FOUND":"HP未発見","ERROR":"取得エラー"}
CONTRACT_LABELS = {"UNKNOWN":"不明","PAID":"課金済み","FREE":"無課金"}
READ_ONLY_EXCLUDED_MARKETING_SIGNALS = frozenset({"EPARK課金済み確認"})
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
def runtime_store_cache_key():
    """Key the process-wide Store cache to backend/credential changes without exposing them."""
    values = [
        os.environ.get("CLINIC_DATA_BACKEND", ""),
        os.environ.get("CLINIC_WRITE_BACKEND", ""),
        os.environ.get("SUPABASE_RUNTIME_DB_URL", ""),
        os.environ.get("CLINIC_COMDESK_PREVIEW_ONLY", ""),
    ]
    payload = json.dumps(values, separators=(",", ":")).encode("utf-8")
    return hmac.new(_STORE_CACHE_SALT, payload, hashlib.sha256).hexdigest()


def read_only_ui_mode():
    return os.environ.get(READ_ONLY_UI_ENV_VAR, "").strip().casefold() in {"1", "true", "yes", "on"}


def comdesk_preview_only_mode():
    from src.safety.preview_mode import comdesk_preview_only_enabled
    return comdesk_preview_only_enabled()


@st.cache_resource(max_entries=2)
def comdesk_preview_repository_for(runtime_config_key):
    # runtime_config_key is a process-local HMAC, never the URL itself.
    from src.repository.comdesk_preview import build_comdesk_preview_repository
    return build_comdesk_preview_repository()


def render_comdesk_preview_only(repository):
    """Single-purpose UI: file upload + read-only plan; no apply/review controls."""
    from src.safety.preview_mode import comdesk_preview_only_enabled
    if not comdesk_preview_only_enabled():
        raise RuntimeError("Comdesk Preview専用モードが有効ではありません。")
    from src.master.comdesk import COMDESK_EXPORT_HEADERS
    from src.master.comdesk_sync import FIELD_HEADERS, plan_sync

    st.header("Comdesk Step7 — Preview専用")
    st.warning("READ ONLYです。差分確認のみ実行できます。Apply・保存・Job・Email/Gmail・HP処理は無効です。")
    st.caption("アップロードしたCSVはメモリ上で照合し、DBには保存しません。URLは表示のみで外部サイトを開きません。")
    upload = st.file_uploader("Comdesk 30列CSV", type=["csv"], key="comdesk_preview_only_upload")
    if upload is None:
        return

    import hashlib
    from src.io.input_loader import load_table
    raw = upload.getvalue()
    upload_digest = hashlib.sha256(raw).hexdigest()
    table = load_table(raw, upload.name)
    expected = set(COMDESK_EXPORT_HEADERS)
    if len(table.headers) != 30 or len(set(table.headers)) != 30 or set(table.headers) != expected:
        st.error("列名・列数がComdeskの30列出力と一致しません。Previewを中止しました。")
        return

    if st.button("UUID照合して差分Preview", key="comdesk_preview_only_run", type="primary"):
        try:
            plan = plan_sync(table, repository)
            st.session_state["comdesk_preview_only_plan"] = plan
            st.session_state["comdesk_preview_only_digest"] = upload_digest
        except Exception:
            st.error("Previewを作成できませんでした。接続権限とCSV形式を確認してください。")
            return

    plan = st.session_state.get("comdesk_preview_only_plan")
    if not plan or st.session_state.get("comdesk_preview_only_digest") != upload_digest:
        return

    counts = plan["counts"]
    st.write(
        f"CSV行数：{len(plan['rows']):,} / UUID照合行："
        f"{sum(row['clinic_id'] is not None for row in plan['rows']):,} / "
        f"更新候補：{counts['AUTO_UPDATE']:,} / 変更なし：{counts['NO_CHANGE']:,} / HOLD：{counts['HOLD']:,}"
    )
    labels = {
        "hp_rank": "HPランク", "hp_url": "公式HP URL", "email": "メール候補",
        "regular_holiday": "休診日", "business_days": "診療日", "morning_start": "午前始",
        "morning_end": "午前終", "afternoon_start": "午後始", "afternoon_end": "午後終",
        "phone": "電話番号", "address": "住所", "clinic_name": "医院名",
    }
    output = []
    for row in plan["rows"]:
        if not row["changes"]:
            output.append({
                "CSV行": row["row_number"], "Clinic ID": row["clinic_id"],
                "Comdesk UUID": row["comdesk_uuid"], "判定": row["status"],
                "項目": "", "変更前": "", "変更後": "", "HOLD理由": row["reason"],
            })
        for field, change in row["changes"].items():
            output.append({
                "CSV行": row["row_number"], "Clinic ID": row["clinic_id"],
                "Comdesk UUID": row["comdesk_uuid"], "判定": row["status"],
                "項目": labels.get(field, FIELD_HEADERS.get(field, field)),
                "変更前": change["before"], "変更後": change["after"],
                "HOLD理由": row["reason"] if row["status"] == "HOLD" else "",
            })
    st.dataframe(pd.DataFrame(output), hide_index=True, use_container_width=True)
    st.info("このモードにApply操作はありません。反映は別途承認された通常Runtimeでのみ行います。")


@st.cache_resource(max_entries=2)
def store_for(path=None, runtime_config_key=None):
    from src.repository.backend import active_backend, BACKEND_SUPABASE
    if active_backend() == BACKEND_SUPABASE:
        from src.repository.runtime_store import SupabaseRuntimeStore
        return SupabaseRuntimeStore()
    store = ClinicStore(path)
    # Stage4-B Shadow Read: default is off (CLINIC_SHADOW_READ_ENABLED unset/0), in which case
    # this returns the exact same ClinicStore as before -- no wrapper, no behavior change.
    from src.repository.shadow import shadow_read_enabled
    if shadow_read_enabled():
        from src.repository.shadow import ShadowClinicStore
        return ShadowClinicStore(store)
    return store


def show_web_research_metrics(store, *, read_only=False, compact=False):
    """通常UIのWebサイト調査状況SSOT。HPの公式性は断定しない。"""
    try:
        if hasattr(store, "web_research_metrics"):
            counts = (store.web_research_metrics(read_only_ui=True)
                      if read_only and getattr(store, "is_supabase_runtime", False)
                      else store.web_research_metrics())
        else:
            counts = web_research_metrics(store.path)
    except BatchMetricInvariantError as exc:
        st.error(f"Webサイト調査件数を表示できません：{exc}")
        return
    if counts is None:
        st.warning("HP research batch sidecarがないため、Webサイト調査状況を表示できません。")
        return
    if not compact:
        definitions = [
            ("WebサイトURL取得済み", "url_acquired", "医院データにWebサイトURLが登録されている医院数です。"),
            ("Webサイト調査完了", "researched", "公式HPの内容確認まで完了したクリニック数です。"),
            ("Webサイト調査失敗", "failed", "Webサイトの取得または解析を正常完了できなかった医院数です。"),
            ("Webサイト未調査", "not_researched", "非アクティブ医院も含む、WebサイトURL登録済み・自動調査未完了の参考件数です。Step 4の「HP調査可能・未調査」とは分母が異なります。"),
            ("治療カテゴリ検出あり", "treatment_detected", "Webサイトから対象の治療・検査・施術カテゴリが1種類以上確認された医院数です。診療科の件数ではありません。"),
            ("治療カテゴリ検出なし", "treatment_not_detected", "Webサイト調査は完了していますが、現在定義している治療・検査・施術カテゴリが確認されなかった医院数です。"),
        ]
        for start in range(0, len(definitions), 3):
            columns = st.columns(3)
            for column, (label, key, help_text) in zip(columns, definitions[start:start + 3]):
                column.metric(label, f"{counts[key]:,}件", help=help_text)
        return
    primary = [
        ("WebサイトURL取得済み", "url_acquired", "医院データにHP URLまたはMaps WebサイトURLが登録されています。"),
        ("Webサイト調査完了", "researched", "fetch_status=OK。取得と公式HPの検証が完了した件数です。"),
        ("厚生局全件", None, "厚生局から取り込んだ元レコード数です。ユニーク医院数とは限りません。"),
        ("既存UUIDあり", None, "現在の医院マスターでComdesk UUIDが登録されている医院数です。"),
    ]
    metrics = store.metrics() if compact else {}
    columns = st.columns(4)
    for column, (label, key, help_text) in zip(columns, primary):
        value = counts[key] if key else metrics.get("厚生局取込レコード" if label == "厚生局全件" else "既存UUIDあり", 0)
        with column.container(border=True):
            st.metric(label, f"{value:,}件", help=help_text)
    details = [
        ("URL取得済み", "url_acquired"), ("調査完了", "researched"),
        ("取得・解析失敗", "failed"), ("URLあり・未調査", "not_researched"),
        ("治療カテゴリ検出あり", "treatment_detected"),
        ("治療カテゴリ検出なし", "treatment_not_detected"),
    ]
    if compact:
        with st.expander("現在のデータ状況：集計定義と補助指標", expanded=False):
            st.caption("HP指標の母集団はHP URLまたはMaps URL登録済みの医院です。厚生局全件は取込元レコード数、UUIDは医院マスター数で、それぞれ別の母集団です。")
            columns = st.columns(3)
            for column, (label, key) in zip(columns, details):
                column.metric(label, f"{counts[key]:,}件")
            quality_done = treatment_quality_completed_count(counts)
            st.metric(
                "治療カテゴリ判定完了", f"{quality_done:,}件",
                help="fetch_status=OKのうち、検出あり・検出なしのどちらかに分類できた医院数です。",
            )
            st.caption("治療カテゴリの判定完了は検出あり＋検出なしで算出します。診療科目そのものの品質確認状態は既存集計に独立した状態列がなく、別件数にはできません。")
            show_email_research_metrics(store)
    else:
        for start in range(0, len(details), 3):
            columns = st.columns(3)
            for column, (label, key) in zip(columns, details[start:start + 3]):
                column.metric(label, f"{counts[key]:,}件")


def show_email_research_metrics(store):
    """Compact operational Email KPIs; values are intentionally distinct from raw candidates."""
    from src.repository.email_database import email_repositories_for_ui, email_database_ui_status

    repositories = email_repositories_for_ui()
    labels = [
        ("Email取得済み", "email_obtained", "営業利用できるメールアドレスが確認できたクリニック数です。採用専用・予約専用などのメールは含みません。"),
        ("Email確認待ち", "review_clinics", "AIだけでは利用可否を確定できなかったメールがあります。メール確認画面から判定してください。Clinic単位の件数です。"),
        ("Email未調査", "not_researched", "まだメールアドレスの自動調査を行っていないクリニック数です。HP確認済み・公式URLありの対象だけを数えます。"),
        ("Email調査済み", "email_researched", "Email調査が完了し、Emailあり・確認待ち・Emailなしのいずれかになったクリニック数です。エラーは含みません。"),
    ]
    repo = repositories.email_research if repositories is not None else None
    human_repo = repositories.email_human_review if repositories is not None else None
    if repo is None or not repo.available():
        columns = st.columns(4)
        for column, (label, _, definition) in zip(columns, labels):
            column.metric(label, "未接続", help=definition)
        st.caption(email_database_ui_status().message)
        return
    summary = repo.summary()
    human_summary = human_repo.summary() if human_repo is not None and human_repo.available() else {}
    values = {
        "email_obtained": summary.get("email_obtained", summary.get("primary", 0)),
        "review_clinics": human_summary.get("unreviewed_clinics", human_summary.get("unreviewed", 0)),
        "not_researched": summary.get("not_researched", summary.get("remaining", 0)),
        "email_researched": summary.get("email_researched", 0),
    }
    columns = st.columns(4)
    for column, (label, key, definition) in zip(columns, labels):
        column.metric(label, f"{values[key]:,}件", help=definition)


AUTO_HP_PROGRESS_REFRESH_SECONDS = 5
AUTO_HP_ACTIVE_STATUSES = {"RUNNING", "WAITING_NETWORK"}


def treatment_quality_completed_count(web_counts):
    """Count existing treatment-category classifications, including no-match results."""
    return int(web_counts["treatment_detected"]) + int(web_counts["treatment_not_detected"])


def _auto_hp_progress_snapshot(store, auto_repo):
    """Read one consistent-enough Auto HP progress snapshot for display only."""
    state = auto_repo.get_state() if auto_repo is not None else {"status": "IDLE"}
    auto_current = None
    if state.get("current_job_id"):
        try:
            auto_current = job_status(store, state["current_job_id"])
        except ValueError:
            auto_current = None

    current_done = auto_current["counts"].get("DONE", 0) if auto_current else 0
    current_pending = auto_current["counts"].get("PENDING", 0) if auto_current else 0
    current_running = auto_current["counts"].get("RUNNING", 0) if auto_current else 0
    current_total = auto_current["total"] if auto_current else 0

    cumulative = auto_repo.cumulative_results(state.get("run_id", "")) if auto_repo is not None and state.get("run_id") else {}
    initial_count = int(state.get("initial_count", 0) or 0)
    # 「処理済み」はこのRunに紐づくDONE(terminal result)の直接集計のみを根拠にする。
    # 開始時対象からの引き算にすると、開始後に新たにeligibleになった医院が
    # remaining_count()経由で混入して処理済みが0にクランプされてしまう。
    processed = sum(cumulative.values())

    # Auto Runは固定コホートではない: hp_auto_run_repository.advance()はBatch完了の
    # たびに_available_count(..., auto_run_id)を再実行し、その時点で新たにeligibleに
    # なった医院も同じRunの次Batchへ取り込む（candidate selectionはここでは変更しない）。
    # そのため「残り」はinitial_countからの差分ではなく、現在Jobの未完了分＋
    # 次Batch以降で実際に取り込まれ得る候補数（future_remaining）で表す。
    current_remaining = current_pending + current_running
    future_remaining = (
        auto_repo.remaining_count(state) if auto_repo is not None and state.get("run_id") else 0
    )
    total_remaining = current_remaining + future_remaining
    # このRunが現時点で対象にしている総数（processed + 未処理）。
    # 新規eligibleが増えれば分母も増えるのがdynamic Auto Runの仕様通りの挙動。
    current_scope_total = processed + total_remaining

    batch_size = max(1, int(state.get("batch_size", 500) or 500))
    current_batch = int(state.get("current_batch", 0) or 0)
    # Batch推定もinitial_count基準の固定値ではなく、現在Batchへfuture_remaining分の
    # 残Batch数を足した「現在推定」にする。future_remainingは現在BatchのPENDING/
    # RUNNINGを含まないため、current_batch自体に足し込めばよい。
    estimated_total = (
        current_batch + (future_remaining + batch_size - 1) // batch_size if current_batch else 0
    )

    return {
        "state": state,
        "controller_health": controller_lease_health(state),
        "current": auto_current,
        "initial_count": initial_count,
        "processed": processed,
        "total_remaining": total_remaining,
        "current_scope_total": current_scope_total,
        "future_remaining": future_remaining,
        "current_done": current_done,
        "current_pending": current_pending,
        "current_running": current_running,
        "current_total": current_total,
        "cumulative": cumulative,
        "estimated_total": estimated_total,
        "last_updated": (
            (auto_current or {}).get("updated_at")
            or state.get("last_progress_at")
            or ""
        ),
    }


def _render_auto_hp_progress(snapshot, *, live=False):
    state = snapshot["state"]
    labels = {
        "RUNNING": "実行中", "PAUSED": "一時停止", "WAITING_NETWORK": "通信回復待ち",
        "COMPLETED": "完了", "ERROR": "エラー", "CANCELLED": "終了済み",
    }
    frozen_types = "・".join(state.get("medical_types") or ["両方"])
    frozen_pref = state.get("prefecture") or "すべて"
    status_label = labels.get(state.get("status"), state.get("status", "不明"))
    if snapshot.get("controller_health") == "RECOVERABLE":
        status_label = "復旧待ち（controller lease期限切れ）"
    st.write(f"自動HP調査：{status_label}")
    st.caption(
        f"固定条件：都道府県={frozen_pref}／医科・歯科={frozen_types}／"
        f"再調査={'する' if state.get('force') else 'しない'}／"
        f"1Batch={int(state.get('batch_size', 500) or 500):,}件"
        + (f"／進捗は{AUTO_HP_PROGRESS_REFRESH_SECONDS}秒ごとに自動更新" if live else "")
    )

    initial_count = snapshot["initial_count"]
    processed = snapshot["processed"]
    total_remaining = snapshot["total_remaining"]
    current_scope_total = snapshot["current_scope_total"]
    overall_ratio = (
        min(1.0, max(0.0, processed / current_scope_total)) if current_scope_total else 0.0
    )
    overall_pct = overall_ratio * 100
    current_total = snapshot["current_total"]
    current_done = snapshot["current_done"]
    if current_total:
        batch_ratio = min(1.0, max(0.0, current_done / current_total))
        batch_pct = batch_ratio * 100
    result = snapshot["cumulative"]
    metrics_cols = st.columns(6)
    headline_metrics = [
        ("開始時対象", f"{initial_count:,}件"),
        ("処理済み", f"{processed:,}件"),
        ("現在残り候補", f"{total_remaining:,}件"),
        ("HP取得成功", f"{result.get('SUCCESS', 0):,}件"),
        ("要確認", f"{result.get('REVIEW', 0):,}件"),
        ("エラー", f"{result.get('ERROR', 0):,}件"),
    ]
    for column, (label, value) in zip(metrics_cols, headline_metrics):
        with column.container(border=True):
            st.metric(label, value)
    st.caption(
        f"全体進捗 {processed:,}/{current_scope_total:,}件（{overall_pct:.1f}%）"
        + (f"／現在Batch {current_done:,}/{current_total:,}件（{batch_pct:.1f}%、処理中 {snapshot['current_running']:,}件）" if current_total else "")
    )

    current = snapshot["current"]
    if current:
        st.caption(
            f"現在Job ID：{state.get('current_job_id')}／"
            f"現在Batch：{current_done:,} / {current_total:,}／"
            f"PENDING {snapshot['current_pending']:,}／RUNNING {snapshot['current_running']:,}／"
            f"完了Batch：{int(state.get('completed_batches', 0) or 0):,}"
        )

    cumulative = snapshot["cumulative"]
    future_remaining = snapshot.get("future_remaining", 0)
    with st.expander("進捗の補足（未発見・Batch・Runの定義）", expanded=False):
        detail_cols = st.columns(4)
        detail_cols[0].metric("未発見", f"{cumulative.get('NOT_FOUND', 0):,}件", help="候補URLを発見できなかった結果です。エラーには含めません。")
        detail_cols[1].metric("現在Batch", f"{current_done:,}/{current_total:,}件")
        detail_cols[2].metric("Batch番号", f"{int(state.get('current_batch', 0) or 0)} / 推定{snapshot['estimated_total']}")
        detail_cols[3].metric("次Batch以降の候補", f"{future_remaining:,}件")
        st.caption("HP取得成功はこのRunのDONE結果のうち公式HP URL取得・検証が通った件数です。診療科目・治療カテゴリの品質判定数とは区別し、治療カテゴリ判定完了は現在の全体集計に表示します。")

    network = "WAITING" if state.get("status") == "WAITING_NETWORK" else "ONLINE"
    updated = snapshot.get("last_updated") or "未取得"
    st.caption(f"通信状態：{network}／最終DB更新：{updated}")


@st.fragment(run_every=f"{AUTO_HP_PROGRESS_REFRESH_SECONDS}s")
def _live_auto_hp_progress(store):
    """Refresh only the Auto HP progress panel while a background run is active."""
    auto_repo = write_repositories_for(store).auto_hp if getattr(store, "is_supabase_runtime", False) else None
    if auto_repo is None:
        st.warning("Auto HP Researchの進捗を取得できません。")
        return
    snapshot = _auto_hp_progress_snapshot(store, auto_repo)
    _render_auto_hp_progress(snapshot, live=True)
    if snapshot["state"].get("status") not in AUTO_HP_ACTIVE_STATUSES:
        # Refresh the whole page once when the run transitions to PAUSED/COMPLETED/ERROR.
        st.rerun()


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


@st.cache_resource
def auto_hp_runner_for(path):
    return AutoHpRunner()


def _create_cloud_hp_job(prefecture, limit, force=False, medical_types=None):
    """CLINIC_HP_EXECUTOR=cloud_run専用：HP Job制御専用のDB資格情報でJobを作る。

    Webの一般WRITE経路（write_repositories_for/CLINIC_WRITE_BACKEND、Comdesk取込・
    age model更新等が使う）とは別の接続（src.cloud_run.job_control、
    SUPABASE_JOB_CONTROL_DB_URL未設定時はfail-closedで停止し、一般DB接続へ
    フォールバックしない。
    """
    from src.cloud_run.job_control import job_control_write_repositories
    return job_control_write_repositories().auto_hp.create_single_job(prefecture, medical_types, force, limit)


def _pause_cloud_hp_job(jid):
    """CLINIC_HP_EXECUTOR=cloud_run専用：Job制御専用のDB資格情報でpauseする。"""
    from src.cloud_run.job_control import job_control_write_repositories
    job_control_write_repositories().jobs.pause_job(jid)


def _dispatch_cloud_hp_job(jid, clinic_count=0):
    """CLINIC_HP_EXECUTOR=cloud_run専用：既存Job IDに対してCloud Run Executionを1つ起動する。

    結果（成功／起動結果不明／予算超過／ブロック／拒否）はsession_stateに保存し、
    次のrerunでStep5セクションの先頭に表示する。DISPATCH_UNKNOWNの場合は絶対に
    自動再試行しない（照合＝reconcile_hp_job_dispatchを人間に促す）。
    """
    from src.cloud_run.hp_executor import (
        BLOCKED_PENDING_RECONCILE, BUDGET_EXCEEDED, CloudRunNotConfiguredError, dispatch_hp_job,
    )
    from src.cloud_run.job_dispatch import DISPATCHED, DISPATCH_UNKNOWN

    try:
        result = dispatch_hp_job(jid, clinic_count=clinic_count)
    except CloudRunNotConfiguredError as exc:
        st.session_state["cloud_hp_dispatch_message"] = ("error", str(exc))
        return
    if result.status == DISPATCHED:
        st.session_state["cloud_hp_dispatch_message"] = (
            "success", f"Cloud Run Jobを起動しました（Execution: {result.execution_name}）。"
        )
    elif result.status == DISPATCH_UNKNOWN:
        st.session_state["cloud_hp_dispatch_message"] = (
            "warning",
            "Cloud Run Jobの起動結果が不明です。下の「起動結果を照合」を実行して確認してください。"
            "自動的な再実行は行いません。",
        )
    elif result.status == BLOCKED_PENDING_RECONCILE:
        st.session_state["cloud_hp_dispatch_message"] = ("warning", result.detail)
    elif result.status == BUDGET_EXCEEDED:
        st.session_state["cloud_hp_dispatch_message"] = ("error", result.detail)
    else:
        st.session_state["cloud_hp_dispatch_message"] = (
            "error", "Cloud Run Jobの起動が拒否されました。設定を確認してください。"
        )


def _reconcile_cloud_hp_job(jid):
    """CLINIC_HP_EXECUTOR=cloud_run専用：DISPATCH_UNKNOWNのOperationを照合する。

    新しいExecutionは絶対に起動しない。結果はsession_stateに保存して表示する。
    """
    from src.cloud_run.dispatch_state import STATUS_CONFIRMED_RUNNING, STATUS_SAFE_TO_RETRY
    from src.cloud_run.hp_executor import CloudRunNotConfiguredError, reconcile_hp_job_dispatch

    try:
        state = reconcile_hp_job_dispatch(jid)
    except CloudRunNotConfiguredError as exc:
        st.session_state["cloud_hp_dispatch_message"] = ("error", str(exc))
        return
    if state is None:
        st.session_state["cloud_hp_dispatch_message"] = ("info", "照合対象の未確認Executionはありません。")
    elif state.get("status") == STATUS_CONFIRMED_RUNNING:
        st.session_state["cloud_hp_dispatch_message"] = (
            "success", "Executionは実行中であることを確認しました。再発行は不要です。"
        )
    elif state.get("status") == STATUS_SAFE_TO_RETRY:
        st.session_state["cloud_hp_dispatch_message"] = (
            "success", "ExecutionはOperationレベルで失敗していたことを確認しました。再発行できます。"
        )
    else:
        st.session_state["cloud_hp_dispatch_message"] = (
            "warning", "まだ起動結果を確認できません。しばらくしてから再度照合してください。"
        )


def ad_count_options(store, *, read_only=False):
    # 施策数の選択肢は実データの最大施策数から作る（固定の大きな数字は並べない）。
    from src.scoring.research_scoring import AD_SIGNAL_NAMES
    ad_signal_names = [name for name in AD_SIGNAL_NAMES
                       if not read_only or name not in READ_ONLY_EXCLUDED_MARKETING_SIGNALS]
    if getattr(store, "is_supabase_runtime", False):
        top = store.ad_count_max(read_only_ui=True) if read_only else store.ad_count_max()
    else:
        with store.connect() as c:
            ad_count_sql = AD_COUNT_SQL
            if read_only:
                names = ",".join("'" + name.replace("'", "''") + "'" for name in ad_signal_names)
                ad_count_sql = "(SELECT count(DISTINCT value) FROM json_each(signals_json) WHERE value IN ("+names+"))"
            top = c.execute("SELECT MAX("+ad_count_sql+") FROM clinics").fetchone()[0] or 0
    return [0]+list(range(2,top+1))


def ad_count_label(n):
    return "指定なし" if n==0 else f"{n}施策以上"


def filters_ui(store,prefix="sales",defaults=None, *, read_only=False):
    defaults = defaults or Filters()
    if getattr(store, "is_supabase_runtime", False):
        prefs, municipality_options = store.prefectures(), store.municipalities()
    else:
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
        if not getattr(store, "is_supabase_runtime", False) and mhlw_sidecar_available():
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
        if getattr(store, "is_supabase_runtime", False):
            hp_treatment_options = store.treatment_category_options()
            hp_treatments = st.multiselect("HP治療カテゴリ（Treatment Research・CONFIRMEDのみ）",hp_treatment_options,
                default=[x for x in defaults.hp_treatment_categories if x in hp_treatment_options],key=key("hp_treatment_categories"),
                help="公式HP上でCONFIRMED（提供確認済み）の治療カテゴリのみ営業対象にします。")
        elif research_sidecar_available():
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
        if getattr(store, "is_supabase_runtime", False) or clinic_research_status_available():
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
    signal_options = [name for name in SIGNAL_NAMES
                      if not read_only or name not in READ_ONLY_EXCLUDED_MARKETING_SIGNALS]
    signals = st.multiselect("集客投資シグナル（いずれか）",signal_options,
                             default=[name for name in defaults.signals if name in signal_options],key=key("signals"))
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
    if getattr(store, "is_supabase_runtime", False):
        status = store.mhlw_join_status()
        if status is None:
            st.info("ナビイ正式診療科データは移行対象外です。既存の厚生局診療科filterは利用できます。")
            return
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


def _email_status_label(email_status):
    if not email_status:
        return "未調査"
    if email_status.get("has_primary"):
        return "Primary"
    status = email_status.get("research_status")
    if status == "REVIEW":
        return "要確認"
    if status in {"NO_EMAIL_FOUND", "ERROR"}:
        return "取得なし"
    return "未調査"


def display_rows(records, *, include_email_status=True, include_age_probability=True, read_only=False):
    rows = []
    for r in records:
        prob = r.get("age_probability_under_59")
        row = {"管理番号":r["id"],"UUID":r.get("uuid",""),"医院名":r.get("clinic_name",""),"電話番号":r.get("phone",""),
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
            "EPARK URL":r.get("epark_url","")}
        if not read_only:
            row["EPARK契約"] = CONTRACT_LABELS.get(r.get("epark_contract"),"不明")
        row.update({
            "治療カテゴリ":" / ".join(r.get("website_treatment_categories") or r.get("treatment_categories",[])),
            "治療カテゴリ状態":TREATMENT_STATUS_DISPLAY_LABELS.get(r.get("treatment_status"),"不明"),
            "集客投資シグナル数":r.get("marketing_signal_count",0),
            "アツさ":r.get("hot_status","通常"),"シグナル一覧":" / ".join(
                s["name"] for s in r.get("marketing_signals",[])
                if not read_only or s.get("name") not in READ_ONLY_EXCLUDED_MARKETING_SIGNALS)})
        if include_email_status:
            row["Email Status"] = _email_status_label(r.get("email_status"))
        if not include_age_probability:
            row.pop("59歳以下確率", None)
        rows.append(row)
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
    email_clinic_status_panel(store, cid)
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
            write_repositories_for(store).research.override(cid,field,value,note=note)
            st.success("手動値を保存しました。再調査しても保持されます。")
            st.rerun()
    if field in r.get("manual_fields",[]) and cols[1].button("この項目を自動値に戻す",key=f"manual_reset_{cid}"):
        write_repositories_for(store).research.override(cid,field,None,note=note)
        st.rerun()


def listing(store,filters,prefix="list", *, read_only=False):
    count = (store.count_read_only_ui(filters)
             if read_only and hasattr(store, "count_read_only_ui") else store.count(filters))
    st.write(f"対象：{count:,}件")
    size = st.selectbox("1ページの表示件数",[25,50,100,200],index=1,key=prefix+"_size")
    pages = max(1,(count+size-1)//size)
    if st.session_state.get(prefix+"_page",1)>pages:
        st.session_state[prefix+"_page"] = 1
    number = st.number_input("ページ",1,pages,1,key=prefix+"_page")
    records = (store.query_read_only_ui(filters,limit=size,offset=(number-1)*size)
               if read_only and hasattr(store, "query_read_only_ui")
               else store.query(filters,limit=size,offset=(number-1)*size))
    if records:
        statuses = store.treatment_status_for_ids([r["id"] for r in records])
        for r in records:
            r["treatment_status"] = statuses.get(r["id"])
        if not read_only:
            from src.repository.email_database import email_repositories_for_ui
            email_repos = email_repositories_for_ui()
            email_repo = email_repos.email_research if email_repos is not None else None
            email_statuses = email_repo.status_for_ids([r["id"] for r in records]) if email_repo is not None and email_repo.available() else {}
            for r in records:
                r["email_status"] = email_statuses.get(r["id"])
        column_config = {"Maps HP":st.column_config.LinkColumn(),"HP URL":st.column_config.LinkColumn(),"EPARK URL":st.column_config.LinkColumn()}
        if not read_only:
            column_config["59歳以下確率"] = st.column_config.NumberColumn(format="percent")
        st.dataframe(display_rows(records, include_email_status=not read_only,
                                  include_age_probability=not read_only, read_only=read_only),
                     hide_index=True, width="stretch", column_config=column_config)
        if not read_only:
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
            st.caption("照合プレビュー（基本28項目。HPランク・メールアドレスは照合キーに使用しません）")
            st.dataframe(pd.DataFrame([fixed_row({},table.headers,mapping,row) for row in table.data.head(5).values.tolist()],columns=COMDESK_HEADERS),hide_index=True,width="stretch")
        if st.button("既存案件を登録",type="primary"):
            with st.spinner("既存案件を登録しています…"):
                result = write_repositories_for(store).clinics.import_comdesk(table,mapping)
            report_import(result)
    st.subheader("2. 厚生局データを統合する")
    if st.button("同梱の東京都マスターを取り込む",disabled=demo):
        with st.spinner("東京都の全施設を統合しています…"):
            frame = pd.read_csv(ROOT/"data/master/tokyo_current.csv",dtype=str,keep_default_na=False)
            result = write_repositories_for(store).clinics.import_master(frame)
        report_import(result)
    st.caption("同梱データは2026-09-01基準の東京都医科一覧です。病院・休止施設も保持し、営業対象フィルターで現存クリニックを選びます。歯科一覧は別途取り込んでください。")
    st.write("取り込み済みのデータを再統合する")
    st.caption("先頭0・ハイフンの違いを統合判定用のキーで吸収します。元のTel1・UUIDは保持します。全レコードを保存し、曖昧な重複は確認待ちとして残します。")
    result_key = "reintegration_result_" + str(store.path)
    if not getattr(store, "is_supabase_runtime", False) and st.button("保存済みデータを再統合",disabled=demo):
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
                result = write_repositories_for(store).clinics.import_master(frame)
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
                result = write_repositories_for(store).provenance.import_maps_results(maps_frame)
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
                        write_repositories_for(store).research.save_research(record["id"],{**age,"license_source":result.source})
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
    # Step4 eligibility is owned by the Supabase HP research ledger. A local clinic
    # projection cannot answer whether a clinic has ever been researched.
    if not getattr(store, "is_supabase_runtime", False):
        raise RuntimeError("Step4 HP target selection requires the Supabase research ledger.")
    # Candidate reservation and job creation share one PostgreSQL transaction so a
    # simultaneous Mac/Windows start cannot enqueue the same clinic twice.
    return write_repositories_for(store).auto_hp.create_single_job(
        prefecture, medical_types, force, limit
    )


def _maps_hp_available_count(store, prefecture="", force=False, medical_types=None):
    # Do not guess research completion from public.clinics.hp_status or local sidecars.
    if not getattr(store, "is_supabase_runtime", False):
        return 0
    return store.maps_hp_available_count(prefecture, medical_types, force)


def current_hp_job_export_ui(store, *, key_prefix="step5"):
    """Show current-job metrics while keeping UUID-empty successful HP results cumulative."""
    if not getattr(store, "is_supabase_runtime", False):
        st.metric("Comdesk出力対象", "0件")
        st.info("今回のHP調査ジョブはSupabase実行時に表示されます。全期間の出力は詳細設定をご利用ください。")
        return
    job = store.latest_completed_hp_job()
    if not job:
        st.metric("Comdesk出力対象", "0件")
        st.info("完了したHP調査ジョブがありません。過去のHP確認済み医院を代わりに出力することはありません。")
        return
    summary = store.hp_job_export_summary(job["id"])
    # A running Streamlit process may still hold the pre-PR42 runtime_store module
    # after a git pull/hot reload. Keep the UI usable until the process is restarted.
    stale_runtime = "carryover_count" not in summary
    carryover_count = summary.get("carryover_count", 0)
    st.caption(f"今回のHP調査結果（ジョブ {job['id']}）")
    if stale_runtime:
        st.warning(
            "HP調査の実行プロセスが更新前コードを保持しています。"
            "調査完了後にアプリを再起動すると、前回分を含むComdesk出力対象へ切り替わります。"
            "この状態では誤出力防止のためComdeskファイル出力を停止しています。"
        )
    cols = st.columns(5)
    cols[0].metric("調査対象", f"{job['target_count']:,}件")
    cols[1].metric("調査完了", f"{summary['done_count']:,}件")
    cols[2].metric("HP確認成功", f"{summary['success_count']:,}件")
    cols[3].metric("既存UUIDあり", f"{summary['uuid_existing_count']:,}件")
    cols[4].metric(
        "Comdesk出力対象",
        f"{len(summary['export_ids']):,}件",
        help=(
            "HP調査成功済み・UUID未付与の累積件数です。"
            f"今回ジョブ以外から {carryover_count:,}件を引き継いでいます。"
            "CSVをダウンロードしても消えず、UUIDが付与されると自動で対象外になります。"
        ),
    )
    signature = json.dumps(
        [str(store.path), str(job["id"]), COMDESK_EXPORT_HEADERS, store.revision(),
         len(summary["export_ids"]), carryover_count],
        ensure_ascii=False,
        sort_keys=True,
    )
    if st.button(
        "今回のHP調査結果をComdesk形式で出力",
        type="primary",
        key=key_prefix + "_current_hp_export",
        disabled=stale_runtime or not summary["export_ids"],
        use_container_width=True,
    ):
        st.session_state[key_prefix + "_current_hp_export_files"] = {
            "signature": signature,
            "files": store.export_hp_job(job["id"]),
        }
    output = st.session_state.get(key_prefix + "_current_hp_export_files")
    if output and output["signature"] == signature:
        for name, content in output["files"].items():
            st.download_button(
                "Excelをダウンロード" if name.endswith("xlsx") else "CSVをダウンロード",
                content,
                name,
                key=key_prefix + "_download_" + name,
                use_container_width=True,
            )


def cloud_hp_job_controls_ui(store, *, read_only=False):
    """Narrow Cloud Run controls for the existing bounded HP Job executor."""
    from src.cloud_run.deployment_context import is_cloud_run_deployment
    from src.cloud_run.feature_flags import cloud_write_feature_enabled
    from src.cloud_run.operator_identity import operator_authorized_for_cloud_write, operator_email_from_headers

    if read_only or not is_cloud_run_deployment():
        return
    with st.expander("HP自動調査（Cloud Run・承認制）", expanded=False):
        if not cloud_write_feature_enabled("CLINIC_CLOUD_HP_JOB_CONTROL_ENABLED"):
            st.caption("CLINIC_CLOUD_HP_JOB_CONTROL_ENABLED=1 が未設定のため無効です。")
            return
        email = operator_email_from_headers(getattr(st.context, "headers", None))
        if not operator_authorized_for_cloud_write(email):
            st.error("IAPログインに加えて、CLINIC_CLOUD_WRITE_OPERATORSで許可されたOperator権限が必要です。")
            return
        if hp_executor_mode() != "cloud_run":
            st.error("CLINIC_HP_EXECUTOR=cloud_runが必要です。ローカル実行へフォールバックしません。")
            return

        prefs = store.prefectures() if getattr(store, "is_supabase_runtime", False) else []
        pref_options = ["すべて"] + list(prefs)
        pref_label = st.selectbox("都道府県", pref_options, key="cloud_hp_prefecture")
        pref = "" if pref_label == "すべて" else pref_label
        med_label = st.selectbox("医科・歯科", ["医科", "歯科", "両方"], key="cloud_hp_medical_type")
        medical_types = ["医科", "歯科"] if med_label == "両方" else [med_label]
        count = _maps_hp_available_count(store, pref, False, medical_types)
        st.caption(f"実行対象候補：{count:,}件／1 Job最大50件。Cloud Runでは常時Auto Runを提供しません。")
        jobs = [j for j in recent_jobs(store) if j.get("kind") == "hp"]
        active_job = next((j for j in jobs if j.get("status") in {"RUNNING", "PAUSED", "BUDGET"}), None)
        selected_job = active_job or (jobs[0] if jobs else None)
        current = job_status(store, selected_job["id"]) if selected_job else None
        active = bool(current and current.get("status") in {"RUNNING", "PAUSED", "BUDGET"})
        limit = min(max_single_job_count("cloud_run"), count) if count else 0
        requested = st.number_input("今回の最大件数", min_value=1, max_value=50, value=max(1, min(25, limit or 25)), key="cloud_hp_count")
        if st.button("Cloud Run HP Jobを開始", key="cloud_hp_start", disabled=active or count == 0):
            from src.cloud_run.hp_executor import precheck_budget
            actual = min(int(requested), count)
            allowed, reason = precheck_budget(actual)
            if not allowed:
                st.error(reason)
            else:
                job_id = _create_cloud_hp_job(pref, actual, False, medical_types)
                _dispatch_cloud_hp_job(job_id, clinic_count=actual)
                st.rerun()
        if current:
            st.caption(f"最新Job：{current['id']}／状態：{current['status']}")
            dispatch_state = None
            if current["status"] in {"PAUSED", "BUDGET"}:
                from src.cloud_run.hp_executor import get_dispatch_state
                from src.cloud_run.dispatch_state import STATUS_DISPATCH_UNKNOWN
                try:
                    dispatch_state = get_dispatch_state(current["id"])
                except Exception as exc:
                    st.error(f"起動状態を確認できないため再開を止めました：{type(exc).__name__}")
                if dispatch_state is None:
                    st.error("起動状態が取得できないため再開できません。復旧担当者に確認してください。")
                elif dispatch_state.get("status") == STATUS_DISPATCH_UNKNOWN:
                    st.warning("前回の起動結果が未確認です。再開前に照合してください。")
                    if st.button("起動結果を照合", key="cloud_hp_reconcile"):
                        _reconcile_cloud_hp_job(current["id"])
                        st.rerun()
                elif st.button("このHP Jobを再開", key="cloud_hp_resume", disabled=active and current["status"] == "RUNNING"):
                    _dispatch_cloud_hp_job(current["id"], clinic_count=0)
                    st.rerun()
            if current["status"] == "RUNNING" and st.button("このHP Jobを一時停止", key="cloud_hp_pause"):
                _pause_cloud_hp_job(current["id"])
                st.rerun()
            show_job_progress(store, current["id"])


def cloud_email_human_review_ui(store, *, read_only=False):
    """Operator-gated Cloud Run review using the existing Email review DB role."""
    from src.cloud_run.deployment_context import is_cloud_run_deployment
    from src.cloud_run.feature_flags import cloud_write_feature_enabled
    from src.cloud_run.operator_identity import operator_authorized_for_cloud_write, operator_email_from_headers

    if read_only or not is_cloud_run_deployment():
        return
    with st.expander("Email Human Review（Cloud Run・承認制）", expanded=False):
        if not cloud_write_feature_enabled("CLINIC_CLOUD_EMAIL_HUMAN_REVIEW_ENABLED"):
            st.caption("CLINIC_CLOUD_EMAIL_HUMAN_REVIEW_ENABLED=1 が未設定のため無効です。")
            return
        email = operator_email_from_headers(getattr(st.context, "headers", None))
        if not operator_authorized_for_cloud_write(email):
            st.error("IAPログインに加えて、CLINIC_CLOUD_WRITE_OPERATORSで許可されたOperator権限が必要です。")
            return
        email_human_review_card(store)
        if st.session_state.pop("_open_email_human_review_dialog", False):
            email_human_review_dialog(store)


def simple_workflow_ui(store, demo, *, read_only=False):
    if read_only:
        st.header("クリニック営業マスター")
        st.caption("READ ONLYで医院マスターとHP調査状況を確認します。営業対象の検索・CSV/Excel出力は「営業対象・出力」をご利用ください。")
        filters = Filters(active_only=False, hp_only=False)
        total = store.count_read_only_ui(filters)
        with_uuid = store.count_read_only_ui(replace(filters, uuid_mode="あり"))
        st.subheader("現在の医院データ")
        columns = st.columns(2)
        columns[0].metric(
            "医院マスター（営業対象外施設を除く）", f"{total:,}件",
            help="病院・センター等の営業対象外施設を除いた、現在の絞り込み条件（フィルタなし）での母集団です。DB全体の行数とは分母が異なります。",
        )
        columns[1].metric(
            "既存UUIDあり", f"{with_uuid:,}件",
            help="左の母集団のうちUUIDが紐付いている件数です。「既存営業リスト」（全国展開前のレガシーコホート）とは別の定義・別の件数です。",
        )
        show_web_research_metrics(store, read_only=True)
        with st.expander("ローカル版の処理順序（参考・READ ONLYでは操作不可）", expanded=False):
            st.caption("①データ準備（Comdesk・厚生局取込） → ②Google Maps収集・取込 → ③HP・Email自動調査 → ④営業対象検索・Comdesk出力")
            st.caption("①〜③はデータ更新を伴うため、READ ONLY版では状態の表示のみで操作ボタンは提供していません。④は「営業対象・出力」メニューから閲覧・検索・CSV/Excel出力のみ利用できます。")
        st.info("取込・Email状況・Job状態・Human Review・Step 6 Comdesk同期はREAD ONLY初回公開では表示しません。")
        return

    # Review fix (Issue #130 PR #131 follow-up): CLINIC_READ_ONLY_UI=false on a
    # Cloud Run deployment previously meant "identical write access to Mac" for
    # every surface below (legacy Comdesk/厚生局/Maps import, HP Job dispatch,
    # Step6/7 Comdesk sync/reimport) -- none of which were ever individually
    # reviewed for multi-operator IAP-authenticated Cloud access (no operator
    # identity, no preview/confirm, no dedicated writer connection, unlike
    # cloud_comdesk_import_ui() or src.cloud_run.job_control.py's own HP Job
    # role separation). is_cloud_run_deployment() locks this whole legacy
    # surface on ANY Cloud Run deployment regardless of CLINIC_READ_ONLY_UI --
    # only a deliberately-built, individually-flagged Cloud path (like
    # cloud_comdesk_import_ui, which does NOT use write_disabled) can run
    # there. On Mac (no K_SERVICE) this changes nothing.
    from src.cloud_run.deployment_context import is_cloud_run_deployment
    write_disabled = demo or read_only or is_cloud_run_deployment()
    metrics = store.metrics()

    st.header("上から順に進めるだけ")
    st.caption("Comdesk・厚生局・Google Maps・HP/Email調査を順番に進めます。")

    st.subheader("Step1：既存Comdesk CSV・Excelを取り込む")
    import_ui_comdesk_step(store, write_disabled)

    st.subheader("現在のデータ状況")
    show_web_research_metrics(store, compact=True)

    if metrics.get("厚生局取込レコード", 0) == 0:
        st.warning("厚生局データがまだありません。最初に東京都マスターを取り込んでください。")
        if st.button("東京都マスターを取り込む", type="primary", disabled=write_disabled, key="simple_import_tokyo", use_container_width=True):
            with st.spinner("東京都マスターを取り込んでいます…"):
                frame = pd.read_csv(ROOT/"data/master/tokyo_current.csv", dtype=str, keep_default_na=False)
                result = write_repositories_for(store).clinics.import_master(frame)
            report_import(result)
            st.rerun()

    st.subheader("Step2：厚生局データを取り込む")
    cloud_kouseikyoku_import_ui(store)

    st.subheader("Step3：Google Maps Collector")
    # Independent, separately-gated Cloud Run management screen for the
    # EXISTING, human-operated Collector (Chrome extension v5.7.0, unchanged
    # by this work) -- see cloud_maps_collection_admin_ui()'s own docstring
    # for the explicit scope boundary (task/assignment state only, never
    # scripting or automating the collection itself).
    cloud_maps_collection_admin_ui(store, key_prefix="simple_maps_collection")

    st.subheader("Step4：Google Maps結果を取り込む")

    maps_upload = st.file_uploader(
        "③ Google Maps収集結果CSVをアップロード",
        type=["csv"],
        key="simple_maps_results",
        disabled=write_disabled,
    )
    if maps_upload:
        maps_table = load_table(maps_upload.getvalue(), maps_upload.name)
        maps_frame = maps_table.data.copy()
        maps_frame.columns = maps_table.headers

        if st.button("Google Maps結果を取り込む", type="primary", key="simple_maps_import", disabled=write_disabled, use_container_width=True):
            with st.spinner("Google Maps結果を取り込んでいます…"):
                result = write_repositories_for(store).provenance.import_maps_results(maps_frame)
            st.success(
                f"取込 {result.get('TOTAL',0):,}件／HP取得 {result.get('WEBSITE',0):,}件／"
                f"HPなし {result.get('NO_WEBSITE',0):,}件／要確認 {result.get('AMBIGUOUS',0):,}件／"
                f"エラー {result.get('ERROR',0):,}件"
            )
            st.rerun()
    # Independent, separately-gated Cloud Run Writer panel -- see
    # cloud_maps_import_ui()'s own docstring. Must render regardless of
    # whether the legacy uploader above has a file selected, same lesson as
    # cloud_comdesk_import_ui() (Issue #130 PR #131 review fix).
    cloud_maps_import_ui(store, key_prefix="simple_maps")

    st.subheader("Step5：HP・Email自動調査")
    if getattr(store, "is_supabase_runtime", False):
        prefs = store.prefectures()
    else:
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
    st.info(
        f"HP調査可能・未調査：{available:,}件",
        icon="ℹ️",
    )
    st.caption("active=true・未統合・Google MapsでWebサイトURL取得済み・未調査（強制再調査時を除く）で、未完了jobにも入っていない医院です。上の参考件数とは分母が異なります。")

    hp_mode = hp_executor_mode()
    cloud_run_executor = hp_mode == "cloud_run"
    mode_options = ["指定件数だけ調査"]
    if allow_unlimited_auto_run(hp_mode):
        mode_options.append("未調査がなくなるまで自動調査")
    mode = st.radio(
        "調査モード",
        mode_options,
        index=0,
        key="simple_research_mode",
    )
    if cloud_run_executor:
        st.caption("Cloud Run実行モード：単発調査（最大50件）のみ。常時稼働の自動調査はローカル環境専用です。")
    dispatch_message = st.session_state.pop("cloud_hp_dispatch_message", None)
    if dispatch_message:
        level, text = dispatch_message
        getattr(st, level)(text)
    runner = runner_for(str(store.path))
    auto_runner = auto_hp_runner_for(str(store.path))
    auto_repo = write_repositories_for(store).auto_hp if getattr(store, "is_supabase_runtime", False) else None
    auto_state = auto_repo.get_state() if auto_repo is not None else {"status": "IDLE"}
    auto_active = auto_state.get("status") in {"RUNNING", "WAITING_NETWORK", "PAUSED", "ERROR"}
    if (not read_only and not is_cloud_run_deployment() and auto_state.get("status") in {"RUNNING", "WAITING_NETWORK"}
            and not auto_runner.running() and not cloud_run_executor):
        # Any PC may offer an executor, but the persisted lease allows only one controller.
        # Cloud Run Web never self-elects as that executor: it must stay request-driven
        # (min 0 / max 1), not an always-on background crawler charged to this service.
        # is_cloud_run_deployment() is required here (not just not_cloud_run_executor,
        # which only excludes the dedicated CLINIC_HP_EXECUTOR=cloud_run deployment):
        # this call has no disable button, so it must also stay off for a Cloud Run
        # Web deployment with CLINIC_READ_ONLY_UI=false and no CLINIC_HP_EXECUTOR set.
        auto_runner.start(store)

    hp_jobs = [j for j in recent_jobs(store) if j.get("kind") == "hp"]
    def is_auto_child(job):
        options = job.get("options_json") or {}
        if isinstance(options, str):
            try:
                options = json.loads(options)
            except (TypeError, ValueError):
                options = {}
        return bool(options.get("auto_run_id")) if isinstance(options, dict) else False
    single_jobs = [job for job in hp_jobs if not is_auto_child(job)]
    current = job_status(store, single_jobs[0]["id"]) if single_jobs else None
    unfinished = bool(current and current["status"] in {"RUNNING", "PAUSED", "BUDGET"})

    if mode == "指定件数だけ調査":
        count_cap = max_single_job_count(hp_mode)
        default_count = min(50, available) if available > 0 else 50
        count = st.number_input(
            "最大調査件数", min_value=1, max_value=count_cap,
            value=max(1, min(default_count, count_cap)), key="simple_count",
        )
        actual = min(int(count), available)
        st.caption(f"今回のウェブサイト調査件数：{actual:,}件。設定が50件でも対象が34件なら34件だけ調査します。")
        if st.button(
            "HP自動調査を開始", type="primary",
            disabled=write_disabled or runner.running() or unfinished or auto_active or actual == 0,
            key="simple_start", use_container_width=True,
        ):
            if cloud_run_executor:
                from src.cloud_run.hp_executor import precheck_budget
                # Budget is checked BEFORE a job row exists: a refusal must never
                # leave an orphaned, undispatchable research_jobs row behind.
                allowed, reason = precheck_budget(actual)
                if not allowed:
                    st.session_state["cloud_hp_dispatch_message"] = ("error", reason)
                else:
                    jid = _create_cloud_hp_job(pref, int(count), force, research_medical_types)
                    st.session_state["active_job"] = jid
                    _dispatch_cloud_hp_job(jid, clinic_count=actual)
            else:
                jid = _create_maps_hp_job(store, pref, int(count), force, research_medical_types)
                st.session_state["active_job"] = jid
                runner.start(store, jid, TavilySearchProvider(""))
            st.rerun()
    else:
        batch_size = st.number_input(
            "1Batch件数", min_value=1, max_value=500, value=500,
            key="simple_auto_batch_size",
        )
        estimated = (available + int(batch_size) - 1) // int(batch_size) if available else 0
        st.write(f"Auto Run対象：{available:,}件")
        st.caption(f"1Batch：{int(batch_size):,}件／予想Batch数：{estimated:,}")
        if st.button(
            "未調査がなくなるまで自動調査を開始", type="primary",
            disabled=write_disabled or auto_repo is None or auto_active or runner.running() or unfinished or available == 0,
            key="simple_auto_start", use_container_width=True,
        ):
            auto_repo.start(pref, research_medical_types, force, int(batch_size))
            auto_runner.start(store)
            st.rerun()

    if auto_state.get("status") != "IDLE":
        if auto_state.get("status") in AUTO_HP_ACTIVE_STATUSES:
            _live_auto_hp_progress(store)
        else:
            _render_auto_hp_progress(_auto_hp_progress_snapshot(store, auto_repo), live=False)

        auto_controls = st.columns(2)
        if auto_controls[0].button(
            "自動調査を一時停止", disabled=write_disabled or auto_state.get("status") not in {"RUNNING", "WAITING_NETWORK"},
            key="simple_auto_pause", use_container_width=True,
        ):
            auto_repo.pause()
            st.rerun()
        if auto_controls[1].button(
            "自動調査を再開", disabled=write_disabled or auto_state.get("status") not in {"PAUSED", "ERROR"},
            key="simple_auto_resume", use_container_width=True,
        ):
            auto_repo.resume()
            if not cloud_run_executor:
                auto_runner.start(store)
            st.rerun()

    if read_only:
        st.info("READ ONLY画面確認中のため、HP・Email JobおよびHuman Review操作は無効です。")
    elif is_cloud_run_deployment():
        # hp_human_review_card/email_human_review_card call write_repositories_for(store)
        # directly from their own button handlers with no is_cloud_run_deployment()
        # awareness at all -- they predate the Cloud Run write-path work entirely.
        # Gating them here (rather than inside each card) keeps this blanket lock
        # in one place, consistent with nav_locked/write_disabled above.
        st.info("Cloud Runでは既存の書込機能を無効化しています。承認済みOperator向けの専用Job・Review画面のみ個別フラグで利用できます。")
        cloud_hp_job_controls_ui(store, read_only=read_only)
        email_auto_research_card(store, demo=demo)
        cloud_email_human_review_ui(store, read_only=read_only)
    else:
        hp_human_review_card(store)
        email_human_review_card(store)
        email_auto_research_card(store, demo=demo)

    st.subheader("Step6：HP確認済み医院をComdesk同期用CSVで出力")
    st.caption("件数ごとに母集団を明記しています。URL・調査完了は全HP URL登録済み母集団、UUID・除外・同期対象は最新のHP成功医院母集団です。各除外件数は優先順で重複を除いています。")
    try:
        breakdown = comdesk_sync_breakdown(store, metrics)
        st.dataframe(pd.DataFrame(breakdown), hide_index=True, use_container_width=True)
    except Exception as exc:
        st.warning(f"Step6の内訳を確認できません：{exc}")
    render_comdesk_sync_export(store, demo)

    st.subheader("Step7：UUID付与後のComdesk CSVを再取り込む")
    st.caption("ComdeskでUUIDを付与したCSVをStep1と同じ既存取込処理で再取り込みできます。UUIDは既存値を上書きしません。")
    import_ui_comdesk_step(store, write_disabled, key_prefix="simple_comdesk_reimport", button_label="UUID付与後CSVを再取り込む")
    render_comdesk_clinic_sync(store, write_disabled)

    with st.expander("HP調査の詳細（通常は触らなくてOK）", expanded=False):
        st.write("Google MapsでHP取得済み医院だけを直接調査します。")
        st.write("最大HPページ数：20ページ")
        st.write("HP未取得医院の検索、EPARK、外部媒体の調査は「詳細設定」から行えます。")
        if auto_state.get("status") in {"RUNNING", "WAITING_NETWORK", "PAUSED", "ERROR"}:
            st.warning("完全終了しても完了済み調査結果は削除されません。現在BatchはPAUSEDで保持されます。")
            if st.button("自動調査を終了", key="simple_auto_cancel", disabled=write_disabled):
                auto_repo.cancel()
                st.rerun()

    st.caption(
        "営業対象・Comdesk出力は従来どおり「営業対象・出力」から利用できます。"
    )


def import_ui_comdesk_step(store, write_disabled, *, key_prefix="simple_comdesk_import", button_label="既存Comdesk案件を登録"):
    upload = st.file_uploader("Comdesk CSV・Excel", type=["csv", "xlsx", "xls"], key=key_prefix+"_upload", disabled=write_disabled)
    if upload:
        table = upload_table(upload, key_prefix)
        inferred = infer_comdesk_columns(table)
        labels = {"uuid":"UUID・案件ID", "clinic_name":"医院名", "phone":"電話番号（Tel1）", "prefecture":"都道府県",
                  "address":"住所・住所１", "address2":"住所２・建物名", "postal_code":"郵便番号",
                  "manager_name":"院長名", "url":"HP URL", "epark_url":"EPARK URL"}
        mapping = {}
        with st.expander("列の対応を確認", expanded=False):
            st.caption("既存の28列形式を含め、UUID・医院名・電話番号・住所を使ってClinic Masterと照合します。")
            for field, label in labels.items():
                choices = [None]+list(range(len(table.headers)))
                selected = inferred.get(field)
                mapping[field] = st.selectbox(label+"の列", choices, index=choices.index(selected),
                    format_func=lambda i: "未設定" if i is None else f"{i+1}列目：{table.headers[i]}", key=key_prefix+"_map_"+field)
            preview = table.data.head(5).copy()
            preview.columns = [f"{i+1}｜{v}" for i,v in enumerate(table.headers)]
            st.dataframe(preview, hide_index=True)
        if st.button(button_label, type="primary", key=key_prefix+"_submit", disabled=write_disabled, use_container_width=True):
            with st.spinner("Comdeskデータを照合・登録しています…"):
                result = write_repositories_for(store).clinics.import_comdesk(table, mapping)
            report_import(result)
    # These are independent, separately-gated panels -- they must render
    # regardless of whether the legacy uploader above has a file selected.
    # (Review fix: previously the `if not upload: return` above exited the
    # whole function before either panel was ever reached.)
    #
    # write_disabled (demo or read_only or is_cloud_run_deployment()) is the
    # correct gate for the legacy-backend snapshot-diff panel below, which
    # writes through the general write backend and was never built with
    # Cloud Run Writer semantics. It must NOT gate cloud_comdesk_import_ui:
    # that panel only ever renders when is_cloud_run_deployment() is True
    # (checked at its own entry), so gating it on write_disabled as well
    # would make it dead code -- its own flag/operator/writer-connection
    # checks are the real, independent gate for that path.
    render_comdesk_snapshot_diff_import(store, write_disabled, key_prefix=key_prefix)
    cloud_comdesk_import_ui(store, key_prefix=key_prefix)


def render_comdesk_clinic_sync(store, write_disabled):
    """Step7's separate preview/apply path for formal Comdesk return files."""
    import pandas as pd
    from src.master.comdesk_sync import plan_sync, apply_sync

    sync_labels = {
        "hp_rank": "HPランク", "hp_url": "公式ホームページURL", "email": "メールアドレス",
        "regular_holiday": "休診日", "business_days": "診療日", "morning_start": "午前始",
        "morning_end": "午前終", "afternoon_start": "午後始", "afternoon_end": "午後終",
        "phone": "電話番号", "address": "住所", "clinic_name": "医院名",
    }
    st.markdown("**医院情報の差分同期（30列形式）**")
    st.caption("CSV取込とDB反映を分けています。完全一致したUUIDだけを照合し、反映前に差分を確認できます。")
    upload = st.file_uploader("Comdesk 30列CSV", type=["csv"], key="step7_clinic_sync_upload", disabled=write_disabled)
    if not upload:
        return
    table = upload_table(upload, "step7_clinic_sync")
    if st.button("CSVを照合して差分プレビュー", key="step7_clinic_sync_preview", disabled=write_disabled):
        try:
            repo = write_repositories_for(store).clinics
            plan = plan_sync(table, repo)
            st.session_state["step7_clinic_sync_plan"] = plan
            st.session_state["step7_clinic_sync_name"] = upload.name
        except Exception as exc:
            st.error(f"差分プレビューを作成できません：{exc}")
    plan = st.session_state.get("step7_clinic_sync_plan")
    if not plan or st.session_state.get("step7_clinic_sync_name") != upload.name:
        return
    st.write(f"CSV取込件数：{len(plan['rows']):,}件 / UUID照合成功：{sum(r['clinic_id'] is not None for r in plan['rows']):,}件 / "
             f"更新候補：{plan['counts']['AUTO_UPDATE']:,}件 / 変更なし：{plan['counts']['NO_CHANGE']:,}件 / 確認待ち：{plan['counts']['HOLD']:,}件")
    st.dataframe(pd.DataFrame([{"項目": sync_labels.get(key, key), "変更件数": value} for key, value in plan["item_counts"].items()]), hide_index=True)
    preview_rows = []
    for row in plan["rows"]:
        for field, change in row["changes"].items():
            preview_rows.append({"CSV行": row["row_number"], "Clinic ID": row["clinic_id"], "Comdesk UUID": row["comdesk_uuid"],
                                 "判定": row["status"], "項目": sync_labels.get(field, field), "変更前": change["before"], "変更後": change["after"], "理由": row["reason"]})
        if row["status"] == "HOLD":
            preview_rows.append({"CSV行": row["row_number"], "Clinic ID": row["clinic_id"], "Comdesk UUID": row["comdesk_uuid"],
                                 "判定": "HOLD", "項目": "", "変更前": "", "変更後": "", "理由": row["reason"]})
    st.dataframe(pd.DataFrame(preview_rows), hide_index=True, use_container_width=True)
    reviewed_url_rows = []
    url_candidates = [row for row in plan["rows"] if "hp_url" in row["changes"] and row["status"] == "HOLD"]
    if url_candidates:
        st.markdown("**HP URLの人間確認**")
        st.caption("リンク先がこの医院の公式HPであることを確認した行だけチェックしてください。反映時にHP状態をVERIFIEDにし、既存HP調査履歴は保持します。")
        for row in url_candidates:
            change = row["changes"]["hp_url"]
            st.write(f"CSV {row['row_number']}行目 / Comdesk UUID {row['comdesk_uuid']} / 変更前: {change['before'] or '空欄'} / 変更後: {change['after']}")
            if str(change["after"]).lower().startswith(("https://", "http://")):
                st.link_button("変更後URLを開く", change["after"], key=f"step7_url_link_{row['row_number']}")
            if (str(change["after"]).lower().startswith(("https://", "http://"))
                    and st.checkbox("このURLが対象医院の公式ホームページであることを確認", key=f"step7_url_review_{row['row_number']}", disabled=write_disabled)):
                reviewed_url_rows.append(row["row_number"])
    auto_count = plan["counts"]["AUTO_UPDATE"]
    apply_enabled = bool(auto_count or reviewed_url_rows)
    if st.button("プレビューの更新候補をDBへ反映", type="primary", key="step7_clinic_sync_apply",
                 disabled=write_disabled or not apply_enabled):
        try:
            repository = write_repositories_for(store).clinics
            fresh_plan = plan_sync(table, repository)
            if fresh_plan != plan:
                raise ValueError("CSVまたは医院データがプレビュー後に変わりました。最新差分を再確認してください。")
            result = apply_sync(repository, fresh_plan, reviewed_url_rows=reviewed_url_rows)
            st.success(f"反映完了：{result.get('updated', 0):,}件")
            st.session_state.pop("step7_clinic_sync_plan", None)
        except Exception as exc:
            st.error(f"同期を反映できません：{exc}")


def render_comdesk_preview_only(repository):
    """Single-purpose UI: a 30-column CSV upload and read-only diff plan only."""
    from src.safety.preview_mode import comdesk_preview_only_enabled
    if not comdesk_preview_only_enabled():
        raise RuntimeError("Comdesk Preview専用モードが有効ではありません。")
    from src.master.comdesk import COMDESK_EXPORT_HEADERS
    from src.master.comdesk_sync import FIELD_HEADERS, plan_sync

    st.header("Comdesk Step7 — Preview専用")
    st.warning("READ ONLYです。差分確認のみ実行できます。Apply・保存・Job・Email/Gmail・HP処理は無効です。")
    st.caption("CSVはメモリ上で照合します。URLはテキスト表示のみで外部サイトを開きません。")
    upload = st.file_uploader("Comdesk 30列CSV", type=["csv"], key="comdesk_preview_only_upload")
    if upload is None:
        return

    import hashlib
    from src.io.input_loader import load_table
    raw = upload.getvalue()
    upload_digest = hashlib.sha256(raw).hexdigest()
    try:
        table = load_table(raw, upload.name)
    except Exception:
        st.error("CSVを読み込めませんでした。ファイル形式を確認してください。")
        return
    expected_headers = set(COMDESK_EXPORT_HEADERS)
    if (len(table.headers) != 30 or len(set(table.headers)) != 30
            or set(table.headers) != expected_headers):
        st.error("列名・列数がComdeskの30列出力と一致しません。Previewを中止しました。")
        return

    if st.button("UUID照合して差分Preview", key="comdesk_preview_only_run", type="primary"):
        try:
            plan = plan_sync(table, repository)
            st.session_state["comdesk_preview_only_plan"] = plan
            st.session_state["comdesk_preview_only_digest"] = upload_digest
        except Exception:
            st.error("Previewを作成できませんでした。Reader権限、RLS、CSV形式を確認してください。")
            return

    plan = st.session_state.get("comdesk_preview_only_plan")
    if not plan or st.session_state.get("comdesk_preview_only_digest") != upload_digest:
        return

    counts = plan["counts"]
    matched = sum(row["clinic_id"] is not None for row in plan["rows"])
    st.write(
        f"CSV行数：{len(plan['rows']):,} / UUID照合行：{matched:,} / "
        f"更新候補：{counts['AUTO_UPDATE']:,} / 変更なし：{counts['NO_CHANGE']:,} / HOLD：{counts['HOLD']:,}"
    )
    labels = {
        "hp_rank": "HPランク", "hp_url": "公式HP URL", "email": "メール候補",
        "regular_holiday": "休診日", "business_days": "診療日", "morning_start": "午前始",
        "morning_end": "午前終", "afternoon_start": "午後始", "afternoon_end": "午後終",
        "phone": "電話番号", "address": "住所", "clinic_name": "医院名",
    }
    output = []
    for row in plan["rows"]:
        if not row["changes"]:
            output.append({
                "CSV行": row["row_number"], "Clinic ID": row["clinic_id"],
                "Comdesk UUID": row["comdesk_uuid"], "判定": row["status"],
                "項目": "", "変更前": "", "変更後": "", "HOLD理由": row["reason"],
            })
        for field, change in row["changes"].items():
            output.append({
                "CSV行": row["row_number"], "Clinic ID": row["clinic_id"],
                "Comdesk UUID": row["comdesk_uuid"], "判定": row["status"],
                "項目": labels.get(field, FIELD_HEADERS.get(field, field)),
                "変更前": change["before"], "変更後": change["after"],
                "HOLD理由": row["reason"] if row["status"] == "HOLD" else "",
            })
    st.dataframe(pd.DataFrame(output), hide_index=True, use_container_width=True)
    st.info("このモードにApply操作はありません。反映は別途承認された通常Runtimeでのみ行います。")


def render_comdesk_snapshot_diff_import(store, demo, *, key_prefix):
    """Separate, opt-in path for the two 30-column CRM snapshot exports.

    The legacy one-file Comdesk import above remains untouched. This UI never
    shows source-row values and stays disabled unless explicitly enabled in a
    development process.
    """
    with st.expander("Comdesk営業状態・履歴の差分取込（2ファイル）", expanded=False):
        environment = (os.environ.get("CLINIC_ENVIRONMENT") or "").strip().casefold()
        enabled = (os.environ.get("CLINIC_COMDESK_SNAPSHOT_DIFF_ENABLED") or "").strip().casefold() in {
            "1", "true", "yes", "on",
        }
        development_db = (os.environ.get("CLINIC_COMDESK_SNAPSHOT_DEVELOPMENT_DB_NAME") or "").strip()
        if environment != "development" or not enabled or not development_db:
            st.caption("CLINIC_ENVIRONMENT=development、CLINIC_COMDESK_SNAPSHOT_DIFF_ENABLED=1、および開発DB名の明示が必要です。")
            return
        if demo:
            st.info("デモ・読み取り専用では差分取込を利用できません。")
            return
        repository = write_repositories_for(store).clinics
        if not hasattr(repository, "preview_comdesk_snapshot_pair"):
            st.warning("この書込バックエンドは対のComdeskスナップショット取込に未対応です。Supabase開発DBでMigration適用後に利用してください。")
            return
        st.caption("データCSVと医院情報CSVをUUIDで結合します。医院情報は画面に表示せず、医院ID/共通医院ID・UUIDの不一致や曖昧一致はHOLDします。")
        sales_upload = st.file_uploader("データ_YYYYMMDDHHMMSS.csv（営業状態・プロジェクト・履歴）",
                                        type=["csv"], key=key_prefix+"_snapshot_sales")
        clinic_upload = st.file_uploader("エクスポート_YYYYMMDDHHMMSS.csv（医院情報・UUID・履歴）",
                                         type=["csv"], key=key_prefix+"_snapshot_clinic")
        if not sales_upload or not clinic_upload:
            st.caption("2ファイルを選択するとREAD ONLYプレビューが利用できます。")
            return
        sales_bytes, clinic_bytes = sales_upload.getvalue(), clinic_upload.getvalue()
        fingerprint = hashlib.sha256(sales_bytes + b"\\0" + clinic_bytes).hexdigest()
        preview_key = key_prefix + "_snapshot_preview"
        if st.button("取込内容をREAD ONLY確認", key=key_prefix+"_snapshot_preview_button"):
            try:
                with st.spinner("CSV構造と医院IDを照合しています…"):
                    summary = repository.preview_comdesk_snapshot_pair(
                        sales_bytes, clinic_bytes, sales_upload.name, clinic_upload.name,
                    )
                st.session_state[preview_key] = {"fingerprint": fingerprint, "summary": summary}
            except Exception as exc:
                st.session_state.pop(preview_key, None)
                st.error(f"プレビューを停止しました: {exc}")
        preview = st.session_state.get(preview_key)
        if not preview or preview.get("fingerprint") != fingerprint:
            return
        summary = preview["summary"]
        metrics = st.columns(4)
        metrics[0].metric("新規スナップショット", f"{summary['insert']:,}件")
        metrics[1].metric("更新", f"{summary['update']:,}件")
        metrics[2].metric("変更なし", f"{summary['unchanged']:,}件")
        metrics[3].metric("HOLD", f"{summary['hold']:,}件")
        if summary.get("hold_reasons"):
            st.dataframe(pd.DataFrame([{"理由": reason, "件数": count}
                                       for reason, count in summary["hold_reasons"].items()]),
                         hide_index=True, use_container_width=True)
        st.warning("UUID・医院IDは既存値を変更しません。HOLD行は取込対象から除外し、履歴差分は通話イベントとして作成しません。")
        if st.button("プレビュー内容を再照合して差分取込", type="primary",
                     key=key_prefix+"_snapshot_apply",
                     disabled=summary["insert"] + summary["update"] + summary["unchanged"] == 0):
            try:
                with st.spinner("最新の医院ID・スナップショットと再照合して取り込んでいます…"):
                    result = repository.import_comdesk_snapshot_pair(
                        sales_bytes, clinic_bytes, sales_upload.name, clinic_upload.name,
                    )
                st.session_state.pop(preview_key, None)
                st.success(f"取込完了: 新規 {result['insert']}件、更新 {result['update']}件、変更なし {result['unchanged']}件、HOLD {result['hold']}件")
            except Exception as exc:
                st.error(f"取込をロールバックしました: {exc}")


def cloud_comdesk_import_ui(store, *, key_prefix):
    """Issue #130 P2-(1): Cloud Run Writer-deployment path for the existing
    single-file Comdesk import above (import_ui_comdesk_step/import_comdesk).

    Never renders under the current READ ONLY Canary (read_only_ui_mode() is
    True there) -- this only activates in a hypothetical future, separately-
    provisioned Cloud Run deployment with CLINIC_READ_ONLY_UI=false, gated by
    its own default-OFF flag AND is_cloud_run_deployment() (so a non-Cloud,
    non-Mac process, e.g. a test runner, is also excluded). On Mac/local this
    is a no-op, same behavior as before this panel existed.

    Deliberately takes no demo/write_disabled parameter: this panel's own
    is_cloud_run_deployment() + feature-flag + operator-authorization checks
    below are its complete, independent gate. (Review fix: an earlier draft
    received the caller's write_disabled -- always True on Cloud Run once the
    blanket legacy-surface lock was added -- through a parameter literally
    named `demo`, and returned early whenever that parameter was truthy,
    permanently blocking this panel regardless of flag/operator/writer
    configuration.)

    Reuses import_comdesk() business logic as-is (same UUID matching,
    provenance, and import-batch idempotency as the local path), but NOT the
    same connection: writes go through a dedicated, fail-closed Comdesk
    import writer role (src.cloud_run.comdesk_import_writer), never the
    general write backend every other local write path shares. Also adds
    what a multi-operator Cloud deployment needs beyond local Mac usage: an
    IAP-identified AND separately-authorized operator (allowlist-checked,
    not "any IAP login"), a read-only preview before any write, and a
      same-session double-submit guard (src.master.comdesk_cloud_import) --
    defense in depth on top of import_comdesk()'s own data-level UUID
    idempotency, which is what actually prevents duplication across
    sessions/restarts/multiple users (the session guard only avoids an
    unnecessary re-submit within one browser tab; it is not the safety
    boundary).

    KNOWN GAP, deliberately not built in this round: the preview below shows
    row/column counts and raw file content only, not a NEW/UPDATE/HOLD
    matching-outcome breakdown like src.master.comdesk_snapshot_import's
    preview_comdesk_snapshot_pair() provides. src.master.matching.match_record()
    is SQLite-specific (uses `?` placeholders against a sqlite3 connection)
    and is not safe to call against the Postgres/Supabase connection this
    path actually uses -- building a correct Postgres-compatible dry-run
    classifier is real new logic, not something to guess at under time
    pressure. Tracked as a follow-up, not silently skipped.
    """
    from src.cloud_run.comdesk_import_writer import (
        ComdeskImportWriterNotConfiguredError, comdesk_import_writer_repositories,
    )
    from src.cloud_run.deployment_context import is_cloud_run_deployment
    from src.cloud_run.feature_flags import cloud_write_feature_enabled
    from src.cloud_run.operator_identity import operator_authorized_for_cloud_write, operator_email_from_headers
    from src.master.comdesk_cloud_import import comdesk_import_idempotency_key, preview_comdesk_import

    if read_only_ui_mode() or not is_cloud_run_deployment():
        return
    with st.expander("Comdesk取込（Cloud Run Writer用・既定無効）", expanded=False):
        if not cloud_write_feature_enabled("CLINIC_CLOUD_COMDESK_IMPORT_ENABLED"):
            st.caption(
                "CLINIC_CLOUD_COMDESK_IMPORT_ENABLED=1 が未設定のため無効です。"
                "Mac通常モードでは上のStep1取込をそのままご利用ください。"
            )
            return
        operator_email = operator_email_from_headers(getattr(st.context, "headers", None))
        if not operator_authorized_for_cloud_write(operator_email):
            st.error(
                "IAPでログイン済みでも、Comdesk取込の業務権限がある操作者として"
                "CLINIC_CLOUD_WRITE_OPERATORSに登録されていないため無効です。"
                "ログイン自体は書込み権限を意味しません。"
            )
            return
        st.caption(f"操作者：{operator_email}")
        st.caption(
            "この操作は、このデプロイが接続しているデータベースへ実際に書き込みます"
            "（本番Supabaseに接続された状態で実行すれば本番データが更新されます）。"
            "プレビュー確認・チェック・ボタン押下まで何も書き込みません。"
            "同一内容の再送信は1セッション内でブロックします（詳細はコード内コメント参照）。"
        )
        upload = st.file_uploader(
            "Comdesk CSV・Excel", type=["csv", "xlsx", "xls"], key=key_prefix + "_cloud_upload",
        )
        if not upload:
            return
        table = upload_table(upload, key_prefix + "_cloud")
        mapping = infer_comdesk_columns(table)
        idempotency_key = comdesk_import_idempotency_key(upload.getvalue(), mapping)
        applied_key = st.session_state.get(key_prefix + "_cloud_applied_key")
        if applied_key == idempotency_key:
            st.success("この内容は既にこのセッションで取り込み済みです。別のファイルを選択してください。")
            return
        preview = preview_comdesk_import(table, mapping)
        st.write(f"対象：{preview['row_count']:,}行 / {preview['column_count']}列")
        st.caption("新規・更新・重複・HOLDの内訳は未実装です（Postgres対応の照合プレビューは別途実装予定）。")
        display_preview = preview["preview"].copy()
        display_preview.columns = [f"{i+1}｜{v}" for i, v in enumerate(table.headers)]
        st.dataframe(display_preview, hide_index=True)
        confirmed = st.checkbox("内容を確認しました", key=key_prefix + "_cloud_confirm_" + idempotency_key)
        if st.button(
            "確認のうえ取り込む", type="primary", disabled=not confirmed,
            key=key_prefix + "_cloud_submit",
        ):
            try:
                writer_repositories = comdesk_import_writer_repositories()
            except ComdeskImportWriterNotConfiguredError as exc:
                st.error(str(exc))
                return
            with st.spinner("Comdeskデータを照合・登録しています…"):
                result = writer_repositories.clinics.import_comdesk(table, mapping)
            st.session_state[key_prefix + "_cloud_applied_key"] = idempotency_key
            logging.info(
                "cloud_comdesk_import operator=%s idempotency_key=%s result=%s",
                operator_email, idempotency_key, result,
            )
            report_import(result)


def cloud_maps_import_ui(store, *, key_prefix):
    """Issue #130: Cloud Run Writer-deployment path for the existing Google
    Maps collection-result import above (import_maps_results()). Mirrors
    cloud_comdesk_import_ui() exactly for every gate (read_only/is_cloud_run_
    deployment entry guard, default-OFF flag, IAP-identified AND separately-
    authorized operator, dedicated fail-closed Writer connection, same-
    session double-submit guard, audit logging) -- see that function's
    docstring for the full reasoning, not repeated here. Takes no demo/
    write_disabled parameter for the same reason: this panel's own gates
    below are its complete, independent gate (Issue #130 PR #131 follow-up
    review lesson, applied here from the start).

    Reuses import_maps_results() business logic as-is on the dedicated
    Google Maps import writer connection (src.cloud_run.maps_import_writer),
    never the general write backend. Preview goes one step further than the
    Comdesk/厚生局 cloud panels' KNOWN GAP: the Google Maps matcher
    (SupabaseProvenanceWriteRepository._prefetch_maps_targets /
    _find_target_prefetched) is already Postgres-native, so
    src.master.maps_cloud_import.preview_maps_import() runs that exact
    matching logic -- plus the same classify_match_status()/build_maps_
    update() pure functions import_maps_results() itself calls -- read-only
    (SELECT-only, always rolled back even on a mid-way exception) to return
    the IDENTICAL counts shape (TOTAL/MATCHED/WEBSITE/NO_WEBSITE/NOT_FOUND/
    AMBIGUOUS/EXCLUDED/ERROR/UNLINKED/PRESERVED_WEBSITE) that
    import_maps_results()'s own return value uses after the real write.
    Review finding: an earlier draft showed a narrower, differently-defined
    set of buckets in the preview (its own "MATCHED" meant "an existing
    clinic was found", not the real result's "the collector found a Maps
    profile") -- reusing the identical shape for both removes that
    ambiguity by construction, so an operator reading preview then result
    is comparing the same fields, not two different definitions that happen
    to share a label. The preview is still labeled an estimate, not the
    confirmed result, because a concurrent write between preview and apply
    can change which clinics match; only import_maps_results()'s own return
    value after the real write is final.
    """
    from src.cloud_run.deployment_context import is_cloud_run_deployment
    from src.cloud_run.feature_flags import cloud_write_feature_enabled
    from src.cloud_run.maps_import_writer import (
        MapsImportWriterNotConfiguredError, maps_import_writer_repositories,
    )
    from src.cloud_run.operator_identity import operator_authorized_for_cloud_write, operator_email_from_headers
    from src.master.maps_cloud_import import maps_import_idempotency_key, preview_maps_import

    if read_only_ui_mode() or not is_cloud_run_deployment():
        return
    with st.expander("Google Maps結果取込（Cloud Run Writer用・既定無効）", expanded=False):
        if not cloud_write_feature_enabled("CLINIC_CLOUD_MAPS_IMPORT_ENABLED"):
            st.caption(
                "CLINIC_CLOUD_MAPS_IMPORT_ENABLED=1 が未設定のため無効です。"
                "Mac通常モードでは上のStep4取込をそのままご利用ください。"
            )
            return
        operator_email = operator_email_from_headers(getattr(st.context, "headers", None))
        if not operator_authorized_for_cloud_write(operator_email):
            st.error(
                "IAPでログイン済みでも、Google Maps取込の業務権限がある操作者として"
                "CLINIC_CLOUD_WRITE_OPERATORSに登録されていないため無効です。"
                "ログイン自体は書込み権限を意味しません。"
            )
            return
        st.caption(f"操作者：{operator_email}")
        st.caption(
            "この操作は、このデプロイが接続しているデータベースへ実際に書き込みます"
            "（本番Supabaseに接続された状態で実行すれば本番データが更新されます）。"
            "プレビューは照合ロジックによる推定であり確定結果ではありません。"
            "確認・チェック・ボタン押下まで何も書き込みません。"
            "同一内容の再送信は1セッション内でブロックします（詳細はコード内コメント参照）。"
        )
        upload = st.file_uploader(
            "Google Maps収集結果CSV", type=["csv"], key=key_prefix + "_cloud_maps_upload",
        )
        if not upload:
            return
        file_bytes = upload.getvalue()
        idempotency_key = maps_import_idempotency_key(file_bytes)
        applied_key = st.session_state.get(key_prefix + "_cloud_maps_applied_key")
        if applied_key == idempotency_key:
            st.success("この内容は既にこのセッションで取り込み済みです。別のファイルを選択してください。")
            return
        # load_table() already enforces a 50MB file-size cap and a 100,000-row
        # cap (see src.io.input_loader.load_table's defaults), raising
        # ValueError rather than reading an unbounded file into memory; this
        # cloud panel is multi-operator-facing (unlike the single-user local
        # Step4 uploader that calls the same function without a try/except),
        # so it must turn that into a readable error instead of an unhandled
        # Streamlit traceback.
        try:
            maps_table = load_table(file_bytes, upload.name)
        except ValueError as exc:
            st.error(f"CSVの形式・サイズ・行数を確認してください: {exc}")
            return
        maps_frame = maps_table.data.copy()
        maps_frame.columns = maps_table.headers
        try:
            writer_repositories = maps_import_writer_repositories()
        except MapsImportWriterNotConfiguredError as exc:
            st.error(str(exc))
            return
        try:
            with st.spinner("既存データと照合してプレビューを作成しています…"):
                preview = preview_maps_import(writer_repositories.provenance, maps_frame)
        except Exception as exc:
            # preview_maps_import() guarantees its own connection is always
            # rolled back (try/finally) before this exception reaches here,
            # whether it came from CSV validation, a SQL error, or the
            # matching functions themselves -- this broad catch is only
            # about showing a readable message, not about safety.
            st.error(f"照合中にエラーが発生したため、プレビューを中止しました: {exc}")
            return
        counts = preview["counts"]
        st.caption(
            "以下は本適用時と同じ照合ロジックによる推定です。確定結果は取込実行後に表示されます。"
            "項目の意味は取込後の表示と同一です。"
        )
        row1 = st.columns(4)
        row1[0].metric("対象（推定）", f"{counts['TOTAL']:,}件")
        row1[1].metric("Maps発見・HP取得（推定）", f"{counts['WEBSITE']:,}件")
        row1[2].metric("Maps発見・HPなし（推定）", f"{counts['NO_WEBSITE']:,}件")
        row1[3].metric("Maps未発見（推定）", f"{counts['NOT_FOUND']:,}件")
        row2 = st.columns(4)
        row2[0].metric("要確認・HOLD（推定）", f"{counts['AMBIGUOUS']:,}件")
        row2[1].metric("除外（推定）", f"{counts['EXCLUDED']:,}件")
        row2[2].metric("エラー（推定）", f"{counts['ERROR']:,}件")
        row2[3].metric("医院未紐付（推定）", f"{counts['UNLINKED']:,}件")
        if counts["PRESERVED_WEBSITE"]:
            st.caption(f"確定済みHP URLの保護対象（推定）：{counts['PRESERVED_WEBSITE']:,}件（既存の確認済みHPは格下げされません）")
        st.write(f"対象：{preview['row_count']:,}行 / {preview['column_count']}列")
        st.dataframe(preview["preview"], hide_index=True)
        confirmed = st.checkbox("内容を確認しました", key=key_prefix + "_cloud_maps_confirm_" + idempotency_key)
        if st.button(
            "確認のうえ取り込む", type="primary", disabled=not confirmed,
            key=key_prefix + "_cloud_maps_submit",
        ):
            try:
                writer_repositories = maps_import_writer_repositories()
            except MapsImportWriterNotConfiguredError as exc:
                st.error(str(exc))
                return
            with st.spinner("Google Maps結果を取り込んでいます…"):
                result = writer_repositories.provenance.import_maps_results(maps_frame)
            st.session_state[key_prefix + "_cloud_maps_applied_key"] = idempotency_key
            logging.info(
                "cloud_maps_import operator=%s idempotency_key=%s result=%s",
                operator_email, idempotency_key, result,
            )
            if result.get("already_imported"):
                st.info("このGoogle Maps結果は取込済みです。重複登録していません。")
            # Same field set and order as the preview metrics above (by
            # construction: preview_maps_import() returns this exact counts
            # shape), so comparing preview vs. confirmed result is reading
            # the same fields twice, not two different definitions.
            st.success(
                f"【確定】対象 {result.get('TOTAL',0):,}件／"
                f"Maps発見・HP取得 {result.get('WEBSITE',0):,}件／"
                f"Maps発見・HPなし {result.get('NO_WEBSITE',0):,}件／"
                f"Maps未発見 {result.get('NOT_FOUND',0):,}件／"
                f"要確認・HOLD {result.get('AMBIGUOUS',0):,}件／"
                f"除外 {result.get('EXCLUDED',0):,}件／"
                f"エラー {result.get('ERROR',0):,}件／"
                f"医院未紐付 {result.get('UNLINKED',0):,}件"
            )
            if result.get("PRESERVED_WEBSITE"):
                st.caption(f"確定済みHP URLの保護対象：{result['PRESERVED_WEBSITE']:,}件（既存の確認済みHPは格下げされませんでした）")


def cloud_maps_collection_admin_ui(store, *, key_prefix):
    """Issue #130: Cloud Run management screen for the EXISTING, human-
    operated Google Maps Collector (the Chrome extension at
    extensions/google-maps-clinic-collector/, v5.7.0) -- NOT a migration of
    collection itself to Cloud Run.

    Scope boundary, deliberate and explicit (this is the single most
    important thing to understand about this function): it only tracks
    *which clinics are assigned to which PC slot, in what state* --
    PENDING/ASSIGNED/ON_HOLD/FAILED/COMPLETED (src.master.
    maps_collection_tasks). It never performs, scripts, or automates any
    Google Maps interaction. A human still manually loads the exported
    queue CSV into the unchanged Chrome extension on their own PC, manually
    runs the collection, and manually exports the result CSV back here.
    CAPTCHA pauses are the extension's own existing behavior and are never
    bypassed or worked around by anything in this function or this PR.

    This separation (task/assignment state vs. collection execution) is
    also what Issue #130 point 10 asks for: if collection is ever replaced
    by an automated, officially-billed API client instead of the manual
    extension, only how a task's queue is produced and how its result is
    consumed would need to change here -- the task lifecycle itself does
    not assume anything about the collection mechanism.

    Reuses, rather than duplicates, the existing Comdesk/厚生局/Maps-import
    Cloud Run infrastructure: same entry gate (read_only_ui_mode()/
    is_cloud_run_deployment()), same default-OFF-flag + IAP-operator-
    authorization pattern, and for the "upload a result CSV" section,
    the SAME maps_import_writer_repositories()/preview_maps_import()/
    import_maps_results() functions cloud_maps_import_ui() already calls
    (via a separate, dedicated DB role from this feature's own task-state
    role -- see src.cloud_run.maps_collection_writer's docstring for why
    they're kept distinct). The only new logic here is linking a
    successful import to the task it completes.
    """
    from src.cloud_run.deployment_context import is_cloud_run_deployment
    from src.cloud_run.feature_flags import cloud_write_feature_enabled
    from src.cloud_run.maps_collection_writer import (
        MapsCollectionWriterNotConfiguredError, maps_collection_tasks_repository,
    )
    from src.cloud_run.maps_import_writer import (
        MapsImportWriterNotConfiguredError, maps_import_writer_repositories,
    )
    from src.cloud_run.operator_identity import operator_authorized_for_cloud_write, operator_email_from_headers
    from src.master.maps_collection_tasks import (
        PC_SLOTS, TaskTransitionError, build_collection_candidates,
        covered_clinic_ids, export_task_queue_rows,
    )
    from src.master.maps_cloud_import import maps_import_idempotency_key, preview_maps_import
    from src.master.store import now

    if read_only_ui_mode() or not is_cloud_run_deployment():
        return
    with st.expander("Google Maps収集管理（Cloud Run・既定無効）", expanded=False):
        if not cloud_write_feature_enabled("CLINIC_CLOUD_MAPS_COLLECTION_ADMIN_ENABLED"):
            st.caption(
                "CLINIC_CLOUD_MAPS_COLLECTION_ADMIN_ENABLED=1 が未設定のため無効です。"
                "収集対象の管理は現状どおりローカルで行ってください。"
            )
            return
        operator_email = operator_email_from_headers(getattr(st.context, "headers", None))
        if not operator_authorized_for_cloud_write(operator_email):
            st.error(
                "IAPでログイン済みでも、Google Maps収集管理の業務権限がある操作者として"
                "CLINIC_CLOUD_WRITE_OPERATORSに登録されていないため無効です。"
                "ログイン自体は書込み権限を意味しません。"
            )
            return
        st.caption(f"操作者：{operator_email}")
        st.caption(
            "実際のGoogle Maps収集は既存のChrome拡張機能（google-maps-clinic-collector v5.7.0）を"
            "人手で操作して行います。この画面はPCへの割り当て・進捗・結果取込との連携のみを管理し、"
            "Google Mapsへは一切アクセスしません。CAPTCHA発生時は拡張機能側でそのまま停止します。"
        )
        try:
            tasks_repo = maps_collection_tasks_repository()
        except MapsCollectionWriterNotConfiguredError as exc:
            st.error(str(exc))
            return
        tasks = tasks_repo.list_tasks()
        covered = covered_clinic_ids(tasks)

        with st.expander("新規タスク作成", expanded=False):
            prefs = store.prefectures()
            prefecture = st.selectbox("都道府県", prefs, key=key_prefix + "_new_pref") if prefs else None
            count = st.number_input("対象件数", min_value=1, max_value=2000, value=100, step=50, key=key_prefix + "_new_count")
            if prefecture and st.button("候補を確認", key=key_prefix + "_new_preview"):
                candidates = build_collection_candidates(store, prefecture, int(count), exclude_clinic_ids=covered)
                st.session_state[key_prefix + "_new_candidates"] = {"prefecture": prefecture, "candidates": candidates}
            pending_candidates = st.session_state.get(key_prefix + "_new_candidates")
            if pending_candidates and pending_candidates["prefecture"] == prefecture:
                candidates = pending_candidates["candidates"]
                st.write(f"既存タスクと重複しない候補：{len(candidates):,}件")
                if candidates and st.button("このタスクを作成", key=key_prefix + "_new_create"):
                    try:
                        task = tasks_repo.create_task(prefecture, candidates, timestamp=now())
                    except TaskTransitionError as exc:
                        st.error(str(exc))
                    else:
                        st.success(f"タスクを作成しました（{len(task['clinic_ids']):,}件、ID: {task['id'][:8]}…）。")
                        st.session_state.pop(key_prefix + "_new_candidates", None)
                        st.rerun()

        st.caption(f"タスク数：{len(tasks):,}（未着手 {sum(1 for t in tasks if t['status']=='PENDING'):,} / "
                   f"担当中 {sum(1 for t in tasks if t['status']=='ASSIGNED'):,} / "
                   f"保留 {sum(1 for t in tasks if t['status']=='ON_HOLD'):,} / "
                   f"失敗 {sum(1 for t in tasks if t['status']=='FAILED'):,} / "
                   f"完了 {sum(1 for t in tasks if t['status']=='COMPLETED'):,}）")
        for task in sorted(tasks, key=lambda t: t["created_at"], reverse=True):
            with st.container(border=True):
                st.write(
                    f"**{task['prefecture']}** ／ {len(task['clinic_ids']):,}件 ／ "
                    f"状態：{task['status']} ／ PC：{task['pc_slot'] or '未割当'} ／ ID: {task['id'][:8]}…"
                )
                if task["note"]:
                    st.caption(f"メモ：{task['note']}")
                cols = st.columns(4)
                if task["status"] == "PENDING":
                    slot = cols[0].selectbox("PC", PC_SLOTS, key=key_prefix + "_slot_" + task["id"], label_visibility="collapsed")
                    if cols[1].button("割り当て", key=key_prefix + "_assign_" + task["id"]):
                        try:
                            tasks_repo.assign_task(task["id"], slot, timestamp=now())
                        except TaskTransitionError as exc:
                            st.error(str(exc))
                        else:
                            st.rerun()
                elif task["status"] == "ASSIGNED":
                    cols[0].download_button(
                        "作業指示CSV", export_task_queue_rows(store, task),
                        f"maps_collection_task_{task['id'][:8]}.csv", mime="text/csv",
                        key=key_prefix + "_csv_" + task["id"],
                    )
                    if cols[1].button("CAPTCHA等で保留", key=key_prefix + "_hold_" + task["id"]):
                        tasks_repo.hold_task(task["id"], "CAPTCHA等により一時停止", timestamp=now())
                        st.rerun()
                    if cols[2].button("失敗にする", key=key_prefix + "_fail_" + task["id"]):
                        tasks_repo.fail_task(task["id"], "操作者により失敗とマーク", timestamp=now())
                        st.rerun()
                elif task["status"] == "ON_HOLD":
                    if cols[0].button("再開", key=key_prefix + "_resume_" + task["id"]):
                        tasks_repo.resume_task(task["id"])
                        st.rerun()
                    if cols[1].button("失敗にする", key=key_prefix + "_fail2_" + task["id"]):
                        tasks_repo.fail_task(task["id"], "操作者により失敗とマーク", timestamp=now())
                        st.rerun()
                elif task["status"] == "FAILED":
                    if cols[0].button("再割当てのため解放", key=key_prefix + "_release_" + task["id"]):
                        tasks_repo.release_task(task["id"], timestamp=now())
                        st.rerun()
                elif task["status"] == "COMPLETED":
                    st.caption(f"完了：{task['completed_at']} ／ 取込キー：{(task['result_batch_id'] or '')[:12]}…")

        with st.expander("結果CSVアップロード → タスク完了", expanded=False):
            linkable = [t for t in tasks if t["status"] in ("ASSIGNED", "ON_HOLD")]
            if not linkable:
                st.caption("担当中・保留のタスクがありません。")
                return
            labels = {t["id"]: f"{t['prefecture']}／{len(t['clinic_ids']):,}件／{t['pc_slot']}／ID:{t['id'][:8]}…" for t in linkable}
            selected_id = st.selectbox(
                "完了させるタスク", list(labels), format_func=lambda tid: labels[tid], key=key_prefix + "_complete_select",
            )
            selected_task = next(t for t in linkable if t["id"] == selected_id)
            upload = st.file_uploader("Google Maps収集結果CSV", type=["csv"], key=key_prefix + "_complete_upload")
            if not upload:
                return
            file_bytes = upload.getvalue()
            idempotency_key = maps_import_idempotency_key(file_bytes)
            applied_key = st.session_state.get(key_prefix + "_complete_applied_key")
            if applied_key == idempotency_key:
                st.success("この内容は既にこのセッションで取り込み済みです。別のファイルを選択してください。")
                return
            try:
                maps_table = load_table(file_bytes, upload.name)
            except ValueError as exc:
                st.error(f"CSVの形式・サイズ・行数を確認してください: {exc}")
                return
            maps_frame = maps_table.data.copy()
            maps_frame.columns = maps_table.headers
            uploaded_clinic_ids = {
                int(v) for v in maps_frame.get("internal_clinic_id", []) if str(v).strip().isdigit()
            } if "internal_clinic_id" in maps_frame.columns else set()
            task_clinic_ids = set(selected_task["clinic_ids"])
            overlap = uploaded_clinic_ids & task_clinic_ids
            if not uploaded_clinic_ids or not uploaded_clinic_ids <= task_clinic_ids:
                st.warning(
                    "アップロードした結果の医院IDがないか、選択したタスクの対象外の医院が含まれています。"
                    "タスクの選択、またはファイルをご確認ください。"
                )
                return
            try:
                import_writer_repositories = maps_import_writer_repositories()
            except MapsImportWriterNotConfiguredError as exc:
                st.error(str(exc))
                return
            try:
                with st.spinner("既存データと照合してプレビューを作成しています…"):
                    preview = preview_maps_import(import_writer_repositories.provenance, maps_frame)
            except Exception as exc:
                st.error(f"照合中にエラーが発生したため、プレビューを中止しました: {exc}")
                return
            counts = preview["counts"]
            st.write(f"対象：{preview['row_count']:,}行 ／ 選択タスクと一致：{len(overlap):,}件（推定）")
            st.caption(
                f"Maps発見・HP取得 {counts['WEBSITE']:,}／HPなし {counts['NO_WEBSITE']:,}／"
                f"Maps未発見 {counts['NOT_FOUND']:,}／要確認・HOLD {counts['AMBIGUOUS']:,}／"
                f"除外 {counts['EXCLUDED']:,}／エラー {counts['ERROR']:,}（推定）"
            )
            confirmed = st.checkbox("内容を確認しました", key=key_prefix + "_complete_confirm_" + idempotency_key)
            if st.button("確認のうえ取り込み、タスクを完了する", type="primary", disabled=not confirmed, key=key_prefix + "_complete_submit"):
                try:
                    import_writer_repositories = maps_import_writer_repositories()
                except MapsImportWriterNotConfiguredError as exc:
                    st.error(str(exc))
                    return
                with st.spinner("Google Maps結果を取り込んでいます…"):
                    result = import_writer_repositories.provenance.import_maps_results(maps_frame)
                st.session_state[key_prefix + "_complete_applied_key"] = idempotency_key
                logging.info(
                    "cloud_maps_collection_import operator=%s idempotency_key=%s task_id=%s result=%s",
                    operator_email, idempotency_key, selected_task["id"], result,
                )
                try:
                    tasks_repo.complete_task(
                        selected_task["id"], uploaded_clinic_ids or overlap, idempotency_key, timestamp=now(),
                    )
                except TaskTransitionError as exc:
                    st.warning(f"取込は完了しましたが、タスクの完了処理に失敗しました: {exc}")
                else:
                    st.success(f"取込・タスク完了を記録しました（対象 {result.get('TOTAL',0):,}件）。")
                    st.rerun()


def cloud_kouseikyoku_import_ui(store):
    """Issue #130 P2-(1-c): Cloud Run Writer-deployment path for 厚生局 master
    import. Mirrors cloud_comdesk_import_ui() exactly -- see that function's
    docstring for the full reasoning behind every gate here, not repeated.

    The LOCAL "①-c 厚生局CSV取込" capability actually lives in two places:
    the bundled-file one-click import ("東京都マスターを取り込む", reads
    data/master/tokyo_current.csv -- NOT shipped into the Cloud Run image
    per Dockerfile.cloudrun's COPY list, so that specific bundled-file path
    has no Cloud equivalent and none is built here) and the general
    user-upload path inside import_ui() (詳細設定 -> マスター管理, "2. 厚生局
    データを統合する", load_master()+import_master()). This panel reuses the
    LATTER's business logic. import_ui() itself is now structurally
    unreachable on Cloud Run anyway (詳細設定 is nav-locked out), so this is
    the only way to reach that capability on a future Writer deployment.

    Deliberately takes no demo/write_disabled parameter, for the same reason
    as cloud_comdesk_import_ui() (Issue #130 PR #131 follow-up review):
    this panel's own is_cloud_run_deployment()/flag/operator checks below are
    its complete, independent gate. An earlier draft of the sibling Comdesk
    panel received the caller's write_disabled -- always True on Cloud Run --
    through a parameter named `demo` and returned early whenever it was
    truthy, permanently blocking that panel. This function originally
    received the caller's real demo flag correctly (its call site never
    passed write_disabled), so it was not actually broken the same way, but
    the parameter is removed anyway to keep both panels structurally
    identical and immune to the same mistake if the call site ever changes.
    """
    from src.cloud_run.deployment_context import is_cloud_run_deployment
    from src.cloud_run.feature_flags import cloud_write_feature_enabled
    from src.cloud_run.kouseikyoku_import_writer import (
        KouseikyokuImportWriterNotConfiguredError, kouseikyoku_import_writer_repositories,
    )
    from src.cloud_run.operator_identity import operator_authorized_for_cloud_write, operator_email_from_headers
    from src.master.kouseikyoku_cloud_import import kouseikyoku_import_idempotency_key, preview_kouseikyoku_import

    if read_only_ui_mode() or not is_cloud_run_deployment():
        return
    with st.expander("厚生局データ取込（Cloud Run Writer用・既定無効）", expanded=False):
        if not cloud_write_feature_enabled("CLINIC_CLOUD_KOUSEIKYOKU_IMPORT_ENABLED"):
            st.caption(
                "CLINIC_CLOUD_KOUSEIKYOKU_IMPORT_ENABLED=1 が未設定のため無効です。"
                "Mac通常モードでは詳細設定のマスター管理をそのままご利用ください。"
            )
            return
        operator_email = operator_email_from_headers(getattr(st.context, "headers", None))
        if not operator_authorized_for_cloud_write(operator_email):
            st.error(
                "IAPでログイン済みでも、厚生局取込の業務権限がある操作者として"
                "CLINIC_CLOUD_WRITE_OPERATORSに登録されていないため無効です。"
                "ログイン自体は書込み権限を意味しません。"
            )
            return
        st.caption(f"操作者：{operator_email}")
        st.caption(
            "この操作は、このデプロイが接続しているデータベースへ実際に書き込みます"
            "（本番Supabaseに接続された状態で実行すれば本番データが更新されます）。"
            "プレビュー確認・チェック・ボタン押下まで何も書き込みません。"
            "同一内容の再送信は1セッション内でブロックします。"
        )
        upload = st.file_uploader(
            "厚生局CSV・Excel", type=["csv", "xlsx", "xls"], key="cloud_kouseikyoku_upload",
        )
        if not upload:
            return
        mode_label = st.selectbox(
            "データ形式", ["標準CSV/Excel", "関東信越厚生局の帳票Excel"], key="cloud_kouseikyoku_mode",
        )
        mode = "kanto_excel" if "帳票" in mode_label else "standard"
        prefecture = st.text_input("都道府県（帳票Excel用）", "東京都", key="cloud_kouseikyoku_pref")
        names = sheet_names(upload.getvalue(), upload.name)
        sheet = st.selectbox("厚生局シート", names, key="cloud_kouseikyoku_sheet") if names else None
        idempotency_key = kouseikyoku_import_idempotency_key(
            upload.getvalue(), mode=mode, prefecture=prefecture, sheet=sheet,
        )
        applied_key = st.session_state.get("cloud_kouseikyoku_applied_key")
        if applied_key == idempotency_key:
            st.success("この内容は既にこのセッションで取り込み済みです。別のファイル・設定を選択してください。")
            return
        frame = load_master(upload.getvalue(), upload.name, mode, prefecture, sheet)
        preview = preview_kouseikyoku_import(frame)
        st.write(f"対象：{preview['row_count']:,}行 / {preview['column_count']}列")
        st.caption("新規・更新・重複の内訳は未実装です（Postgres対応の照合プレビューは別途実装予定）。")
        st.dataframe(preview["preview"], hide_index=True)
        confirmed = st.checkbox("内容を確認しました", key="cloud_kouseikyoku_confirm_" + idempotency_key)
        if st.button(
            "確認のうえ取り込む", type="primary", disabled=not confirmed,
            key="cloud_kouseikyoku_submit",
        ):
            try:
                writer_repositories = kouseikyoku_import_writer_repositories()
            except KouseikyokuImportWriterNotConfiguredError as exc:
                st.error(str(exc))
                return
            with st.spinner("厚生局データを統合しています…"):
                result = writer_repositories.clinics.import_master(frame)
            st.session_state["cloud_kouseikyoku_applied_key"] = idempotency_key
            logging.info(
                "cloud_kouseikyoku_import operator=%s idempotency_key=%s result=%s",
                operator_email, idempotency_key, result,
            )
            report_import(result)

def render_comdesk_sync_export(store, demo):
    try:
        candidates = comdesk_sync_candidates(store)
    except Exception:
        st.metric("Comdesk同期対象", "未確定")
        st.error("Comdesk取込履歴・照合保留をREAD ONLYで確認できないため、同期用CSV出力を停止しました。provenance参照権限を確認してください。")
        return
    st.metric("Comdesk同期対象", f"{len(candidates):,}件", help="HP調査成功・UUID・Comdesk取込履歴・照合保留・merge holdを確認します。営業用HPランクは使用しません。")
    with st.expander("同期対象医院一覧", expanded=False):
        st.dataframe(pd.DataFrame([{k:r.get(k,"") for k in ("clinic_name","phone","address","prefecture")} for r in candidates]), hide_index=True)
    signature = json.dumps([str(store.path), [r["id"] for r in candidates], COMDESK_EXPORT_HEADERS, store.revision()], ensure_ascii=False)
    if st.button(f"同期対象 {len(candidates):,}件をComdesk CSVで出力", key="simple_comdesk_sync_export", disabled=demo or not candidates, use_container_width=True):
        st.session_state["simple_comdesk_sync_output"] = {
            "signature": signature,
            "content": comdesk_sync_csv(store, candidates),
        }
    output = st.session_state.get("simple_comdesk_sync_output")
    if output and output["signature"] == signature:
        st.download_button("同期対象CSVをダウンロード", output["content"], "comdesk_hp_confirmed_sync.csv", mime="text/csv", key="simple_comdesk_sync_download")


def comdesk_sync_csv(store, candidates, *, project_key="default"):
    """Export one row per sales entity, using the entity's canonical UUID when present."""
    clinics = [dict(row) for row in candidates]
    identity_loader = getattr(store, "comdesk_identity_for_clinics", None)
    identity_rows = identity_loader([row["id"] for row in clinics]) if identity_loader else {}
    canonical_by_entity = {}
    for clinic in clinics:
        identity = identity_rows.get(int(clinic["id"]), {})
        if identity:
            clinic.update(identity)
        common_id = clinic.get("common_clinic_id")
        if common_id is not None and clinic.get("canonical_comdesk_uuid"):
            canonical_by_entity[int(common_id)] = str(clinic["canonical_comdesk_uuid"]).strip()
    clinics = project_export_rows(clinics, project_key, canonical_by_entity)
    row_mapping = {"uuid":0,"clinic_name":2,"phone":9,"prefecture":5,"address":6}
    rows = [new_export_row(r, COMDESK_HEADERS, row_mapping) for r in clinics]
    if hasattr(store, "_attach_export_metadata"):
        rows = store._attach_export_metadata(clinics, rows)
    else:
        rows = [
            [*row, str(clinic.get("effective_hp_rank") or "").strip().upper()
             if str(clinic.get("effective_hp_rank") or "").strip().upper() in {"A", "B", "C", "D"} else "", ""]
            for clinic, row in zip(clinics, rows)
        ]
    return csv_bytes(COMDESK_EXPORT_HEADERS, rows)


def comdesk_sync_breakdown(store, master_metrics):
    """Return display-only Step6 counts with explicit, non-overlapping exclusions."""
    if getattr(store, "is_supabase_runtime", False):
        job = store.latest_completed_hp_job()
        successful_ids = store.hp_job_export_summary(job["id"])["export_ids"] if job else []
        clinics = store.records_by_ids(successful_ids)
    else:
        filters = Filters(active_only=True, hp_only=True)
        total = store.count(filters)
        clinics = []
        for offset in range(0, total, 50000):
            clinics.extend(store.query(filters, limit=50000, offset=offset))

    linkage = store.comdesk_linkage_state()
    imported = set(linkage["imported_clinic_ids"])
    held = set(linkage["held_clinic_ids"])
    unassigned = [row for row in clinics if not str(row.get("uuid") or "").strip()]
    imported_rows = [row for row in unassigned if int(row["id"]) in imported]
    after_import = [row for row in unassigned if int(row["id"]) not in imported]
    held_rows = [row for row in after_import if int(row["id"]) in held]
    after_held = [row for row in after_import if int(row["id"]) not in held]
    merge_rows = [row for row in after_held if row.get("merge_hold")]
    candidates = len(after_held) - len(merge_rows)
    try:
        if hasattr(store, "web_research_metrics"):
            hp_counts = (store.web_research_metrics(read_only_ui=True)
                         if getattr(store, "is_supabase_runtime", False)
                         else store.web_research_metrics())
        else:
            hp_counts = web_research_metrics(store.path)
    except TypeError:
        hp_counts = store.web_research_metrics()
    if hp_counts is None:
        raise ValueError("HP URL母集団の集計を取得できません。")
    return [
        {"項目": "厚生局全件", "件数": f"{int(master_metrics.get('厚生局取込レコード', 0)):,}件", "母集団・条件": "厚生局 source_records の取込元レコード数（医院数ではありません）"},
        {"項目": "HP URL取得済み", "件数": f"{int(hp_counts.get('url_acquired', 0)):,}件", "母集団・条件": "hp_url または Maps WebサイトURL 登録済み医院"},
        {"項目": "HP調査完了", "件数": f"{int(hp_counts.get('researched', 0)):,}件", "母集団・条件": "上記URL母集団のうち fetch_status=OK（取得・公式HP検証完了）"},
        {"項目": "HP成功医院（同期候補の母集団）", "件数": f"{len(clinics):,}件", "母集団・条件": "現行同期処理と同じ最新完了HP Job（SQLiteはhp_status=VERIFIEDかつURLあり）"},
        {"項目": "UUID未付与", "件数": f"{len(unassigned):,}件", "母集団・条件": "HP成功医院のうちUUIDが空欄"},
        {"項目": "除外：Comdesk取込済み", "件数": f"{len(imported_rows):,}件", "母集団・条件": "UUID未付与の医院から除外（最初に判定）"},
        {"項目": "除外：照合保留", "件数": f"{len(held_rows):,}件", "母集団・条件": "Comdesk取込済み除外後の未解決照合・候補医院"},
        {"項目": "除外：merge hold", "件数": f"{len(merge_rows):,}件", "母集団・条件": "取込済み・照合保留除外後にmerge holdがある医院"},
        {"項目": "最終Comdesk同期対象", "件数": f"{candidates:,}件", "母集団・条件": "HP成功・UUID未付与から上記3条件を優先順に除外。営業用HPランクは不使用"},
    ]


def comdesk_sync_candidates(store, page_size=50000):
    if getattr(store, "is_supabase_runtime", False):
        # Reuse the HP workflow's canonical SUCCESS/Human Review/fetch-status gate.
        job = store.latest_completed_hp_job()
        successful_ids = store.hp_job_export_summary(job["id"])["export_ids"] if job else []
        clinics = store.records_by_ids(successful_ids)
    else:
        # SQLite/test mode has no canonical HP job ledger. Its established projection of the
        # same success state is hp_status=VERIFIED with a non-empty official hp_url.
        filters = Filters(active_only=True, hp_only=True)
        total = store.count(filters)
        clinics = []
        for offset in range(0, total, page_size):
            clinics.extend(store.query(filters, limit=page_size, offset=offset))

    linkage = store.comdesk_linkage_state()
    imported_ids = set(linkage["imported_clinic_ids"])
    held_ids = set(linkage["held_clinic_ids"])
    held_ids.update(int(r["id"]) for r in clinics if r.get("merge_hold"))
    candidates = [
        r for r in clinics
        if not str(r.get("uuid") or "").strip()
        and int(r["id"]) not in imported_ids
        and int(r["id"]) not in held_ids
    ]
    return candidates

def simple_sales_ui(store, demo=False, *, read_only=False):
    st.header("営業対象・Comdesk出力")
    st.caption("普段使う営業条件だけを表示しています。")
    st.subheader("Webサイト調査状況")
    show_web_research_metrics(store, read_only=read_only)

    scope_options = [SCOPE_LEGACY_PRE_NATIONAL, SCOPE_ALL]
    # 2026-10-05: 正式Sales Tier分類(SSOT)のcohortを旧13,970件のlegacy scopeへ
    # 取りこぼさないよう、既定値だけを全国Clinic Masterへ変更（表示順・選択肢はそのまま）。
    scope = st.selectbox("対象データ", scope_options, index=scope_options.index(SCOPE_ALL), format_func=lambda s: SCOPE_LABELS[s], key="simple_sales_scope")
    if getattr(store, "is_supabase_runtime", False):
        prefs = store.prefectures(read_only_ui=True) if read_only else store.prefectures()
    else:
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
    ad_signal_options = {name: label for name, label in AD_SIGNAL_LABELS.items()
                         if not read_only or name not in READ_ONLY_EXCLUDED_MARKETING_SIGNALS}
    ads = st.multiselect("広告・集客施策", list(ad_signal_options), format_func=ad_signal_options.get, key="simple_sales_ads")
    ad_min = st.selectbox("広告・集客施策数", ad_count_options(store, read_only=read_only), format_func=ad_count_label, key="simple_sales_ad_min")
    companies = st.multiselect("HP制作会社", list(read_config(ROOT/"config/production_companies.yml")), key="simple_sales_companies")

    cols = st.columns(4)
    recent = cols[0].checkbox("開業10年以内", key="simple_sales_recent")
    age = False if read_only else cols[1].checkbox("59歳以下 50%以上", key="simple_sales_age")
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
        count = (0 if non_ab_only_selected else
                 store.count_read_only_ui(sales_filters)
                 if read_only and hasattr(store, "count_read_only_ui") else store.count(sales_filters))
    except SalesClassificationUnavailableError as exc:
        st.error(str(exc))
        return
    st.metric("営業対象", f"{count:,}件")
    # The historical Sales Tier CSV/SQLite artifact is not a production SoT.  It remains
    # available to explicit offline/admin analysis only and is never opened by Supabase runtime.
    summary = None if getattr(store, "is_supabase_runtime", False) else sales_classification_summary()
    if summary:
        st.caption(
            f"参考：Sales Tier分類（{summary['source_path'].name}、HP ABC判定とは別軸）："
            f"A={summary['by_tier']['A']:,}　B={summary['by_tier']['B']:,}　"
            f"C={summary['by_tier']['C']:,}　D={summary['by_tier']['D']:,}（営業対象外）　"
            f"｜営業利用可能(A+B+C)：{summary['sales_usable']:,}件"
        )

    with st.expander("対象医院を確認", expanded=False):
        if non_ab_only_selected:
            st.info("選択したHP ABC判定には営業対象のA/Bが含まれないため、対象医院は0件です。")
        else:
            listing(store, sales_filters, "simple_sales_results", read_only=read_only)

    st.subheader("営業対象をComdesk形式で出力")
    st.caption(
        "上で選択した営業条件をそのまま反映して出力します。"
        "治療カテゴリを選択していない場合は治療カテゴリでは絞り込みません。"
        "HP ABC判定は営業対象のA/B条件を反映します。"
    )
    readonly_export_over_limit = read_only and count > 100000
    if readonly_export_over_limit:
        st.warning(
            f"READ ONLY出力は100,000件までです。現在の条件は{count:,}件のため、"
            "一部のみのファイルを誤って出力しないようダウンロードを停止しました。条件を絞ってください。"
        )
    sales_export_signature = json.dumps(
        [str(store.path), asdict(sales_filters), COMDESK_EXPORT_HEADERS,
         None if read_only else store.revision(), count],
        ensure_ascii=False,
        sort_keys=True,
    )
    if st.button(
        f"営業対象 {count:,}件をComdesk形式で出力",
        type="primary",
        key="simple_sales_filtered_export",
        disabled=demo or count == 0 or readonly_export_over_limit,
        use_container_width=True,
    ):
        try:
            files = (store.export_read_only(sales_filters)
                     if read_only and hasattr(store, "export_read_only")
                     else store.export(sales_filters))
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.session_state["simple_sales_filtered_export_files"] = {
                "signature": sales_export_signature,
                "files": files,
            }
    sales_output = st.session_state.get("simple_sales_filtered_export_files")
    if sales_output and sales_output["signature"] == sales_export_signature:
        for name, content in sales_output["files"].items():
            st.download_button(
                "営業対象Excelをダウンロード" if name.endswith("xlsx") else "営業対象CSVをダウンロード",
                content,
                name,
                key="simple_sales_filtered_download_" + name,
                use_container_width=True,
            )



def single_hp_job_recovery_ui(store, demo):
    """Administrator-facing controls for saved, standalone HP jobs."""
    st.caption("単発HP調査Jobの進捗と復旧操作です。Auto Runとは別のJobで、Step1〜7には操作パネルを表示しません。")
    jobs = []
    for job in recent_jobs(store):
        if job.get("kind") != "hp":
            continue
        options = job.get("options_json") or {}
        if isinstance(options, str):
            try:
                options = json.loads(options)
            except (TypeError, ValueError):
                options = {}
        if isinstance(options, dict) and options.get("auto_run_id"):
            continue
        jobs.append(job)

    if not jobs:
        st.info("管理対象の単発HP調査Jobはありません。")
        return

    labels = {"RUNNING": "実行中", "PAUSED": "一時停止", "BUDGET": "予算待ち", "COMPLETED": "完了", "ERROR": "エラー"}
    selected = st.selectbox(
        "単発HP Job履歴",
        [job["id"] for job in jobs],
        format_func=lambda jid: next(
            f"{job.get('created_at', '')[:16]}｜{labels.get(job.get('status'), job.get('status', '不明'))}｜{jid}"
            for job in jobs if job["id"] == jid
        ),
        key="advanced_single_hp_job",
    )
    current = job_status(store, selected)
    runner = runner_for(str(store.path))
    cloud_run_executor = hp_executor_mode() == "cloud_run"
    write_disabled = demo
    st.caption(f"Job ID：{selected}／開始時対象 {current['total']:,}件／状態 {labels.get(current['status'], current['status'])}")
    controls = st.columns(3)

    can_pause = not write_disabled and (runner.running() or current["status"] == "RUNNING")
    if controls[0].button("一時停止", disabled=not can_pause, key="advanced_single_hp_pause", use_container_width=True):
        if cloud_run_executor:
            _pause_cloud_hp_job(current["id"])
        else:
            write_repositories_for(store).jobs.pause_job(current["id"])
        st.rerun()

    can_resume = (
        not write_disabled
        and not runner.running()
        and current["status"] in {"PAUSED", "BUDGET"}
        and current["counts"].get("PENDING", 0) > 0
    )
    if controls[1].button("この調査を再開", disabled=not can_resume, key="advanced_single_hp_resume", use_container_width=True):
        st.session_state["active_job"] = current["id"]
        if cloud_run_executor:
            _dispatch_cloud_hp_job(current["id"], clinic_count=0)
        else:
            runner.start(store, current["id"], TavilySearchProvider(""))
        st.rerun()

    if cloud_run_executor:
        from src.cloud_run.hp_executor import get_dispatch_state
        from src.cloud_run.dispatch_state import STATUS_DISPATCH_UNKNOWN
        try:
            dispatch_state = get_dispatch_state(current["id"])
        except Exception:
            dispatch_state = None
        if dispatch_state and dispatch_state.get("status") == STATUS_DISPATCH_UNKNOWN:
            st.warning("このJobは前回の起動結果が未確認です。再開の前に照合してください。")
            if st.button("起動結果を照合", key="advanced_single_hp_reconcile", use_container_width=True):
                _reconcile_cloud_hp_job(current["id"])
                st.rerun()

    can_reset = not write_disabled and not runner.running() and current["status"] != "RUNNING"
    if controls[2].button("この調査をリセット", disabled=not can_reset, key="advanced_single_hp_reset", use_container_width=True):
        try:
            write_repositories_for(store).jobs.reset_job(current["id"])
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

    show_job_progress(store, current["id"])


def advanced_ui(store, demo):
    st.header("詳細設定")
    st.caption("普段は使わない設定・確認機能です。")
    section = st.selectbox(
        "開く画面",
        ["マスター管理", "自動情報収集（詳細）", "HP単発Job管理・復旧", "メール調査", "営業対象フィルター（詳細）", "要確認・設定", "HP Human Review", "Email Human Review", "歯科営業タグ", "ダッシュボード"],
        key="advanced_section",
    )

    if section == "マスター管理":
        import_ui(store, demo)
    elif section == "自動情報収集（詳細）":
        research_ui(store, demo)
    elif section == "HP単発Job管理・復旧":
        single_hp_job_recovery_ui(store, demo)
    elif section == "メール調査":
        email_research_page(store)
    elif section == "営業対象フィルター（詳細）":
        sales_ui(store)
    elif section == "要確認・設定":
        settings_ui(store)
    elif section == "HP Human Review":
        hp_human_review_page(store)
    elif section == "Email Human Review":
        email_human_review_page(store)
    elif section == "歯科営業タグ":
        dental_sales_tags_page(store)
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
        jid = write_repositories_for(store).jobs.create_job_from_filters(f,kind,count,max_search,force,max_pages)
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
            write_repositories_for(store).jobs.job_limit(selected,increased)
            runner.start(store,selected,provider)
        if cols[1].button("一時停止",disabled=not runner.running()):
            write_repositories_for(store).jobs.pause_job(selected)
        reset_disabled = runner.running() or current["status"] == "RUNNING"
        if cols[2].button("この調査をリセット",disabled=reset_disabled,help="この調査の進捗履歴だけをリセットします。医院の調査結果、Google Maps取込結果、UUID、月間検索使用数は削除しません。"):
            try:
                write_repositories_for(store).jobs.reset_job(selected)
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
        if getattr(store, "is_supabase_runtime", False):
            has_maps = store.has_maps()
        else:
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
    st.subheader("全期間の営業対象を出力（詳細設定）")
    st.caption("かんたん操作のStep5とは別の全期間検索です。過去のHP確認済み医院も選択条件に応じて含まれます。")
    export_headers = COMDESK_EXPORT_HEADERS if getattr(store, "is_supabase_runtime", False) else COMDESK_HEADERS
    st.caption(
        f"出力形式：{len(export_headers)}項目（固定）。C列「名前」にクリニック名、AA列「院長名」に先生のお名前を出力します。"
        "Supabase本番ではAC列「HPランク」・AD列「メールアドレス」も追加します。"
    )
    with st.expander(f"毎回1行目に出力する{len(export_headers)}項目", expanded=True):
        labels = [chr(65+i) if i < 26 else "A" + chr(65+i-26) for i in range(len(export_headers))]
        st.dataframe(pd.DataFrame({"列": labels, "1行目の項目名": export_headers}), hide_index=True, width="stretch")
    signature = json.dumps([str(store.path),asdict(filters),export_headers,store.revision()],ensure_ascii=False,sort_keys=True)
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
                write_repositories_for(store).clinics.resolve_review(rid,target,note)
                st.rerun()
    st.subheader("判定の確認・手動修正")
    keyword = st.text_input("確認する医院名・電話番号・UUID",key="review_search")
    if keyword:
        listing(store,Filters(active_only=False,hp_only=False,keyword=keyword),"review")
    st.subheader("データ基盤")
    if getattr(store, "is_supabase_runtime", False):
        st.write("使用中のデータ基盤：Supabase PostgreSQL")
        st.caption("SQLiteバックアップは管理・移行専用です。通常Runtimeからは作成・参照しません。")
    else:
        st.caption("管理・テスト用SQLiteモード")
        if st.button("バックアップを作成"):
            st.session_state["db_backup"] = store.backup_bytes()
        if "db_backup" in st.session_state:
            st.download_button("バックアップをダウンロード",st.session_state["db_backup"],f"clinics_backup_{today_japan().isoformat()}.sqlite3")


def _show_supabase_startup_error(exc):
    st.error(safe_connection_message(exc, service="Clinic/HP"))
    st.caption("設定読込を統一するため、scripts/launch_v2.py またはOS別の正式ランチャーから起動してください。")
    st.stop()


def main():
    st.set_page_config(page_title="クリニック営業マスター", page_icon="📋", layout="wide")
    if comdesk_preview_only_mode():
        st.title("Clinic Lead System — Comdesk Preview専用")
        st.caption("他の画面・自動処理・外部APIを起動せず、Step7の読取Previewだけを表示します。")
        try:
            repository = comdesk_preview_repository_for(runtime_store_cache_key())
        except Exception:
            st.error("専用Reader接続の検証に失敗しました。接続ロール・権限・RLSを確認してください。")
            return
        render_comdesk_preview_only(repository)
        return

    st.title("クリニック営業マスター")
    st.caption("①データ準備 → ②Google Maps → ③HP調査 → ④営業対象・Comdesk出力")

    if st.session_state.pop("_jump_to_hp_human_review", False):
        # Apply before the sidebar/selectbox widgets are instantiated in this rerun.
        st.session_state["navigation"] = "詳細設定"
        st.session_state["advanced_section"] = "HP Human Review"

    read_only = read_only_ui_mode()
    # Review fix (Issue #130 PR #131 follow-up): lock the NAV down whenever
    # EITHER read_only is True OR this process is a Cloud Run deployment at
    # all (is_cloud_run_deployment(), regardless of CLINIC_READ_ONLY_UI) --
    # see the write_disabled comment in simple_workflow_ui for the full
    # reasoning. nav_locked, not read_only alone, decides nav visibility.
    from src.cloud_run.deployment_context import is_cloud_run_deployment
    nav_locked = read_only or is_cloud_run_deployment()
    with st.sidebar:
        nav_options = NAV[:2] if nav_locked else NAV
        nav = st.radio("メニュー", nav_options, key="navigation")
        if nav_locked:
            demo = False
            with st.expander("ローカル版のメニュー構成（参考）", expanded=False):
                st.caption("ローカル版は以下の5メニューを順に使います。この画面では①②のみ操作でき、③〜⑤はボタン自体を表示していません。")
                for name in NAV:
                    if name in nav_options:
                        st.caption(f"✅ {name}：利用可能")
                    else:
                        st.caption(f"🔒 {name}：未有効・別権限が必要（データ更新/送信を含むため非表示）")
        else:
            with st.expander("開発・確認用", expanded=False):
                demo = st.toggle("サンプルモード", key="sample_mode")
        st.caption("普段は「かんたん操作」を上から順に進めればOKです。")
    if read_only:
        st.warning("READ ONLY画面確認モードです。取込・UUID更新・Job操作・データ更新は無効です。")
    elif nav_locked:
        st.warning("Cloud Run環境のため、既存の取込・HP Job・Email・設定機能は既定で無効です。個別に承認・実装されたCloud専用機能のみ利用できます。")

    from src.repository.backend import active_backend, BACKEND_SUPABASE
    from src.repository.write_backend import active_write_backend, WRITE_BACKEND_SUPABASE
    if demo:
        path = Path(os.getenv("CLINIC_DEMO_DB_PATH", str(ROOT/"data/demo.sqlite3")))
    elif active_backend() != BACKEND_SUPABASE:
        path, db_error = resolve_production_db_path()
        if db_error:
            st.error(db_error)
            st.stop()
    else:
        path = None
    if not demo and (active_backend() == BACKEND_SUPABASE or active_write_backend() == WRITE_BACKEND_SUPABASE):
        required_keys = ("CLINIC_DATA_BACKEND", "CLINIC_WRITE_BACKEND", "SUPABASE_RUNTIME_DB_URL")
        if any(not os.environ.get(key, "").strip() for key in required_keys):
            _show_supabase_startup_error(RuntimeError("Clinic runtime configuration is missing"))
    try:
        store = store_for(str(path) if path is not None else None, runtime_store_cache_key())
        from src.cloud_run.deployment_context import is_cloud_run_deployment
        if not read_only and not is_cloud_run_executor() and not is_cloud_run_deployment():
            # refresh_age_model() is a general write unrelated to HP Job control.
            # CLINIC_HP_EXECUTOR=cloud_run is meant to run with a least-privilege
            # job-control-only DB role (see src.cloud_run.job_control); it must
            # never also need this broader write capability on every page load.
            # is_cloud_run_deployment() is also required: a Cloud Run Web
            # deployment with CLINIC_READ_ONLY_UI=false but no CLINIC_HP_EXECUTOR
            # set must still never auto-write on every page load.
            write_repositories_for(store).clinics.refresh_age_model()
    except Exception as exc:
        if not demo and (active_backend() == BACKEND_SUPABASE or active_write_backend() == WRITE_BACKEND_SUPABASE):
            _show_supabase_startup_error(exc)
        raise

    if not read_only and not is_cloud_run_deployment():
        # Same write-trigger concern as the simple_workflow_ui human-review
        # cards above: these sidebar/dialog widgets write via their own
        # write_repositories_for(store) calls with no Cloud Run awareness.
        hp_human_review_sidebar(store)
        email_research_sidebar_status(store)
        email_human_review_sidebar(store)
        if st.session_state.pop("_open_hp_human_review_dialog", False):
            hp_human_review_dialog(store)
        if st.session_state.pop("_open_email_human_review_dialog", False):
            email_human_review_dialog(store)

    if demo:
        load_demo(store)
        st.info("架空サンプル4医院を表示しています。実データとは別に保存されます。")

    if nav == "かんたん操作":
        simple_workflow_ui(store, demo, read_only=read_only)
    elif nav == "営業対象・出力":
        simple_sales_ui(store, demo, read_only=read_only)
    elif nav == "Email調査":
        email_research_page(store)
    elif nav == "メール送付文章":
        from src.outreach_ui import outreach_email_page
        outreach_email_page(store, demo, read_only=read_only)
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
        if is_database_connection_exception(exc):
            st.error(safe_connection_message(exc, service="Clinic/HP"))
        else:
            # Driver exceptions may embed host, user, or credential fragments. Do not
            # print tracebacks or interpolate raw errors into the Streamlit page.
            category = connection_error_category(exc)
            st.error(f"処理を完了できませんでした（{category}）。接続状態と操作内容を確認してください。")
