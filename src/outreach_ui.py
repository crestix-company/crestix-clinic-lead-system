"""「メール送付文章」画面（4タブ）。

Clinic Master・Email Research・記事原本・Gmail履歴は読み取り専用で照合する。
メール送信・予約送信はなく、明示設定と個別確認後のGmail drafts.createのみ許可する。
送付履歴は専用Supabase roleを使ったテーブル限定操作で記録する。
"""
from __future__ import annotations

import csv
import os
import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path

import pandas as pd
import streamlit as st

from src.outreach.article_adapter import ArticleLink
from src.outreach.article_recommender import Article, read_master
from src.outreach.compose import (
    ARTICLE_LISTED, ARTICLE_NOT_LISTED, ARTICLE_STATUS_LABELS, ARTICLE_UNCONFIRMED,
)
from src.outreach.generation import (
    FAILED, NEEDS_REVIEW, READY, STATUS_LABELS, GenerationSettings, generate_batch,
    summarize, to_csv_bytes,
)
from src.outreach.pre_send_review import build_draft_save_review, configured_i3_reference
from src.outreach.pre_send_ui import render_pre_send_review_gate
from src.outreach.source_import import CSVPreviewError, preview_csv
from src.outreach.legacy_import_preview import (
    LegacyImportInputError, parse_legacy_import_csv, preview_legacy_import,
)
from src.outreach.staff import load_staff
from src.outreach.targets import DEFAULT_LIMIT, EMAIL_FOUND, LIMIT_OPTIONS, select_targets, target_from_record, valid_email
from src.utils.date_utils import today_japan

ARTICLE_MASTER_ENV = "OUTREACH_ARTICLE_MASTER_CSV"
ARTICLE_JOIN_ENV = "OUTREACH_ARTICLE_JOIN_CSV"
ARTICLE_COLUMNS = {"clinic_uuid", "listing_status", "example_article_url", "listing_url",
                   "listing_kind", "source_row", "article_id", "verified"}
HP_RANK_OPTIONS = {"指定なし": [], "A+B": ["A", "B"], "A": ["A"], "B": ["B"], "C": ["C"], "D": ["D"]}
K = "outreach_"  # session_state key prefix (既存画面のキーと衝突させない)


# ---------- READ ONLY data loaders ----------

def _upload_or_env(label, env_var, key):
    """Returns (source, name): source is uploaded bytes or the env-configured Path (read only)."""
    upload = st.file_uploader(label, type=["csv"], key=K + key)
    if upload is not None:
        return upload.getvalue(), upload.name
    path = (os.environ.get(env_var) or "").strip()
    if path and Path(path).is_file():
        return Path(path), Path(path).name
    if path:
        st.warning(f"{env_var} のファイルが見つかりません: {path}")
    return None, ""


def _load_articles(source) -> list[Article]:
    if isinstance(source, Path):
        return read_master(source)  # 原本を読み取りモードで直接読む（書き込み・コピーなし）
    # アップロードはメモリ上のため、読み取り用の一時コピーを作り即削除する
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "article_master.csv"
        path.write_bytes(source)
        return read_master(path)


def _bool(value):
    text = str(value).strip().lower()
    if text not in {"true", "false"}:
        raise ValueError("verified列は true / false のみ許可します")
    return text == "true"


def _load_links(source) -> list[ArticleLink]:
    data = source.read_bytes() if isinstance(source, Path) else source
    reader = csv.DictReader(StringIO(data.decode("utf-8-sig")))
    if not reader.fieldnames or not ARTICLE_COLUMNS.issubset(reader.fieldnames):
        raise ValueError("記事JOIN CSVの必要列がありません: " + ", ".join(sorted(ARTICLE_COLUMNS - set(reader.fieldnames or []))))
    return [ArticleLink(
        clinic_uuid=(r["clinic_uuid"] or "").strip(), listing_status=(r["listing_status"] or "").strip(),
        example_article_url=(r["example_article_url"] or "").strip(), listing_url=(r["listing_url"] or "").strip(),
        listing_kind=(r["listing_kind"] or "").strip(), source_row=(r["source_row"] or "").strip(),
        article_id=(r["article_id"] or "").strip(), verified=_bool(r["verified"]),
        evidence=(r.get("evidence") or "").strip(), checked_at=(r.get("checked_at") or "").strip(),
        processing_version=(r.get("processing_version") or "").strip(),
        article_type=(r.get("article_type") or "UNKNOWN").strip().upper(),
        clinic_profile_url=(r.get("clinic_profile_url") or "").strip(),
    ) for r in reader]


def _prefectures(store):
    if getattr(store, "is_supabase_runtime", False):
        return store.prefectures()
    with store.connect() as c:
        return [r[0] for r in c.execute("SELECT DISTINCT prefecture FROM clinics WHERE prefecture<>'' ORDER BY prefecture")]


def _formal_email_page(page=1, after_clinic_id=None):
    """Canonical formal eligibility only; any connection/query failure stops extraction."""
    st.session_state.pop(K + "email_error", None)
    try:
        from src.repository.email_database import email_repositories_for_ui
        bundle = email_repositories_for_ui()
        repo = bundle.email_research if bundle is not None else None
        if repo is None or not repo.available():
            raise RuntimeError("Email Research未接続")
        return repo.formal_sales_eligible_page(page=page, after_clinic_id=after_clinic_id, page_size=500)
    except Exception as exc:  # Email DB未接続でも画面は止めない（未取得扱い）
        st.session_state[K + "email_error"] = type(exc).__name__
        raise


