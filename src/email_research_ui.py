"""Read/control panel for the Email Auto Research subsystem."""
from __future__ import annotations

from functools import wraps
import logging
import streamlit as st
import os
from pathlib import Path
from src.master.email_auto_run import EmailAutoRunner, build_default_research_one
from src.master.email_research_config import load_email_research_config
from src.repository.email_research_repository import email_controller_health
from src.repository.email_database import (
    build_email_repositories, email_database_status, email_database_ui_status,
    email_target_reader_status, email_repositories_for_ui,
    note_email_database_connection_error,
)
from src.repository.email_target_reader import connect_email_target_reader


EMAIL_RUN_LABELS = {
    "RUNNING": "実行中", "PAUSED": "一時停止", "STOPPED": "停止",
    "COMPLETED": "完了", "ERROR": "エラー", "STOPPING": "停止中",
}

DB_CONNECTION_LOST_MESSAGE = "Email Research DBとの接続が一時的に切れました。自動再接続を試行します。"
EMAIL_AUTO_START_ERROR_STATE_KEY = "_email_auto_start_error"
_logger = logging.getLogger(__name__)


def _safe_connection_error_category(exc: BaseException) -> str:
    """Classify a connection failure without returning/logging its raw message."""
    messages = []
    sqlstates = set()
    current = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        messages.append(str(current).casefold())
        if getattr(current, "sqlstate", None):
            sqlstates.add(str(current.sqlstate))
        current = current.__cause__ or current.__context__
    message = " ".join(messages)
    if any(token in message for token in (
        "tenant or user not found", "no such tenant", "project not found",
        "max client connections", "max clients reached", "connection pool",
    )):
        return "pool"
    if sqlstates & {"28P01", "28000"} or any(token in message for token in (
        "password authentication failed", "authentication failed", "invalid password",
    )):
        return "auth"
    if any(token in message for token in (
        "name or service not known", "nodename nor servname provided",
        "temporary failure in name resolution", "could not translate host name",
    )):
        return "dns"
    if "connection refused" in message:
        return "refused"
    if any(token in message for token in (
        "certificate verify failed", "ssl error", "tls error", "certificate has expired",
    )):
        return "ssl"
    if any(token in message for token in (
        "timeout expired", "timed out", "connection timeout",
    )):
        return "timeout"
    return "other"


def _email_auto_start_error_message(*, stage: str, category: str) -> str:
    stage_labels = {
        "email_repository": "Email DB接続",
        "target_reader": "FULL対象取得",
        "sync_targets": "対象同期",
        "create_auto_run": "Run作成",
    }
    category_labels = {
        "auth": "DB認証に失敗しました。正式ランチャーの接続資格情報を確認してください。",
        "pool": "DB接続プールで接続を確立できませんでした。接続設定またはプール上限を確認してください。",
        "dns": "DBホストの名前解決に失敗しました。DNS・ネットワーク設定を確認してください。",
        "refused": "DB接続が拒否されました。接続先とポートを確認してください。",
        "ssl": "DBのSSL/TLS検証に失敗しました。SSL設定を確認してください。",
        "timeout": "DB接続がタイムアウトしました。ネットワークと接続先の状態を確認してください。",
        "other": "開始前処理でエラーが発生しました。安全な診断ログを確認してください。",
    }
    stage_label = stage_labels.get(stage, "開始前処理")
    detail = category_labels.get(category, category_labels["other"])
    if stage == "create_auto_run":
        return (
            f"メール自動調査の開始結果を確認できません（{stage_label}）：{detail} "
            "Run作成の応答が失われた可能性があります。再実行せず、Run一覧を再読込してください。"
        )
    return f"メール自動調査の開始失敗（{stage_label}）：{detail} 新規Runは作成されていません。"


def _fail_soft_on_db_loss(func):
    """Last line of defense: a fragment must never show a raw traceback for a transient
    DB disconnect. The repository layer already retries reads once internally; this only
    catches the rare case where that retry also failed (or a non-Email-Research call, such
    as the Human Review repository sharing the same connection, hit the same error)."""
    @wraps(func)
    def wrapped(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            try:
                import psycopg
            except ImportError:
                raise
            if isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError)):
                note_email_database_connection_error(exc)
                status = email_database_ui_status()
                st.warning(f"{DB_CONNECTION_LOST_MESSAGE} {status.message}" if not status.ready
                           else DB_CONNECTION_LOST_MESSAGE)
                return None
            raise
    return wrapped


