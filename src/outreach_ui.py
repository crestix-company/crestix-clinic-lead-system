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
from src.outreach.targets import DEFAULT_LIMIT, EMAIL_FOUND, LIMIT_OPTIONS, select_targets
from src.utils.date_utils import today_japan

ARTICLE_MASTER_ENV = "OUTREACH_ARTICLE_MASTER_CSV"
ARTICLE_JOIN_ENV = "OUTREACH_ARTICLE_JOIN_CSV"
ARTICLE_COLUMNS = {"clinic_uuid", "listing_status", "example_article_url", "listing_url",
                   "listing_kind", "source_row", "article_id", "verified"}
HP_RANK_OPTIONS = {"指定なし": [], "A+B": ["A", "B"], "A": ["A"], "B": ["B"], "C": ["C"], "D": ["D"]}
SCAN_POOL = 300  # 上限医院数ぶんの走査余地（重複・フィルタ除外を見込んで最低4倍は確保する）
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
    ) for r in reader]


def _prefectures(store):
    if getattr(store, "is_supabase_runtime", False):
        return store.prefectures()
    with store.connect() as c:
        return [r[0] for r in c.execute("SELECT DISTINCT prefecture FROM clinics WHERE prefecture<>'' ORDER BY prefecture")]


def _email_lookup(ids):
    """Email調査のPrimary Email（Ownership guard通過分のみ）をREAD ONLYで1クエリ取得。"""
    st.session_state.pop(K + "email_error", None)
    if not ids:
        return {}
    try:
        from src.repository.email_database import email_repositories_for_ui
        bundle = email_repositories_for_ui()
        repo = bundle.email_research if bundle is not None else None
        if repo is None or not repo.available():
            st.session_state[K + "email_error"] = "未接続"
            return {}
        wanted = set(ids)
        return {int(r["clinic_id"]): str(r.get("email") or "").strip()
                for r in repo.primary_listing(limit=1_000_000)
                if int(r["clinic_id"]) in wanted and r.get("final_sales_eligible") is not False}
    except Exception as exc:  # Email DB未接続でも画面は止めない（未取得扱い）
        st.session_state[K + "email_error"] = type(exc).__name__
        return {}


# ---------- tabs ----------

def _tab_targets(store, demo):
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

    st.markdown("#### 絞り込み（Clinic Master・READ ONLY）")
    prefs = _prefectures(store)
    from src.normalizer.departments import DEPARTMENTS
    c1, c2 = st.columns(2)
    pref = c1.multiselect("都道府県", prefs, default=["東京都"] if "東京都" in prefs else [], key=K + "pref")
    city_text = c2.text_input("市区町村（複数は読点区切り）", key=K + "city")
    deps = c1.multiselect("標榜診療科", DEPARTMENTS, key=K + "deps")
    rank_label = c2.selectbox("HPランク（HP ABC判定）", list(HP_RANK_OPTIONS), index=0 if demo else 1, key=K + "rank")
    email_filter = c1.radio("メール", ["指定なし", "取得済み", "未取得"], horizontal=True, key=K + "email_filter")
    article_filter = c2.multiselect(
        "記事", [ARTICLE_LISTED, ARTICLE_NOT_LISTED, ARTICLE_UNCONFIRMED],
        format_func=ARTICLE_STATUS_LABELS.get, key=K + "article_filter")
    limit = st.selectbox("生成件数（上限医院数）", LIMIT_OPTIONS,
                         index=LIMIT_OPTIONS.index(DEFAULT_LIMIT), key=K + "limit")

    if st.button("対象医院を抽出", type="primary", key=K + "select"):
        from src.master.filters import Filters
        from src.master.scope import SCOPE_ALL
        filters = Filters(
            active_only=True, hp_only=False, prefectures=pref,
            municipalities=[x.strip() for x in city_text.replace("、", ",").split(",") if x.strip()],
            departments=deps, effective_ranks=HP_RANK_OPTIONS[rank_label], scope=SCOPE_ALL,
        )
        # 重複・フィルタ除外を見込み、選んだ件数ぶんの余地を持って走査する（最低SCAN_POOL件）。
        scan_pool = max(SCAN_POOL, int(limit) * 4)
        try:
            records = store.query(filters, limit=scan_pool)  # READ ONLY
        except Exception as exc:
            st.error(f"Clinic Masterの取得に失敗しました: {exc}")
            return
        selection = select_targets(records, email_lookup=_email_lookup, links=links,
                                   email_filter=email_filter, article_filter=tuple(article_filter),
                                   limit=int(limit))
        st.session_state[K + "selection"] = selection
        st.session_state[K + "requested_limit"] = int(limit)
        st.session_state.pop(K + "results", None)

    selection = st.session_state.get(K + "selection")
    if selection is None:
        return
    requested = st.session_state.get(K + "requested_limit", DEFAULT_LIMIT)
    if st.session_state.get(K + "email_error"):
        st.caption("Email調査DBに接続できないため、メールは全件「未取得」扱いです。")
    st.success(f"対象 {len(selection.targets)}医院（走査 {selection.scanned}件／UUIDなし除外 {selection.skipped_no_uuid}件"
               f"／UUID重複除外 {selection.skipped_duplicate}件{'／上限で打ち切り' if selection.truncated else ''}）")
    if not selection.truncated and len(selection.targets) < requested:
        st.caption(f"指定件数{requested}件に対し、条件に一致する医院は実際には{len(selection.targets)}件でした。")
    st.dataframe(pd.DataFrame([{
        "医院名": t.clinic_name, "Clinic UUID": t.clinic_uuid, "都道府県": t.prefecture, "市区町村": t.municipality,
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
                 disabled=not (staff or "").strip() or not dates):
        settings = GenerationSettings(staff=staff.strip(), dates=tuple(sorted(dates)), start_time=start_time,
                                      end_time=end_time, called_uuids=frozenset(called))
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
        to_csv_bytes(results, settings.staff, settings.generation_batch_id),
        f"mail_send_text_{today_japan():%Y%m%d}.csv", mime="text/csv",
        key=K + "download", use_container_width=True)
    st.caption("CSV出力のみです。Gmail下書き作成・メール送信は行いません。")


def outreach_email_page(store, demo=False):
    st.header("メール送付文章")
    st.info("営業メール文章の生成・確認専用です。Gmail下書き作成・メール送信・本番DBへの書き込みは一切行いません。")
    tabs = st.tabs(["① 対象選択", "② 文章生成", "③ プレビュー", "④ 生成結果"])
    with tabs[0]:
        _tab_targets(store, demo)
    with tabs[1]:
        _tab_generate()
    with tabs[2]:
        _tab_preview()
    with tabs[3]:
        _tab_results()