def _all_formal_email_rows():
    rows, after, page, seen_ids = [], None, 1, set()
    first = _formal_email_page(page, after_clinic_id=after)
    rows.extend(first["rows"])
    seen_ids.update(int(row[0]) for row in first["rows"])
    after = first.get("next_after_clinic_id")
    while len(rows) < int(first["total"]):
        page += 1
        current = _formal_email_page(page, after_clinic_id=after)
        if int(current["total"]) != int(first["total"]):
            raise RuntimeError("Email Researchの対象件数が取得中に変化しました。再抽出してください")
        if not current["rows"] or current.get("next_after_clinic_id") == after:
            raise RuntimeError("Email Researchページング中に対象を取りこぼしました")
        rows.extend(current["rows"])
        after = current["next_after_clinic_id"]
        batch_ids = [int(row[0]) for row in current["rows"]]
        if len(set(batch_ids)) != len(batch_ids) or seen_ids.intersection(batch_ids):
            raise RuntimeError("Email Researchページングで重複clinic_idを検出しました")
        seen_ids.update(batch_ids)
    if len(rows) != int(first["total"]):
        raise RuntimeError("Email Researchの総件数と取得件数が一致しません")
    return int(first["total"]), rows


def _clinic_records_for_ids(store, ids):
    """Fetch eligible clinic IDs in stable ID order through bounded READ ONLY pages."""
    if getattr(store, "is_supabase_runtime", False):
        return store.repositories.clinics.records_by_ids(sorted(ids), page_size=500)
    if hasattr(store, "records_by_ids"):
        return store.records_by_ids(sorted(ids), page_size=500)
    from src.master.filters import Filters
    wanted = set(int(x) for x in ids)
    filters = Filters(active_only=False, hp_only=False)
    found, offset = {}, 0
    while wanted - found.keys():
        page = store.query(filters, limit=500, offset=offset)
        if not page:
            break
        for row in page:
            cid = int(row.get("id") or 0)
            if cid in wanted:
                found[cid] = row
        offset += len(page)
        if len(page) < 500:
            break
    return [found[cid] for cid in sorted(wanted & found.keys())]


# ---------- tabs ----------