@st.cache_resource
def _auto_runner():
    def repository_factory():
        bundle, status = build_email_repositories()
        if bundle is None:
            raise RuntimeError(status.message)
        return bundle.email_research

    return EmailAutoRunner(
        repository_factory,
        build_default_research_one(),
        target_reader=lambda: _read_email_targets(), config=load_email_research_config(),
    )


def _read_email_targets():
    reader = connect_email_target_reader()
    try:
        return reader.eligible_targets()
    finally:
        reader._conn.close()


@_fail_soft_on_db_loss
def _email_auto_research_controls(store, *, demo=False):
    """Compact live controls for Email's independent HP-verified-URL consumer."""
    repositories = email_repositories_for_ui()
    if repositories is None:
        st.subheader("メールアドレス自動調査")
        st.caption(email_database_ui_status().message)
        st.button("メール自動調査を開始", key="simple_email_auto_start_unavailable", disabled=True)
        return
    repo = repositories.email_research
    st.subheader("メールアドレス自動調査")
    if repo is None:
        st.caption(email_database_ui_status().message)
        st.button("メール自動調査を開始", key="simple_email_auto_start_unavailable", disabled=True)
        return
    if not repo.available():
        st.caption("Email Research用共有DBにschemaがありません。未設定・未適用のため開始できません。")
        st.button("メール自動調査を開始", key="simple_email_auto_start_unavailable", disabled=True)
        return

    summary = repo.summary()
    run = repo.latest_auto_run()
    status = run.get("status") if run else "STOPPED"
    controller_health = email_controller_health(run)
    human_repo = repositories.email_human_review
    human_summary = human_repo.summary() if human_repo is not None and human_repo.available() else {}
    status_label = EMAIL_RUN_LABELS.get(status, "停止中")
    if controller_health == "RECOVERABLE":
        status_label = "復旧待ち（controller lease期限切れ）"
    st.markdown(f"状態：**{status_label}**")
    metrics = run.get("metrics") or {} if run else {}
    counts = st.columns(6)
    counts[0].metric("対象", f"{summary.get('total', 0):,}件")
    counts[1].metric("処理済み", f"{summary.get('processed', 0):,}件")
    counts[2].metric("残り", f"{summary.get('remaining', 0):,}件")
    counts[3].metric("Email取得済み", f"{summary.get('email_obtained', summary.get('primary', 0)):,}件")
    counts[4].metric("確認待ち", f"{human_summary.get('unreviewed_clinics', human_summary.get('unreviewed', 0)):,}件")
    counts[5].metric("Emailなし / Error", f"{summary.get('no_email_found', 0):,} / {summary.get('error', 0)}件")
    if run:
        st.caption(
            f"Search Query {metrics.get('search_query_count', 0):,}回 / "
            f"HTTP {metrics.get('http_attempts', metrics.get('http_attempt_count', 0)):,}回 / "
            f"timeout {metrics.get('timeout_count', 0) + metrics.get('web_fetch_timeout', 0):,} / "
            f"retry {metrics.get('retry_count', 0) + metrics.get('rate_limit_retry_count', 0):,}"
        )

    enabled = os.environ.get("EMAIL_AUTO_RUN_ENABLED", "false").strip().casefold() == "true"
    target_status = email_target_reader_status()
    target_ready = target_status.ready
    if not target_ready:
        st.caption(target_status.message)
    runtime = bool(getattr(store, "is_supabase_runtime", False))
    from src.cloud_run.email_executor import email_cloud_run_enabled
    cloud_email = email_cloud_run_enabled()
    runner = _auto_runner() if enabled and runtime and not demo and not cloud_email else None
    if cloud_email:
        st.caption("Cloud Run Email Worker：1実行あたり最大件数を制限し、既存Queueを分割処理します。曖昧な起動結果は自動再実行しません。")
        try:
            from src.cloud_run.email_executor import max_clinics_per_execution
            st.caption(f"1 Execution上限：{max_clinics_per_execution():,}件")
        except Exception as exc:
            st.caption(str(exc))
    if (runner is not None and hasattr(runner, "running") and run and status == "RUNNING"
            and controller_health == "RECOVERABLE" and not runner.running()):
        # Re-offer the existing run to this process. The persisted lease decides
        # Active vs Standby; no new run/cohort is created here.
        runner.recover(run["run_id"])
    controls = st.columns(4)
    if controls[0].button(
        "メール自動調査を開始", key="simple_email_auto_start", type="primary",
        disabled=not enabled or demo or not runtime or not target_ready or status in {"RUNNING", "PAUSED"}
                 or (not cloud_email and runner is None),
        use_container_width=True,
    ):
        try:
            if cloud_email:
                from src.cloud_run.email_executor import create_email_run, dispatch_email_worker
                run_id = create_email_run()
                dispatch_email_worker(run_id)
                started = run_id
            else:
                started = runner.start()
        except Exception as exc:
            stage = str(getattr(runner, "last_start_stage", "unknown") or "unknown")
            category = _safe_connection_error_category(exc)
            # Never log exc/str(exc)/exc_info: libpq errors may contain endpoint details.
            _logger.error(
            "email_auto_start_failed stage=%s category=%s exception_type=%s",
                stage, category, type(exc).__name__,
            )
            from src.cloud_run.email_executor import EmailDispatchOutcomeUnknownError
            if cloud_email and isinstance(exc, EmailDispatchOutcomeUnknownError):
                st.session_state[EMAIL_AUTO_START_ERROR_STATE_KEY] = (
                    "Worker起動結果が未確認です。新規Runの再作成・自動再実行は行っていません。"
                    "状態を再読込し、Cloud Run Executionと照合してください。"
                )
            else:
                st.session_state[EMAIL_AUTO_START_ERROR_STATE_KEY] = (
                    _email_auto_start_error_message(stage=stage, category=category)
                )
            started = False
        if started:
            st.session_state.pop(EMAIL_AUTO_START_ERROR_STATE_KEY, None)
            st.rerun(scope="fragment")
        elif EMAIL_AUTO_START_ERROR_STATE_KEY not in st.session_state:
            # cache_resource intentionally preserves the controller thread across
            # fragment reruns. If it is still alive, start() returns False; make that
            # otherwise-silent state explicit without creating another Run.
            st.session_state[EMAIL_AUTO_START_ERROR_STATE_KEY] = (
                "メール自動調査を開始できませんでした。このMac Runtime内で前回Runの"
                "終了処理が継続中です。新規Runは作成されていません。"
            )
            _logger.error(
                "email_auto_start_failed stage=%s category=runner_busy exception_type=none",
                str(getattr(runner, "last_start_stage", "unknown") or "unknown"),
            )
    if st.session_state.get(EMAIL_AUTO_START_ERROR_STATE_KEY):
        st.error(st.session_state[EMAIL_AUTO_START_ERROR_STATE_KEY])
    if controls[1].button(
        "一時停止", key="simple_email_auto_pause",
        disabled=(not cloud_email and not runner) or status != "RUNNING", use_container_width=True,
    ):
        if cloud_email:
            from src.cloud_run.email_executor import update_run
            update_run(run["run_id"], "PAUSED")
        else:
            runner.pause(run["run_id"])
        st.rerun(scope="fragment")
    if controls[2].button(
        "再開", key="simple_email_auto_resume",
        disabled=(not cloud_email and not runner) or status != "PAUSED", use_container_width=True,
    ):
        if cloud_email:
            from src.cloud_run.email_executor import dispatch_email_worker, update_run
            update_run(run["run_id"], "RUNNING")
            dispatch_email_worker(run["run_id"])
            st.rerun(scope="fragment")
        elif runner.resume(run["run_id"]): st.rerun(scope="fragment")
        else: st.warning("前Runの終了処理中です。少し待ってから再度お試しください。")
    if controls[3].button(
        "停止", key="simple_email_auto_stop",
        disabled=(not cloud_email and not runner) or status not in {"RUNNING", "PAUSED"}, use_container_width=True,
    ):
        if cloud_email:
            from src.cloud_run.email_executor import update_run
            update_run(run["run_id"], "STOPPED")
        else:
            runner.kill(run["run_id"])
        st.rerun(scope="fragment")
    if cloud_email and run and status == "RUNNING" and controller_health == "RECOVERABLE":
        st.warning("controller leaseが期限切れです。直前Executionの状態を確認してから手動でWorkerを再ディスパッチしてください。")
        if st.button("既存RunのWorkerを再ディスパッチ", key="email_cloud_worker_recover"):
            from src.cloud_run.email_executor import dispatch_email_worker
            dispatch_email_worker(run["run_id"])
            st.rerun(scope="fragment")
    if not enabled:
        st.caption("開始操作は環境側で許可された場合のみ有効になります。")


