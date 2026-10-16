"""「メール送付文章」画面（4タブ）。

Clinic Master・Email調査結果・マイナビ記事原本はすべてREAD ONLYで参照する。
Gmail下書き作成・メール送信・DB書き込みは一切行わない（CSVダウンロードのみ）。
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
from src.outreach.staff import load_staff
from src.outreach.targets import DEFAULT_LIMIT, EMAIL_FOUND, LIMIT_OPTIONS, select_targets, valid_email
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
    st.metric("正式なEmail営業利用可能数（動的）", f"{formal_count:,}")
    ledger = None
    ledger_path = (os.environ.get("OUTREACH_STATE_DB_PATH") or "").strip()
    if ledger_path:
        try:
            from src.outreach.history import OutreachLedger
            ledger = OutreachLedger(ledger_path)
            counts = ledger.counts()
            st.metric("下書き済み", counts.get("DRAFTED", 0))
            st.metric("送付済み", counts.get("SENT", 0))
        except Exception:
            st.error("明示設定された別管理の送付履歴台帳を開けません。履歴操作を停止します。")
    else:
        st.caption("送付履歴台帳: 未設定（ローカル自動作成・DBフォールバックなし）。")
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
        try:
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
        except Exception as exc:
            st.error(f"正式Email対象またはClinic Masterの取得に失敗しました。抽出を停止しました（{type(exc).__name__}）。")
            return
        from src.outreach.history import email_collisions, reconcile_history
        blocked = set()
        if history_verified:
            uuid_by_id = {int(record.get("id") or 0): str(record.get("uuid") or "") for record in records}
            blocked = {cid for cid, _email in email_rows
                       if reconcile_history(cid, uuid_by_id.get(cid, ""), campaign_id, history_rows)[0]
                       not in {"NOT_STARTED", "ARTICLE_VERIFIED"}}
            blocked |= email_collisions(email_rows)
            if ledger is not None:
                blocked |= {cid for cid, status in ledger.campaign_statuses(campaign_id).items()
                            if status in {"DRAFTED", "SENT", "REPLY_RECEIVED", "APPOINTMENT", "EXCLUDED", "HOLD", "GENERATED"}}
        else:
            blocked = {cid for cid, _email in email_rows}
        if not campaign_id:
            blocked = set(email_by_id)
        selection = select_targets(records, email_lookup=lambda ids: {cid: email_by_id[cid] for cid in ids if cid in email_by_id},
                                   links=links, limit=int(limit), eligible_clinic_ids=set(email_by_id),
                                   withheld_clinic_ids=blocked)
        st.session_state[K + "selection"] = selection
        st.session_state[K + "requested_limit"] = int(limit)
        st.session_state.pop(K + "results", None)

    selection = st.session_state.get(K + "selection")
    if selection is None:
        return
    requested = st.session_state.get(K + "requested_limit", DEFAULT_LIMIT)
    if not history_verified or not campaign_id:
        st.warning("履歴照合CSVとCampaign IDの両方が必要です。未照合医院は新規候補にしません。")
    st.success(f"対象 {len(selection.targets)}医院（正式Email対象走査 {selection.scanned}件／UUIDなし除外 {selection.skipped_no_uuid}件"
               f"／UUID重複検出 {selection.skipped_duplicate}件{'／上限で打ち切り' if selection.truncated else ''}）")
    if not selection.truncated and len(selection.targets) < requested:
        st.caption(f"指定件数{requested}件に対し、条件に一致する医院は実際には{len(selection.targets)}件でした。")
    st.dataframe(pd.DataFrame([{
        "医院名": t.clinic_name, "Clinic UUID": t.clinic_uuid, "Comdesk UUID": t.comdesk_uuid,
        "都道府県": t.prefecture, "市区町村": t.municipality,
        "標榜科": "、".join(t.departments), "HPランク": t.hp_rank,
        "メール": "取得済み" if t.email_status == EMAIL_FOUND else "未取得",
        "記事": ARTICLE_STATUS_LABELS[t.article.status],
    } for t in selection.targets]), hide_index=True, use_container_width=True)


def _tab_generate():
    selection = st.session_state.get(K + "selection")
    articles = st.session_state.get(K + "articles") or []
    if not selection or not selection.targets:
        st.info("先に「① 対象選択」で対象医院を抽出してください。")
        return
    campaign_id = st.session_state.get(K + "campaign_id", "").strip()
    ledger_path = (os.environ.get("OUTREACH_STATE_DB_PATH") or "").strip()
    if not campaign_id or not ledger_path:
        st.warning("文章生成にはCampaign IDと明示設定された別管理の履歴台帳が必要です。台帳未設定時は重複防止のため生成を停止します。")
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
                 disabled=not (staff or "").strip() or not dates or not campaign_id or not ledger_path):
        settings = GenerationSettings(staff=staff.strip(), dates=tuple(sorted(dates)), start_time=start_time,
                                      end_time=end_time, called_uuids=frozenset(called))
        total = len(selection.targets)
        progress = st.progress(0, text=f"生成中… 0/{total}件")

        def _on_progress(done, total_count):
            progress.progress(done / total_count if total_count else 1.0, text=f"生成中… {done}/{total_count}件")

        results = generate_batch(selection.targets, articles, settings, on_progress=_on_progress)
        progress.empty()
        ledger_path = (os.environ.get("OUTREACH_STATE_DB_PATH") or "").strip()
        if ledger_path:
            try:
                from src.outreach.history import OutreachLedger
                ledger = OutreachLedger(ledger_path)
                for result in results:
                    if result.status != FAILED:
                        ledger.record_generation(clinic_id=result.target.clinic_id,
                                                 clinic_uuid=result.target.clinic_uuid,
                                                 campaign_id=st.session_state.get(K + "campaign_id", "").strip(),
                                                 generation_batch_id=settings.generation_batch_id,
                                                 owner=settings.staff)
            except Exception as exc:
                st.error(f"別管理の履歴台帳への記録に失敗しました。CSVを送付済み扱いにはしません（{type(exc).__name__}）。")
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
    c1.text_input("宛先メールアドレス", r.target.email or "（未取得・要確認）", disabled=True, key=K + f"pv_to_{uid}")
    c2.text_input("IS担当者", settings.staff, disabled=True, key=K + f"pv_staff_{uid}")
    c1.text_input("記事ステータス", ARTICLE_STATUS_LABELS[r.target.article.status], disabled=True, key=K + f"pv_art_{uid}")
    c2.text_input("電話連絡文", "あり（本日電話済み）" if r.called else "なし", disabled=True, key=K + f"pv_call_{uid}")
    if r.recommendations:
        st.markdown("**おすすめ記事（第一推奨＋候補）**")
        for n, rec in enumerate(r.recommendations, 1):
            st.markdown(f"{'🥇 第一推奨' if n == 1 else f'候補{n}'}：{rec.article.title}  \n{rec.article.url}")
            st.caption("選定理由: " + " / ".join(rec.reasons) + f"（score {rec.score}）")
    if r.listed_url:
        st.caption(f"掲載確認済み記事: {r.listed_url}")
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
                     st.session_state.get(K + "campaign_id", "").strip()),
        f"mail_send_text_{today_japan():%Y%m%d}.csv", mime="text/csv",
        key=K + "download", use_container_width=True)
    st.caption("CSV出力のみです。Gmail下書き作成・メール送信は行いません。")
    ledger_path = (os.environ.get("OUTREACH_STATE_DB_PATH") or "").strip()
    if ledger_path:
        st.caption("下書き・送付の状態は、Gmail連携を行わず、別管理の台帳へ担当者が取り込んだ事実として記録します。")
        campaign_id = st.session_state.get(K + "campaign_id", "").strip()
        if campaign_id:
            selected = st.selectbox("履歴を更新する医院", range(len(results)),
                                    format_func=lambda i: f"{results[i].target.clinic_name}（Clinic ID {results[i].target.clinic_id}）",
                                    key=K + "history_target")
            c1, c2 = st.columns(2)
            status = c1.selectbox("送付状態", ["DRAFTED", "SENT", "REPLY_RECEIVED", "APPOINTMENT", "EXCLUDED", "HOLD"], key=K + "history_status")
            owner = c2.text_input("記録担当者", key=K + "history_owner")
            c1.text_input("Gmail Draft ID（手入力）", key=K + "gmail_draft_id")
            c2.text_input("Gmail Message ID（手入力）", key=K + "gmail_message_id")
            sent_at = st.text_input("送付日時（送付済みの場合・手入力）", key=K + "sent_at")
            if st.button("別管理台帳へ状態を記録", key=K + "save_history"):
                result = results[selected]
                try:
                    from src.outreach.history import OutreachLedger
                    OutreachLedger(ledger_path).record_gmail_metadata(
                        clinic_id=result.target.clinic_id, campaign_id=campaign_id, status=status,
                        gmail_draft_id=st.session_state.get(K + "gmail_draft_id", "").strip(),
                        gmail_message_id=st.session_state.get(K + "gmail_message_id", "").strip(),
                        sent_at=sent_at.strip(), owner=owner.strip(), source="manual_ui")
                    st.success("状態を記録しました。Gmailへの接続・送信操作は行っていません。")
                except Exception as exc:
                    st.error(f"履歴を記録できませんでした（{type(exc).__name__}）。")


def outreach_email_page(store, demo=False, *, formal_email_page=None):
    st.header("メール送付文章")
    st.info("Email Research正式利用可の医院を対象に、履歴照合・記事判定・文章生成・プレビューを行います。Gmail下書き作成・メール送信は行いません。")
    tabs = st.tabs(["① 対象選択", "② 文章生成", "③ プレビュー", "④ 生成結果"])
    with tabs[0]:
        _tab_targets(store, demo, formal_email_page=formal_email_page)
    with tabs[1]:
        _tab_generate()
    with tabs[2]:
        _tab_preview()
    with tabs[3]:
        _tab_results()