def _tab_targets(store, demo, formal_email_page=None):
    formal_email_page = formal_email_page or _formal_email_page
    formal_count = st.session_state.get(K + "formal_count")
    try:
        formal_count = int(formal_email_page(1)["total"])
        st.session_state[K + "formal_count"] = formal_count
    except Exception:
        st.error("Email Researchの正式利用可能数を取得できません。接続・権限・状態を確認するまで抽出を停止します。")
        return
    delivery_ready = False
    delivery_counts = {}
    if not demo:
        repository = None
        try:
            from src.repository.outreach_delivery_repository import OutreachDeliveryRepository
            repository = OutreachDeliveryRepository.connect()
            delivery_counts = repository.summary_counts(jst_today=today_japan())
            delivery_ready = True
        except Exception as exc:
            st.error(f"送付履歴DBを確認できません。履歴不明のため候補抽出・保存は停止します（{type(exc).__name__}）。")
        finally:
            if repository is not None:
                repository.close()
    st.session_state[K + "delivery_ready"] = delivery_ready
    st.metric("正式なEmail営業利用可能数（動的）", f"{formal_count:,}")
    selection = st.session_state.get(K + "selection")
    generated = st.session_state.get(K + "results")
    live_counts = st.session_state.get(K + "gmail_counts", {})
    save_counts = st.session_state.get(K + "draft_counts", {})
    ready_count = sum(result.status == READY for result in generated[1]) if generated else "未生成"
    kpi = st.columns(4)
    kpi[0].metric("対象医院", f"{formal_count:,}")
    kpi[1].metric("作成可能（READY）", ready_count)
    kpi[2].metric("下書きあり", live_counts.get("drafts", "未照合"))
    kpi[3].metric("本日送信済み（JST）", live_counts.get("sent_today", "未照合"))
    second_kpi = st.columns(4)
    second_kpi[0].metric("予約送信あり", live_counts.get("scheduled", "未照合"))
    second_kpi[1].metric("PR・監修除外", live_counts.get("pr_supervised_excluded", "未抽出"))
    second_kpi[2].metric("REVIEW", live_counts.get("review", delivery_counts.get("review", 0)))
    second_kpi[3].metric("履歴DB", "確認済み" if delivery_ready else "未確認")
    third_kpi = st.columns(2)
    third_kpi[0].metric("今回の保存成功", save_counts.get("success", 0))
    third_kpi[1].metric("今回の保存失敗・不明", save_counts.get("failure", 0))
    st.markdown("#### 記事データ（READ ONLY）")
    st.caption(f"アップロードがない場合は環境変数 {ARTICLE_MASTER_ENV} / {ARTICLE_JOIN_ENV} のCSVを読み取ります。原本は変更しません。")
    cols = st.columns(2)
    with cols[0]:
        master_bytes, master_name = _upload_or_env("マイナビ記事原本CSV（No./タイトル/URL/KW）", ARTICLE_MASTER_ENV, "master")
    with cols[1]:
        join_bytes, join_name = _upload_or_env("記事JOIN CSV（Clinic UUID・掲載確認根拠）", ARTICLE_JOIN_ENV, "join")
    try:
        articles = _load_articles(master_bytes) if master_bytes else []
        links = _load_links(join_bytes) if join_bytes else []
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        st.error(f"記事CSVを確認してください: {exc}")
        return
    st.session_state[K + "articles"] = articles
    st.caption(f"記事原本: {len(articles):,}件{f'（{master_name}）' if master_name else '（未読込）'}　"
               f"記事JOIN: {len(links):,}行{f'（{join_name}）' if join_name else '（任意・未読込）'}")
    st.caption("記事あり＝掲載確認済み／記事なし＝未掲載確認済み（いずれも記事JOIN CSVの検証済み行のみ）／"
               "それ以外は未確認。JOIN CSVがなくても、未確認として地域・標榜科目から記事例を選び安全な文面で生成します"
               "（未確認はNEEDS_REVIEW）。JOINに無いだけで記事なしとは判定しません。")

    from src.outreach.history import HistoryInputError, parse_history_csv
    st.markdown("#### 外部履歴（標準CSV契約・未提供は全件保留）")
    st.caption("既存スプレッドシート/Gmailの実データ構造は推測しません。標準列に明示変換した履歴CSVを照合してください。未照合は新規候補にしません。")
    history_upload = st.file_uploader("照合済み履歴CSV（任意・標準列）", type=["csv"], key=K + "history")
    campaign_id = st.text_input("Campaign ID（正確な履歴照合に必要）", key=K + "campaign_id").strip()
    history_rows = []
    history_verified = False
    if history_upload:
        try:
            history_rows = parse_history_csv(history_upload.getvalue())
            history_verified = True
            st.success(f"履歴 {len(history_rows):,}行を標準列で読込済み")
        except HistoryInputError as exc:
            st.error(f"履歴CSVを照合できません: {exc}")
    if history_verified and campaign_id:
        campaign_rows = [row for row in history_rows if row.campaign_id == campaign_id]
        known_statuses = [row.status for row in campaign_rows]
        cols = st.columns(3)
        cols[0].metric("未送付（履歴で未着手）", sum(status in {"NOT_STARTED", "ARTICLE_VERIFIED"} for status in known_statuses))
        cols[1].metric("下書き済み", sum(status == "DRAFTED" for status in known_statuses))
        cols[2].metric("送付済み", sum(status == "SENT" for status in known_statuses))
    st.markdown("#### 抽出条件（正式Email利用可医院から）")
    limit = st.selectbox("抽出件数", (10, 50, 100, 200, 500),
                         index=LIMIT_OPTIONS.index(DEFAULT_LIMIT), key=K + "limit")
    if st.button("対象医院を抽出", type="primary", key=K + "select"):
        if demo or not delivery_ready:
            st.error("サンプル起動中または送付履歴DB未検証のため、送付候補の抽出を停止しました。")
            return
        if not history_verified or not campaign_id:
            st.error("照合済み履歴CSVとCampaign IDが必要です。未照合の医院を候補にしません。")
            return
        try:
            # Gmail state must be readable before extracting new-draft candidates.
            from src.outreach.gmail_oauth import connect_existing
            from src.outreach.gmail_workflow import read_gmail_state, classify_target_state
            gmail_connection = connect_existing(root=Path.cwd())
            gmail_state = read_gmail_state(gmail_connection.service, as_of=today_japan())
            first = formal_email_page(1, after_clinic_id=None)
            email_rows = list(first["rows"])
            seen_ids = {int(row[0]) for row in first["rows"]}
            after = first.get("next_after_clinic_id")
            while len(email_rows) < int(first["total"]):
                page_num = len(email_rows) // max(1, int(first["page_size"])) + 1
                page_data = formal_email_page(page_num, after_clinic_id=after)
                if int(page_data["total"]) != int(first["total"]):
                    raise RuntimeError("Email Researchの対象件数が取得中に変化しました")
                email_rows.extend(page_data["rows"])
                if not page_data["rows"] or page_data.get("next_after_clinic_id") == after:
                    raise RuntimeError("Email Researchページングで取りこぼしが発生しました")
                after = page_data["next_after_clinic_id"]
                batch_ids = [int(row[0]) for row in page_data["rows"]]
                if len(set(batch_ids)) != len(batch_ids) or seen_ids.intersection(batch_ids):
                    raise RuntimeError("Email Researchページングで重複clinic_idを検出しました")
                seen_ids.update(batch_ids)
            if len(email_rows) != int(first["total"]):
                raise RuntimeError("Email Researchの総件数と取得件数が一致しません")
            total = int(first["total"])
            email_by_id = {cid: email for cid, email in email_rows if valid_email(email)}
            records = _clinic_records_for_ids(store, email_by_id)
            from src.repository.outreach_delivery_repository import OutreachDeliveryRepository
            delivery_repository = OutreachDeliveryRepository.connect()
            try:
                campaign_rows = delivery_repository.campaign_rows(campaign_id)
            finally:
                delivery_repository.close()
        except Exception as exc:
            st.error(f"Email/Clinic/Gmail/送付履歴の照合を完了できません。候補抽出を停止しました（{type(exc).__name__}）。")
            return
        from src.outreach.history import email_collisions, reconcile_history
        uuid_by_id = {int(record.get("id") or 0): str(record.get("uuid") or "") for record in records}
        blocked = set()
        history_review = set()
        history_reasons = {}
        for cid, _email in email_rows:
            history_status, match_reason = reconcile_history(
                cid, uuid_by_id.get(cid, ""), campaign_id, history_rows)
            if history_status not in {"NOT_STARTED", "ARTICLE_VERIFIED"}:
                blocked.add(int(cid))
                history_reasons[int(cid)] = match_reason
                if history_status in {"HOLD", "REVIEW", "UNKNOWN", "NEEDS_REVIEW"}:
                    history_review.add(int(cid))
        gmail_states, exclusion_reasons = {}, dict(history_reasons)
        shared_email_ids = email_collisions(email_rows)
        blocked |= shared_email_ids
        for cid in shared_email_ids:
            exclusion_reasons[int(cid)] = "同一メールアドレスを複数医院で共有"
        ledger_ids = {int(row["clinic_id"]) for row in campaign_rows}
        blocked |= ledger_ids
        for cid in ledger_ids:
            exclusion_reasons[int(cid)] = "同一Campaignの送付履歴あり"
        by_id = {int(record.get("id") or 0): record for record in records}
        pr_supervised_excluded = set()
        for clinic_id, email in email_rows:
            record = by_id.get(int(clinic_id))
            if record is None or clinic_id not in email_by_id:
                blocked.add(int(clinic_id))
                exclusion_reasons[int(clinic_id)] = "Clinic Master/email不一致"
                continue
            target = target_from_record(record, email=email_by_id[int(clinic_id)], links=links)
            state = classify_target_state(target, state=gmail_state)
            gmail_states[int(clinic_id)] = state
            if state != "CLEAR":
                blocked.add(int(clinic_id))
                exclusion_reasons[int(clinic_id)] = state
            if target.article.status == ARTICLE_LISTED and target.article.article_type in {"PR", "SUPERVISED"}:
                blocked.add(int(clinic_id))
                pr_supervised_excluded.add(int(clinic_id))
                exclusion_reasons[int(clinic_id)] = "PR/監修記事のため除外"
        selection = select_targets(records, email_lookup=lambda ids: {cid: email_by_id[cid] for cid in ids if cid in email_by_id},
                                   links=links, limit=int(limit), eligible_clinic_ids=set(email_by_id),
                                   withheld_clinic_ids=blocked)
        st.session_state[K + "selection"] = selection
        st.session_state[K + "gmail_states"] = gmail_states
        st.session_state[K + "exclusion_reasons"] = exclusion_reasons
        withheld_rows = []
        selected_ids = {target.clinic_id for target in selection.targets}
        for cid, email in email_rows:
            if int(cid) in selected_ids:
                continue
            record = by_id.get(int(cid))
            if not record:
                continue
            target = target_from_record(record, email=email_by_id.get(int(cid), ""), links=links)
            withheld_rows.append({
                "clinic_id": target.clinic_id, "Clinic UUID": target.clinic_uuid,
                "医院名": target.clinic_name, "住所": target.clinic_address,
                "宛先メール": target.email, "Gmail状態": gmail_states.get(target.clinic_id, "未照合"),
                "判定": exclusion_reasons.get(target.clinic_id, "履歴照合保留"),
            })
        st.session_state[K + "withheld_rows"] = withheld_rows
        st.session_state[K + "gmail_counts"] = {
            "drafts": sum(value == "DRAFT_EXISTS" for value in gmail_states.values()),
            "sent_today": sum(value == "SENT_TODAY" for value in gmail_states.values()),
            "scheduled": sum(value == "SCHEDULED_EXISTS" for value in gmail_states.values()),
            "pr_supervised_excluded": len(pr_supervised_excluded),
            "review": len(history_review | {cid for cid, reason in exclusion_reasons.items()
                                            if reason in {"Clinic Master/email不一致", "未照合", "REVIEW"}}),
        }
        st.session_state[K + "gmail_history_verified"] = True
        st.session_state[K + "requested_limit"] = int(limit)
        st.session_state.pop(K + "results", None)

    selection = st.session_state.get(K + "selection")
    if selection is None:
        return
    requested = st.session_state.get(K + "requested_limit", DEFAULT_LIMIT)
    if not history_verified or not campaign_id:
        st.warning("履歴照合CSVとCampaign IDの両方が必要です。未照合医院は新規候補にしません。")
    gmail_states = st.session_state.get(K + "gmail_states", {})
    st.success(f"対象 {len(selection.targets)}医院（正式Email対象走査 {selection.scanned}件／UUIDなし除外 {selection.skipped_no_uuid}件"
               f"／UUID重複検出 {selection.skipped_duplicate}件{'／上限で打ち切り' if selection.truncated else ''}）")
    if not selection.truncated and len(selection.targets) < requested:
        st.caption(f"指定件数{requested}件に対し、条件に一致する医院は実際には{len(selection.targets)}件でした。")
    st.dataframe(pd.DataFrame([{
        "clinic_id": t.clinic_id, "Clinic UUID": t.clinic_uuid, "Comdesk UUID": t.comdesk_uuid,
        "医院名": t.clinic_name, "住所": t.clinic_address,
        "院長名": t.director_name, "宛先メール": t.email,
        "都道府県": t.prefecture, "市区町村": t.municipality,
        "標榜科": "、".join(t.departments), "HPランク": t.hp_rank,
        "掲載判定": ARTICLE_STATUS_LABELS[t.article.status],
        "記事種別": t.article.article_type,
        "記事例URL": t.article.example_url, "実掲載記事URL": t.article.listed_url,
        "医院詳細URL": t.article.clinic_profile_url,
        "Gmail状態": gmail_states.get(t.clinic_id, "未照合"),
        "送付判定": exclusion_reasons.get(t.clinic_id, "候補"),
    } for t in selection.targets]), hide_index=True, use_container_width=True)
    withheld_rows = st.session_state.get(K + "withheld_rows", [])
    if withheld_rows:
        with st.expander(f"除外・照合保留 {len(withheld_rows):,}医院の理由"):
            st.dataframe(pd.DataFrame(withheld_rows), hide_index=True, use_container_width=True)