@st.fragment(run_every="5s")
def email_auto_research_card(store, *, demo=False):
    return _email_auto_research_controls(store, demo=demo)


@st.fragment(run_every="10s")
@_fail_soft_on_db_loss
def email_research_sidebar_status(store):
    """Compact always-visible sidebar status, mirroring hp/email human review sidebar widgets."""
    repositories = email_repositories_for_ui()
    if repositories is None:
        with st.sidebar:
            st.caption(email_database_ui_status().message)
        return
    repo = repositories.email_research
    if repo is None or not repo.available():
        return
    run = repo.latest_auto_run()
    status = run.get("status") if run else "STOPPED"
    health = email_controller_health(run)
    if health == "RECOVERABLE":
        status = "RECOVERABLE"
    primary_count = repo.summary().get("primary", 0)
    with st.sidebar:
        st.markdown("### 📧 Email調査")
        label = "復旧待ち" if status == "RECOVERABLE" else EMAIL_RUN_LABELS.get(status, "停止中")
        st.caption(f"● {label}　Primary {primary_count:,}件")


def _batch_progress_card(repo):
    """Current Canary cohort's own progress -- distinct from summary()'s all-time total."""
    batch = repo.cohort_batch_summary()
    if batch is None:
        return
    st.markdown("### 現在のバッチ")
    st.progress(
        batch["processed"] / batch["cohort_size"] if batch["cohort_size"] else 0.0,
        text=f"{batch['processed']:,} / {batch['cohort_size']:,}"
             f"（{(batch['processed']/batch['cohort_size']*100 if batch['cohort_size'] else 0):.1f}%）",
    )
    cols = st.columns(4)
    cols[0].metric("Primary", f"{batch['found']:,}")
    cols[1].metric("要確認", f"{batch['review']:,}")
    cols[2].metric("取得なし", f"{batch['no_email_found']:,}")
    cols[3].metric("Error", f"{batch['error']:,}")
    with st.expander("管理者向け詳細"):
        cohort_file = os.environ.get("EMAIL_RESEARCH_COHORT_FILE", "")
        st.caption(f"対象バッチ: {batch['cohort_size']:,} clinics")
        if cohort_file:
            st.caption(f"cohort file: {cohort_file}")