def _tab_generate():
    selection = st.session_state.get(K + "selection")
    articles = st.session_state.get(K + "articles") or []
    if not selection or not selection.targets:
        st.info("先に「① 対象選択」で対象医院を抽出してください。")
        return
    campaign_id = st.session_state.get(K + "campaign_id", "").strip()
    if not campaign_id or not st.session_state.get(K + "delivery_ready"):
        st.warning("Campaign IDまたは送付履歴DBが未確認です。候補生成を停止します。")
    staff_config = load_staff()
    for error in staff_config.errors:
        st.warning(f"IS担当者設定: {error}")
    if not staff_config.names:
        st.caption(f"IS担当者は {staff_config.path.name} に登録すると選択肢に表示されます（コード修正不要）。")
    choice = st.selectbox("IS担当者", list(staff_config.names) + ["（手入力）"], key=K + "staff_choice")
    staff = st.text_input("IS担当者名（手入力）", key=K + "staff_manual") if choice == "（手入力）" else choice

    st.markdown("##### 日程候補")
    c1, c2, c3 = st.columns(3)
    start = c1.date_input("開始日", value=today_japan() + timedelta(days=2), key=K + "start")
    start_time = c2.text_input("開始時刻", value="9:00", key=K + "start_time")
    end_time = c3.text_input("終了時刻", value="21:00", key=K + "end_time")
    weekdays, cursor = [], start
    while len(weekdays) < 10:
        if cursor.weekday() < 5:
            weekdays.append(cursor)
        cursor += timedelta(days=1)
    dates = st.multiselect("候補日（平日）", weekdays, default=weekdays[:5], key=K + "dates",
                           format_func=lambda d: f"{d.month}/{d.day}（{'月火水木金土日'[d.weekday()]}）")
    calendar_confirmed = st.checkbox(
        "候補日時を髙原のGoogleカレンダーまたは本人予定と照合済み",
        key=K + "calendar_confirmed",
    )
    provisional_approved = st.checkbox(
        "関連記事紹介型テンプレートをGmail保存前の最終確認済み",
        key=K + "provisional_approved",
    )

    st.markdown("##### 電話連絡の事実")
    st.caption("ここで選択した医院だけに「本日お電話させていただき…」の文を入れます。未選択の医院には記載しません。")
    labels = {t.clinic_uuid: f"{t.clinic_name}（{t.clinic_uuid[:8]}…）" for t in selection.targets}
    called = st.multiselect("本日電話し、メール送付を案内された医院", list(labels), format_func=labels.get, key=K + "called")

    counts = {s: sum(t.article.status == s for t in selection.targets) for s in ARTICLE_STATUS_LABELS}
    st.caption("記事判定: " + "　".join(f"{ARTICLE_STATUS_LABELS[s]} {n}件" for s, n in counts.items())
               + f"　｜記事原本 {len(articles):,}件から第一推奨1件＋候補最大3件を選定")
    if not articles:
        st.warning("記事原本CSVが未読込です。推奨記事を選定できないため、記事あり以外はFAILEDになります。")
    if st.button(f"{len(selection.targets)}医院の文章を一括生成", type="primary", key=K + "generate",
                 disabled=not (staff or "").strip() or not dates or not campaign_id
                 or not st.session_state.get(K + "delivery_ready")
                 or not st.session_state.get(K + "gmail_history_verified")):
        settings = GenerationSettings(staff=staff.strip(), dates=tuple(sorted(dates)), start_time=start_time,
                                      end_time=end_time, called_uuids=frozenset(called),
                                      calendar_confirmed=calendar_confirmed,
                                      provisional_template_approved=provisional_approved)
        total = len(selection.targets)
        progress = st.progress(0, text=f"生成中… 0/{total}件")

        def _on_progress(done, total_count):
            progress.progress(done / total_count if total_count else 1.0, text=f"生成中… {done}/{total_count}件")

        results = generate_batch(selection.targets, articles, settings, on_progress=_on_progress)
        progress.empty()
        st.session_state[K + "results"] = (settings, results)
        st.success("生成しました。「③ プレビュー」「④ 生成結果」で確認してください。")
    if not (staff or "").strip():
        st.caption("IS担当者を指定すると生成できます。")