def _system_status_card():
    """Admin-facing runtime status only -- never the API key or any DB URL."""
    with st.expander("System status（管理者向け）"):
        provider = os.environ.get("EMAIL_SEARCH_PROVIDER", "未設定")
        try:
            from src.master.email_research_config import load_email_research_config
            workers = load_email_research_config(os.environ).workers
        except Exception:
            workers = "-"
        runtime_ready = email_database_status().ready
        cols = st.columns(3)
        cols[0].metric("Provider", provider)
        cols[1].metric("Workers", str(workers))
        cols[2].metric("Runtime", "Ready" if runtime_ready else "未接続")


EMAIL_LISTING_PAGE_SIZE = 100
_HAS_EMAIL_OPTIONS = {"Emailあり": "yes", "Emailなし": "no", "すべて": "all"}


def _email_listing_filters(repo):
    """Filter widgets for the Email listing. Values are passed to SQL; nothing is filtered in Python."""
    values = repo.PRIMARY_LISTING_FILTER_VALUES
    with st.expander("絞り込み", expanded=False):
        first = st.columns(3)
        prefecture = first[0].selectbox("都道府県", [""] + repo.listing_prefectures(),
                                        format_func=lambda v: v or "すべて", key="email_list_prefecture")
        clinic_name = first[1].text_input("医院名（部分一致）", key="email_list_clinic_name")
        has_email = first[2].radio("Email", list(_HAS_EMAIL_OPTIONS), horizontal=True, key="email_list_has_email")
        second = st.columns(3)
        research_status = second[0].multiselect("Research Status", values["research_status"], key="email_list_status")
        auto_decision = second[1].multiselect("AI判定（AUTO_TRUE / AUTO_FALSE / REVIEW）", values["auto_decision"],
                                              key="email_list_decision")
        confidence = second[2].multiselect("Confidence", values["confidence"], key="email_list_confidence")
        third = st.columns(2)
        role = third[0].multiselect("Role", values["role"], key="email_list_role")
        ownership = third[1].multiselect("Ownership", values["ownership"], key="email_list_ownership")
    return {"prefecture": prefecture, "clinic_name": clinic_name, "has_email": _HAS_EMAIL_OPTIONS[has_email],
            "research_status": research_status, "auto_decision": auto_decision, "confidence": confidence,
            "role": role, "ownership": ownership}