def _highlight(row):
    color = {"要確認": "background-color: #fff3cd", "失敗": "background-color: #f8d7da"}.get(row["判定"], "")
    return [color] * len(row)


def _tab_preview():
    stored = st.session_state.get(K + "results")
    if not stored:
        st.info("「② 文章生成」で一括生成すると表示されます。")
        return
    settings, results = stored
    status_filter = st.multiselect("判定で絞り込み", [READY, NEEDS_REVIEW, FAILED], default=[READY, NEEDS_REVIEW, FAILED],
                                   format_func=STATUS_LABELS.get, key=K + "preview_filter")
    shown = [r for r in results if r.status in status_filter]
    frame = pd.DataFrame([{
        "判定": STATUS_LABELS[r.status], "医院名": r.target.clinic_name, "Clinic UUID": r.target.clinic_uuid,
        "Comdesk UUID": r.target.comdesk_uuid,
        "宛先": r.target.email or "（未取得）", "記事": ARTICLE_STATUS_LABELS[r.target.article.status],
        "おすすめ記事": r.first.article.title if r.first else "", "要確認理由": " / ".join(r.reasons),
    } for r in shown])
    if frame.empty:
        st.caption("該当なし")
        return
    st.dataframe(frame.style.apply(_highlight, axis=1), hide_index=True, use_container_width=True)
    index = st.selectbox("詳細を表示する医院", range(len(shown)), key=K + "preview_index",
                         format_func=lambda i: f"[{STATUS_LABELS[shown[i].status]}] {shown[i].target.clinic_name}")
    r = shown[index]
    if r.status == NEEDS_REVIEW:
        st.warning("要確認: " + " / ".join(r.reasons))
    elif r.status == FAILED:
        st.error("失敗: " + " / ".join(r.reasons))
    else:
        st.success("READY")
    uid = r.target.clinic_uuid
    c1, c2 = st.columns(2)
    c1.text_input("医院名", r.target.clinic_name, disabled=True, key=K + f"pv_name_{uid}")
    c2.text_input("Clinic UUID", uid, disabled=True, key=K + f"pv_uuid_{uid}")
    c1.text_input("Comdesk UUID", r.target.comdesk_uuid or "（なし）", disabled=True, key=K + f"pv_comdesk_{uid}")
    c2.text_input("住所", r.target.clinic_address or "（未取得）", disabled=True, key=K + f"pv_address_{uid}")
    c1.text_input("宛先メールアドレス", r.target.email or "（未取得・要確認）", disabled=True, key=K + f"pv_to_{uid}")
    c2.text_input("IS担当者", settings.staff, disabled=True, key=K + f"pv_staff_{uid}")
    c1.text_input("院長名", r.target.director_name or "（未確認）", disabled=True, key=K + f"pv_director_{uid}")
    c2.text_input("記事種別", r.target.article.article_type, disabled=True, key=K + f"pv_article_type_{uid}")
    c1.text_input("記事ステータス", ARTICLE_STATUS_LABELS[r.target.article.status], disabled=True, key=K + f"pv_art_{uid}")
    c2.text_input("電話連絡文", "あり（本日電話済み）" if r.called else "なし", disabled=True, key=K + f"pv_call_{uid}")
    if r.recommendations:
        st.markdown("**おすすめ記事（第一推奨＋候補）**")
        for n, rec in enumerate(r.recommendations, 1):
            st.markdown(f"{'🥇 第一推奨' if n == 1 else f'候補{n}'}：{rec.article.title}  \n{rec.article.url}")
            st.caption("選定理由: " + " / ".join(rec.reasons) + f"（score {rec.score}）")
    if r.listed_url:
        st.caption(f"掲載確認済み記事: {r.listed_url}")
    if r.target.article.clinic_profile_url:
        st.caption(f"医院詳細ページ: {r.target.article.clinic_profile_url}")
    st.text_input("件名", r.subject, disabled=True, key=K + f"pv_subject_{uid}")
    st.text_area("メール本文", r.body, height=520, disabled=True, key=K + f"pv_body_{uid}")


def _tab_results():
    stored = st.session_state.get(K + "results")
    if not stored:
        st.info("「② 文章生成」で一括生成すると表示されます。")
        return
    settings, results = stored
    s = summarize(results)
    cols = st.columns(4)
    cols[0].metric("生成対象数", s["total"])
    cols[1].metric("READY", s[READY])
    cols[2].metric("NEEDS_REVIEW", s[NEEDS_REVIEW])
    cols[3].metric("FAILED", s[FAILED])
    issues = [r for r in results if r.status != READY]
    if issues:
        st.markdown("##### 医院別エラー・要確認理由")
        st.dataframe(pd.DataFrame([{
            "判定": r.status, "医院名": r.target.clinic_name, "Clinic UUID": r.target.clinic_uuid,
            "理由": " / ".join(r.reasons),
        } for r in issues]), hide_index=True, use_container_width=True)
    st.download_button(
        "CSVダウンロード（医院・記事・宛先・件名・本文・判定）",
        to_csv_bytes(results, settings.staff, settings.generation_batch_id,
                     st.session_state.get(K + "campaign_id", "").strip(),
                     calendar_confirmed=settings.calendar_confirmed),
        f"mail_send_text_{today_japan():%Y%m%d}.csv", mime="text/csv",
        key=K + "download", use_container_width=True)
    st.caption("メール送信・予約送信は実装していません。Gmail下書き保存はデフォルト無効で、環境設定・個別確認・Gmail/DB照合を通過した場合だけ実行できます。")
    from src.outreach.gmail_workflow import create_gmail_draft, gmail_drafts_enabled
    draft_enabled = gmail_drafts_enabled()
    i3_reference = configured_i3_reference()
    if not draft_enabled:
        st.info("Gmail下書き保存は無効です（OUTREACH_GMAIL_DRAFTS_ENABLED=false）。")
    if not i3_reference.configured:
        st.info("I3正式定義は未確定です。下書き保存とは別の送信前確認・承認は未完了のままです。")
    else:
        st.caption(f"送信前確認で参照するI3定義：{i3_reference.definition_id}")
    ready_options = [i for i, result in enumerate(results) if result.status == READY]
    selected_drafts = st.multiselect(
        "下書き保存対象（READYのみ・最大25医院）", ready_options,
        format_func=lambda i: f"Clinic {results[i].target.clinic_id}｜{results[i].target.clinic_name}",
        key=K + "draft_selection",
    )
    reviewer = st.text_input("下書き保存確認担当者", key=K + "reviewer") if selected_drafts else ""
    review_payloads = {}
    review_blockers_by_index = {}
    for i in selected_drafts:
        result = results[i]
        clinic_key = str(result.target.clinic_id)
        with st.expander(f"保存前プレビュー：{result.target.clinic_name}（Clinic ID {clinic_key}）", expanded=True):
            st.text_input("医院名", result.target.clinic_name, disabled=True, key=K + f"draft_clinic_{clinic_key}")
            st.text_input("院長名", result.target.director_name or "（未確認）", disabled=True, key=K + f"draft_director_{clinic_key}")
            st.text_input("宛先", result.target.email, disabled=True, key=K + f"draft_to_{clinic_key}")
            st.text_input("件名", result.subject, disabled=True, key=K + f"draft_subject_{clinic_key}")
            st.text_area("本文（正式テンプレート）", result.body, height=260, disabled=True, key=K + f"draft_body_{clinic_key}")
            article_url = result.listed_url or result.example_url or (result.first.article.url if result.first else "")
            st.text_input("実掲載記事または類似記事URL", article_url or "（未確認）", disabled=True,
                          key=K + f"draft_article_{clinic_key}")
            profile_url = result.target.article.clinic_profile_url
            st.text_input("医院詳細ページURL", profile_url or "（未取得）", disabled=True,
                          key=K + f"draft_profile_{clinic_key}")
            operation_key = st.text_input(
                "案件ID / operation_key（Sheets連携値があればその値）",
                value=f"clinic:{clinic_key}:campaign:{st.session_state.get(K + 'campaign_id', '').strip()}",
                key=K + f"operation_key_{clinic_key}",
            ).strip()
            gmail_state = st.session_state.get(K + "gmail_states", {}).get(result.target.clinic_id, "未照合")
            st.caption(f"Gmail重複照合状態：{gmail_state}。下書き保存時にもGmailと案件台帳を再照合します。")
            duplicate_clear = st.checkbox(
                "既存下書き・送信予約・送信済みとの重複がないことを確認しました",
                disabled=gmail_state != "CLEAR",
                key=K + f"review_duplicate_{clinic_key}",
            )
            checks = {
                "clinic_identity": st.checkbox("医院名・院長名・宛先を照合しました", key=K + f"review_identity_{clinic_key}"),
                "template_body": st.checkbox("正式テンプレート本文を全文確認しました", key=K + f"review_body_{clinic_key}"),
                "article_reference": st.checkbox("実掲載記事または類似記事URLを確認しました", key=K + f"review_article_{clinic_key}"),
                "clinic_detail_page": st.checkbox("医院詳細ページURLを確認しました", key=K + f"review_profile_{clinic_key}"),
                "duplicate_clear": duplicate_clear,
            }
            payload, blockers = build_draft_save_review(
                result=result, reviewer=reviewer, checks=checks,
                operation_key=operation_key,
                campaign_id=st.session_state.get(K + "campaign_id", "").strip(),
            )
            review_payloads[i] = payload
            review_blockers_by_index[i] = blockers
            if blockers:
                st.warning("保存前確認が未完了：" + " / ".join(blockers))
            else:
                st.success(f"下書き保存確認者：{payload['reviewed_by']}｜確認日時：{payload['reviewed_at']}")
            if not i3_reference.configured:
                st.info("I3正式定義は未確定です。下書き保存とは分離した送信前確認は未承認のままです。Clinic Leadから送信・予約送信はできません。")
            else:
                st.caption(f"I3送信前確認の参照候補：{i3_reference.definition_id}（送信判断はGmailでISリーダーが実施）")
    all_reviewed = bool(selected_drafts) and all(not review_blockers_by_index.get(i) for i in selected_drafts)
    typed_confirmation = st.text_input("下書き保存の確認語", key=K + "draft_confirmation")
    st.caption("確認語：CREATE_GMAIL_DRAFTS。最大25件を順に保存し、曖昧な結果は自動再送しません。")
    if st.button("Gmail下書きに保存", key=K + "create_drafts", type="primary",
                 disabled=(not draft_enabled or not selected_drafts or len(selected_drafts) > 25
                           or not all_reviewed or typed_confirmation != "CREATE_GMAIL_DRAFTS")):
        repository = None
        try:
            from src.outreach.gmail_oauth import connect_existing
            from src.repository.outreach_delivery_repository import OutreachDeliveryRepository
            campaign_id = st.session_state.get(K + "campaign_id", "").strip()
            gmail = connect_existing(root=Path.cwd())
            repository = OutreachDeliveryRepository.connect()
            if not repository.health_check():
                raise RuntimeError("Outreach DB health check failed")
            outcomes = []
            for i in selected_drafts:
                outcomes.append(create_gmail_draft(
                    results[i], campaign_id=campaign_id,
                    generation_batch_id=settings.generation_batch_id,
                    staff_name=settings.staff, gmail=gmail, repository=repository,
                    as_of=today_japan(), enabled=draft_enabled,
                    draft_save_review=review_payloads[i],
                ))
            counts = {}
            for outcome in outcomes:
                counts[outcome.status] = counts.get(outcome.status, 0) + 1
            st.session_state[K + "draft_outcomes"] = outcomes
            st.session_state[K + "draft_result_by_outreach"] = {
                outcome.outreach_id: results[i] for i, outcome in zip(selected_drafts, outcomes)
                if outcome.outreach_id
            }
            st.session_state[K + "draft_counts"] = {
                "success": sum(outcome.status in {"DRAFT_CREATED", "DRAFT_RECOVERED"} for outcome in outcomes),
                "failure": sum(outcome.status not in {"DRAFT_CREATED", "DRAFT_RECOVERED"} for outcome in outcomes),
            }
            st.success("下書き処理結果：" + "、".join(f"{status} {count}件" for status, count in sorted(counts.items())))
            if any(outcome.status == "UNKNOWN" for outcome in outcomes):
                st.warning("結果不明の医院があります。Gmailと送付履歴DBを手動照合するまで再実行しないでください。")
        except Exception as exc:
            st.error(f"Gmail下書き保存を安全に完了できませんでした（{type(exc).__name__}）。")
        finally:
            if repository is not None:
                repository.close()
    if st.session_state.get(K + "draft_outcomes"):
        st.dataframe(pd.DataFrame([{
            "状態": outcome.status, "outreach_id": outcome.outreach_id,
            "Draft ID": outcome.gmail_draft_id, "Message ID": outcome.gmail_message_id,
            "Thread ID": outcome.gmail_thread_id, "理由": outcome.reason,
        } for outcome in st.session_state[K + "draft_outcomes"]]), hide_index=True, use_container_width=True)
        result_by_outreach = st.session_state.get(K + "draft_result_by_outreach", {})
        for outcome in st.session_state[K + "draft_outcomes"]:
            result = result_by_outreach.get(outcome.outreach_id)
            if result:
                with st.expander(f"送信前確認：{result.target.clinic_name}", expanded=False):
                    render_pre_send_review_gate(
                        result=result, outcome=outcome,
                        campaign_id=st.session_state.get(K + "campaign_id", "").strip(),
                        key=K + "send_review_" + outcome.outreach_id,
                    )