def _primary_listing_table(repo):
    st.markdown("### Primary Email一覧")
    filters = _email_listing_filters(repo)
    signature = repr(sorted(filters.items()))
    if st.session_state.get("_email_list_signature") != signature:
        st.session_state["_email_list_signature"] = signature
        st.session_state["email_list_page"] = 1
    result = repo.primary_listing_page(filters=filters, page=st.session_state.get("email_list_page", 1),
                                       page_size=EMAIL_LISTING_PAGE_SIZE)
    st.session_state["email_list_page"] = result["page"]
    nav = st.columns([1, 2, 1])
    if nav[0].button("前へ", key="email_list_prev", disabled=result["page"] <= 1, use_container_width=True):
        st.session_state["email_list_page"] = result["page"] - 1
        st.rerun(scope="fragment")
    nav[1].markdown(f"<div style='text-align:center'>対象: {result['total']:,}件　{result['page']:,} / {result['pages']:,}</div>",
                    unsafe_allow_html=True)
    if nav[2].button("次へ", key="email_list_next", disabled=result["page"] >= result["pages"], use_container_width=True):
        st.session_state["email_list_page"] = result["page"] + 1
        st.rerun(scope="fragment")
    rows = result["rows"]
    if not rows:
        st.caption("条件に一致するClinicはありません。")
        return
    import pandas as pd
    frame = pd.DataFrame([{
        "clinic_id": r["clinic_id"], "医院名": r.get("clinic_name") or "",
        "都道府県": r.get("prefecture") or "", "市区町村": r.get("city") or "",
        "research_status": r.get("research_status") or "",
        "Primary Email": r.get("email") or "", "role": r.get("role") or "",
        "ownership": r.get("ownership") or "", "source_type": r.get("discovery_source") or "",
        "source_url": r.get("source_url") or "", "confidence": r.get("confidence") or "",
        "updated_at": r.get("updated_at") or "",
    } for r in rows])
    st.dataframe(frame, hide_index=True, width="stretch",
                 column_config={"source_url": st.column_config.LinkColumn()})
    with st.expander("Ownership Safety詳細（内部監査向け）"):
        audit_frame = pd.DataFrame([{
            "clinic_id": r["clinic_id"], "ownership": r.get("ownership") or "",
            "email_ownership": r.get("email_ownership") or "",
            "hard_block_codes": ", ".join(r.get("hard_block_codes") or []),
            "final_sales_eligible": r.get("final_sales_eligible"),
        } for r in rows])
        st.dataframe(audit_frame, hide_index=True, width="stretch")


@st.fragment(run_every="5s")
@_fail_soft_on_db_loss
def email_research_page(store):
    st.header("メール調査")
    st.caption("ホームページ自動調査とは独立して動き、HP確認済み・公式URLありのClinicを後から処理します。")
    repositories = email_repositories_for_ui()
    if repositories is None:
        st.warning(email_database_ui_status().message)
        return
    repo = repositories.email_research
    if repo is None or not repo.available():
        st.warning("Email Research schemaは未適用です。Production migrationはこの画面から実行しません。")
        return
    _system_status_card()
    _batch_progress_card(repo)
    st.markdown("### 全期間サマリー（Clinic単位）")
    summary = repo.summary()
    first = st.columns(4)
    first[0].metric("対象", f"{summary['total']:,}件")
    first[1].metric("処理済み", f"{summary['processed']:,}")
    first[2].metric("残り", f"{summary['remaining']:,}")
    first[3].metric("営業利用できるEmail", f"{summary.get('email_obtained', summary['primary']):,}件")
    second = st.columns(4)
    second[0].metric("AI TRUE", f"{summary['true']:,}件")
    second[1].metric("確認待ち候補", f"{summary['candidate_review']:,}件")
    second[2].metric("Emailなし", f"{summary['no_email_found']:,}件")
    second[3].metric("Error", f"{summary['error']:,}件")
    st.caption(f"AUTO_FALSE {summary.get('false', 0):,} / Found {summary.get('found', 0):,}")
    processed = int(summary.get("processed") or 0)
    third = st.columns(4)
    third[0].metric("平均 sec / Clinic", f"{float(summary.get('elapsed_seconds') or 0) / processed:.3f}" if processed else "-")
    third[1].metric("Search Query / Clinic", f"{float(summary.get('search_query_count') or 0) / processed:.3f}" if processed else "-")
    human_repo = repositories.email_human_review
    unreviewed = human_repo.summary()["unreviewed"] if human_repo is not None and human_repo.available() else 0
    third[2].metric("Human Review未処理", f"{unreviewed:,}件")
    zero_rate = (float(summary.get("search_0_count") or 0) / processed * 100) if processed else 0
    third[3].metric("Search 0回率", f"{zero_rate:.1f}%" if processed else "-",
                    help=f"Official HP等で先に候補が見つかり、Search APIを使わなかったClinicの割合です（{summary.get('search_0_count', 0):,}件）。")
    from src.email_human_review_ui import email_human_review_card
    email_human_review_card(store)
    fourth = st.columns(4)
    fourth[0].metric("Search Query", f"{summary.get('search_query_count', 0):,}")
    fourth[1].metric("Query 1 Clinic", f"{summary.get('query1_called', 0):,}")
    fourth[2].metric("Query 2 Clinic", f"{summary.get('query2_called', 0):,}")
    fourth[3].metric("Query上限", "2回 / Clinic", help="1ClinicあたりのSearch Query上限です。")
    fifth = st.columns(5)
    fifth[0].metric("Search API attempts", f"{summary.get('http_attempts', 0):,}")
    fifth[1].metric("Web fetch attempts", f"{summary.get('web_fetch_attempts', 0):,}")
    fifth[2].metric("timeout", f"{summary.get('timeout_count', 0):,}")
    fifth[3].metric("retry", f"{summary.get('retry_count', 0):,}")
    fifth[4].metric("Elapsed sec", f"{float(summary.get('elapsed_seconds') or 0):.1f}")
    audit_csv = Path("artifacts/email_canary_external_100_20261010_results/human_operator_audit.csv")
    if audit_csv.is_file():
        st.download_button(
            "External Canary Human Operator Audit CSVを取得",
            data=audit_csv.read_bytes(), file_name=audit_csv.name, mime="text/csv",
            key="email_external_human_audit_download",
        )
    st.markdown("### Email Auto Research")
    _email_auto_research_controls(store)
    _primary_listing_table(repo)


@_fail_soft_on_db_loss
def email_clinic_status_panel(store, clinic_id):
    repositories = email_repositories_for_ui()
    if repositories is None:
        return
    repo = repositories.email_research
    if repo is None or not repo.available():
        return
    status = repo.clinic_status(clinic_id)
    if not status:
        return
    st.markdown("### Email Research")
    st.write(f"状態: {status['research_status']} / 最終調査: {status.get('researched_at') or '未調査'}")
    if status.get("email"):
        st.write(f"Primary Email: {status['email']}")
        st.caption(f"Primary AI: {status['auto_decision']} / {status['confidence']} / {status['role']} / {status['ownership']}")
        st.caption(f"Primary Source: {status['discovery_source']} / {status['source_url'] or '-'}")
    if status.get("latest_auto_decision"):
        st.caption(f"最新Candidate AI: {status['latest_auto_decision']} / {status['latest_confidence']}")
        st.caption(f"Source: {status['latest_source']} / {status.get('latest_source_url') or '-'}")
    if int(status.get("unreviewed") or 0) > 0:
        if st.button(f"レビューへ ({status['unreviewed']})", key=f"email_review_clinic_{clinic_id}"):
            st.session_state["_email_human_review_clinic_id"] = int(clinic_id)
            st.session_state["_open_email_human_review_dialog"] = True
            st.rerun()