def _tab_cases(demo):
    st.markdown("#### 営業案件・Gmail同期状況")
    campaign_id = st.session_state.get(K + "campaign_id", "").strip()
    if demo or not campaign_id or not st.session_state.get(K + "delivery_ready"):
        st.info("案件台帳のREAD ONLY確認には、履歴DB接続・Campaign IDが必要です。")
        return
    repository = None
    try:
        from src.repository.outreach_delivery_repository import OutreachDeliveryRepository
        repository = OutreachDeliveryRepository.connect()
        rows = repository.campaign_rows(campaign_id)
        event_counts = repository.clinic_contact_event_counts(campaign_id)
        history_by_clinic = repository.clinic_statuses([int(row["clinic_id"]) for row in rows])
    except Exception as exc:
        st.error(f"案件・同期状況を読み取れません。書込みは行いません（{type(exc).__name__}）。")
        return
    finally:
        if repository is not None:
            repository.close()
    st.caption("既存フォーム・GAS・Gmailはそのままです。この画面はClinic Leadの送付台帳をREAD ONLY表示します。")
    if not rows:
        st.info("このCampaign IDの保存済み案件はありません。")
        return
    stored = st.session_state.get(K + "results")
    by_clinic = {r.target.clinic_id: r for r in stored[1]} if stored else {}
    visible = []
    for row in rows:
        result = by_clinic.get(int(row["clinic_id"]))
        visible.append({
            "Clinic ID": row["clinic_id"], "Clinic UUID": row["clinic_uuid"],
            "医院名": result.target.clinic_name if result else row.get("clinic_name_snapshot", ""),
            "宛先": row.get("recipient_email", ""), "担当IS": row.get("staff_name", ""),
            "担当FS": "未同期", "記事URL": ((result.listed_url or result.example_url) if result else ""),
            "記事判定": (result.target.article.status if result else row.get("listing_status", "未照合")),
            "Gmail状態": row["status"], "Draft ID": row.get("gmail_draft_id", ""),
            "送信済みID": row.get("gmail_message_id", ""),
            "返信状態": "返信あり" if row["status"] == "REPLIED" else "未確認",
            "開封推定イベント": event_counts.get(int(row["clinic_id"]), {}).get("OPEN", 0)
                + event_counts.get(int(row["clinic_id"]), {}).get("OPENED", 0),
            "クリックイベント": event_counts.get(int(row["clinic_id"]), {}).get("CLICK", 0)
                + event_counts.get(int(row["clinic_id"]), {}).get("CLICKED", 0),
            "アポイント": "Calendar/Sheets同期未導入",
            "Sheets/Calendar同期": "未接続",
            "DB同期": "Clinic Lead台帳から取得",
            "同期エラー": row.get("last_error", "") or "なし",
        })
    st.dataframe(pd.DataFrame(visible), hide_index=True, use_container_width=True)
    selected_id = st.selectbox("医院・過去案件の詳細", [int(r["clinic_id"]) for r in rows],
                               format_func=lambda cid: next(
                                   (f"{item['医院名']}｜Clinic {cid}" for item in visible if item["Clinic ID"] == cid), str(cid)),
                               key=K + "case_detail")
    selected_row = next(row for row in rows if int(row["clinic_id"]) == selected_id)
    st.json({key: selected_row.get(key) for key in (
        "clinic_id", "clinic_uuid", "campaign_id", "status", "gmail_draft_id",
        "gmail_message_id", "gmail_thread_id", "sent_at", "scheduled_at", "last_synced_at", "last_error",
    )})
    st.markdown("**同一医院の過去案件（全Campaign）**")
    history = history_by_clinic.get(selected_id, [])
    if history:
        st.dataframe(pd.DataFrame(history), hide_index=True, use_container_width=True)
    else:
        st.caption("過去案件なし")


def _tab_import_preview():
    st.markdown("#### Google Sheets / CSV取込プレビュー（ローカル・書込みなし）")
    st.caption("全元列・元値・行順を保ったローカルプレビューです。Clinic ID・Clinic UUID・案件ID・operation_keyは候補として表示し、重複行・履歴行・同一医院の複数案件を統合しません。")
    uploaded = st.file_uploader("変換前の案件・履歴CSV", type=["csv"], key=K + "source_csv_preview")
    if uploaded is None:
        return
    try:
        preview = preview_csv(uploaded.getvalue())
    except CSVPreviewError as exc:
        st.error(str(exc))
        return
    try:
        legacy_rows = parse_legacy_import_csv(uploaded.getvalue())
        legacy = preview_legacy_import(legacy_rows)
    except LegacyImportInputError:
        legacy = None
    if legacy is not None:
        st.markdown("**既存328医院CSV照合（既存プレビュー機能を再利用）**")
        st.caption(
            f"行数 {legacy.total_rows}｜Clinic ID重複 {legacy.duplicate_clinic_ids}｜"
            f"UUID重複 {legacy.duplicate_clinic_uuids}｜ID/UUID/医院名/住所不足 {legacy.incomplete_identity_rows}"
        )
        st.json({"記事判定": legacy.listing_status_counts, "Gmail状態": legacy.gmail_state_counts,
                 "期待行数328": legacy.expected_count_matches,
                 "記事判定件数一致": legacy.status_count_matches})
    st.metric("取込候補行（ヘッダー除外）", f"{len(preview.rows):,}")
    duplicate_count = len(preview.row_hashes) - len(set(preview.row_hashes))
    st.caption(f"元列数 {len(preview.headers)}｜行幅不一致 {len(preview.ragged_rows)}｜同一行hash {duplicate_count}件（自動統合なし）")
    st.dataframe(pd.DataFrame([{
        "元列名": header,
        "候補型": ", ".join(f"{kind}:{count}" for kind, count in counts.items() if kind != "empty") or "empty",
    } for header, counts in zip(preview.headers, preview.type_counts)]), hide_index=True, use_container_width=True)
    st.json({key: values for key, values in preview.key_suggestions.items() if values})
    st.dataframe(pd.DataFrame(preview.rows[:25], columns=list(preview.headers)), hide_index=True, use_container_width=True)
    st.warning("プレビューのみです。取込履歴への保存・Sheets書込み・DB書込み・自動紐付けは実行しません。")


def outreach_email_page(store, demo=False, *, formal_email_page=None):
    st.header("メール送付文章")
    st.info("Email Research正式利用可の医院を対象に、履歴照合・記事判定・文章生成・プレビューを行います。メール送信・予約送信は行いません。")
    tabs = st.tabs(["① 対象選択", "② 文章生成", "③ プレビュー", "④ 生成結果", "⑤ 下書き一括保存", "⑥ 案件・状態", "⑦ 取込プレビュー"])
    with tabs[0]:
        _tab_targets(store, demo, formal_email_page=formal_email_page)
    with tabs[1]:
        _tab_generate()
    with tabs[2]:
        _tab_preview()
    with tabs[3]:
        _tab_results()
    with tabs[4]:
        from src.outreach.auto_draft_ui import auto_draft_save_page
        auto_draft_save_page(demo=demo)
    with tabs[5]:
        _tab_cases(demo)
    with tabs[6]:
        _tab_import_preview()
