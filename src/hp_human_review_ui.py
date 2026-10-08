"""Streamlit UI for HP Human Review.

Kept separate from app_v2.py so the production HP worker/controller remains unchanged.
"""
from __future__ import annotations

import json
import os

import pandas as pd
import streamlit as st

from src.master.hp_human_review import (
    BUCKET_LABELS,
    DECISION_LABELS,
    POSITIVE_DECISIONS,
    PRIORITY_LABELS,
    reanalyze_human_verified_hp,
)
from src.repository.write_backend import write_repositories_for


def _repo_for(store):
    return write_repositories_for(store).hp_human_review


@st.fragment(run_every="10s")
def hp_human_review_card(store):
    """Compact card for the simple workflow. Hidden until the migration exists."""
    repo = _repo_for(store)
    if repo is None or not repo.available():
        return
    summary = repo.summary()
    st.markdown("#### 🔍 HP要確認レビュー")
    cols = st.columns(4)
    cols[0].metric("未レビュー", f"{summary['unreviewed']:,}件")
    cols[1].metric("高確度候補", f"{summary['HIGH']:,}件")
    cols[2].metric("中確度候補", f"{summary['MEDIUM']:,}件")
    cols[3].metric("低確度候補", f"{summary['LOW']:,}件")
    st.caption("公式HPを自動確定できなかった医院を、人間が1件ずつ確認して教師データとして保存します。")
    if st.button(
        "要確認をレビューする",
        type="primary",
        key="simple_hp_human_review_start",
        use_container_width=True,
        disabled=summary["unreviewed"] <= 0,
    ):
        st.session_state["_jump_to_hp_human_review"] = True
        st.rerun()


def _candidate_frame(snapshot):
    rows = []
    for item in snapshot.get("candidates") or []:
        rows.append({
            "候補URL": item.get("url", ""),
            "スコア": int(item.get("score") or 0),
            "医院名": "一致" if item.get("name_match") else "不一致",
            "電話": "一致" if item.get("phone_match") else "不一致",
            "住所": "一致" if item.get("address_match") else "不一致",
            "院長名": "一致" if item.get("manager_match") else "不一致",
            "理由": " / ".join(item.get("reasons") or []),
        })
    return pd.DataFrame(rows)


def _feature_mark(value):
    return "✅ 一致" if value else "❌ 不一致"


def _save_decision(store, repo, row, decision, selected_url, reviewer, note):
    review_id = repo.save_review(
        clinic_id=row["clinic_id"],
        hp_checked_at=row["snapshot"]["hp_checked_at"],
        human_decision=decision,
        selected_url=selected_url,
        reviewer=reviewer,
        review_note=note,
        research_job_id=row.get("job_id") or "",
        auto_run_id=row.get("auto_run_id") or "",
    )
    message = f"{row['clinic_name']} の判定を保存しました（{DECISION_LABELS[decision]}）。"
    if decision in POSITIVE_DECISIONS:
        with st.spinner("Human確認済みHPを再取得し、治療カテゴリを解析しています…"):
            outcome = reanalyze_human_verified_hp(
                store,
                human_review_id=review_id,
                clinic_id=row["clinic_id"],
                selected_url=selected_url,
            )
        if outcome["status"] == "DONE":
            count = int(outcome.get("treatment_count") or 0)
            destination = "治療カテゴリ検出あり" if count > 0 else "治療カテゴリ検出なし"
            message += f" 再解析完了：{count}カテゴリ → {destination}。"
        else:
            message += " HP内容を取得できなかったため「Webサイト調査失敗」へ反映しました。後から再試行できます。"
    st.session_state["_hp_human_review_flash"] = message
    st.session_state["_hp_human_review_last_id"] = review_id
    st.rerun()


def _review_tab(store, repo):
    flash = st.session_state.pop("_hp_human_review_flash", None)
    if flash:
        st.success(flash)

    filters = st.columns(2)
    priority_label = filters[0].selectbox(
        "優先度",
        ["すべて", "高確度候補", "中確度候補", "低確度候補"],
        key="hp_human_review_priority",
    )
    priority = {
        "すべて": "ALL",
        "高確度候補": "HIGH",
        "中確度候補": "MEDIUM",
        "低確度候補": "LOW",
    }[priority_label]
    include_reviewed = filters[1].checkbox(
        "レビュー済みも表示",
        value=False,
        key="hp_human_review_include_reviewed",
    )

    queue = repo.queue(
        include_reviewed=include_reviewed,
        priority=priority,
        limit=200,
    )
    if not queue:
        st.success("この条件で未レビューのHP要確認はありません。")
        return

    reviewer_default = os.environ.get("CLINIC_REVIEWER_NAME", "")
    reviewer = st.text_input(
        "確認者",
        value=st.session_state.get("hp_human_reviewer", reviewer_default),
        key="hp_human_reviewer",
        placeholder="例：前川",
    )
    st.caption("確認者名は教師データの監査用に保存します。")

    ids = [row["clinic_id"] for row in queue]
    selected_id = st.selectbox(
        "確認する医院",
        ids,
        format_func=lambda cid: next(
            f"{row['snapshot']['priority']}｜{cid}｜{row['clinic_name']}｜score {row['snapshot']['score']}"
            + (f"｜レビュー済 {row.get('human_decision')}" if row["reviewed"] else "")
            for row in queue if row["clinic_id"] == cid
        ),
        key="hp_human_review_clinic",
    )
    row = next(row for row in queue if row["clinic_id"] == selected_id)
    snapshot = row["snapshot"]

    st.markdown(f"### {row['clinic_name']}")
    info_cols = st.columns(4)
    info_cols[0].metric("Clinic ID", str(row["clinic_id"]))
    info_cols[1].metric("都道府県", row.get("prefecture") or "-")
    info_cols[2].metric("自動スコア", str(snapshot.get("score", 0)))
    info_cols[3].metric("優先度", PRIORITY_LABELS.get(snapshot.get("priority"), snapshot.get("priority", "")))
    st.write(f"**電話番号：** {row.get('phone') or '-'}")
    st.write(f"**住所：** {row.get('address') or '-'}")

    st.markdown("#### 自動判定の根拠")
    match_cols = st.columns(4)
    match_cols[0].metric("医院名", _feature_mark(snapshot.get("name_match")))
    match_cols[1].metric("電話番号", _feature_mark(snapshot.get("phone_match")))
    match_cols[2].metric("住所", _feature_mark(snapshot.get("address_match")))
    match_cols[3].metric("院長名", _feature_mark(snapshot.get("manager_match")))
    bucket = snapshot.get("bucket") or "OTHER"
    st.caption(
        f"判定条件：{BUCKET_LABELS.get(bucket, bucket)}／"
        f"Rule: {snapshot.get('rule_version', '')}"
    )
    if snapshot.get("hp_match_reason"):
        st.info(" / ".join(str(x) for x in snapshot["hp_match_reason"]))

    candidate_urls = snapshot.get("candidate_urls") or []
    selected_url = ""
    if candidate_urls:
        default_url = snapshot.get("best_candidate_url")
        default_index = candidate_urls.index(default_url) if default_url in candidate_urls else 0
        selected_url = st.selectbox(
            "判定する候補URL",
            candidate_urls,
            index=default_index,
            key=f"hp_human_review_url_{row['clinic_id']}",
        )
        st.link_button("候補HPを開く", selected_url, use_container_width=True)
    else:
        st.warning("候補URLがありません。URLを正しいと判定する選択肢は使用できません。")

    candidates = _candidate_frame(snapshot)
    if not candidates.empty:
        with st.expander("候補ごとの本人確認情報", expanded=True):
            st.dataframe(
                candidates,
                hide_index=True,
                width="stretch",
                column_config={"候補URL": st.column_config.LinkColumn()},
            )

    if snapshot.get("crawl_errors"):
        with st.expander("取得時のエラー・アクセス制限", expanded=False):
            st.dataframe(pd.DataFrame(snapshot["crawl_errors"]), hide_index=True, width="stretch")

    if row["reviewed"]:
        st.info(
            "最新Human Review："
            + DECISION_LABELS.get(row.get("human_decision"), str(row.get("human_decision") or ""))
            + f"／確認者 {row.get('reviewer') or '-'}"
        )
        if row.get("review_note"):
            st.caption("前回メモ：" + str(row["review_note"]))

        if row.get("reanalysis_status") == "DONE":
            count = int(row.get("reanalysis_treatment_count") or 0)
            destination = "治療カテゴリ検出あり" if count > 0 else "治療カテゴリ検出なし"
            categories = row.get("reanalysis_treatment_categories") or []
            st.success(f"治療カテゴリ再解析：完了／{count}カテゴリ → {destination}")
            if categories:
                st.caption("検出：" + " / ".join(str(x) for x in categories))
        elif row.get("reanalysis_status") == "FAILED":
            st.warning(
                "治療カテゴリ再解析：失敗"
                + (f"／{row.get('reanalysis_error')}" if row.get("reanalysis_error") else "")
            )
            retry_url = row.get("reviewed_url") or selected_url
            if (
                row.get("human_decision") in POSITIVE_DECISIONS
                and retry_url
                and st.button(
                    "治療カテゴリ再解析を再試行",
                    key=f"hp_human_review_retry_{row['clinic_id']}",
                    use_container_width=True,
                )
            ):
                with st.spinner("治療カテゴリを再解析しています…"):
                    outcome = reanalyze_human_verified_hp(
                        store,
                        human_review_id=row["human_review_id"],
                        clinic_id=row["clinic_id"],
                        selected_url=retry_url,
                    )
                if outcome["status"] == "DONE":
                    count = int(outcome.get("treatment_count") or 0)
                    destination = "治療カテゴリ検出あり" if count > 0 else "治療カテゴリ検出なし"
                    st.session_state["_hp_human_review_flash"] = (
                        f"{row['clinic_name']} の再解析が完了しました：{count}カテゴリ → {destination}。"
                    )
                else:
                    st.session_state["_hp_human_review_flash"] = (
                        f"{row['clinic_name']} の再解析は再度失敗しました。"
                    )
                st.rerun()

    note = st.text_area(
        "確認メモ（任意）",
        key=f"hp_human_review_note_{row['clinic_id']}",
        placeholder="判断理由や補足があれば入力",
    )

    st.markdown("#### この医院をどう判定しますか？")
    buttons = st.columns(5)
    decisions = [
        ("OFFICIAL", "✅ 公式HP"),
        ("ORGANIZATION_PAGE", "🏢 法人内正式ページ"),
        ("NOT_OFFICIAL", "❌ 公式HPではない"),
        ("ACCESS_RESTRICTED", "🔒 URL正しい・制限"),
        ("UNCERTAIN", "❓ 判断できない"),
    ]
    for col, (decision, label) in zip(buttons, decisions):
        disabled = not reviewer.strip() or (decision in POSITIVE_DECISIONS and not selected_url)
        if col.button(
            label,
            key=f"hp_human_review_decision_{row['clinic_id']}_{decision}",
            use_container_width=True,
            disabled=disabled,
        ):
            try:
                _save_decision(store, repo, row, decision, selected_url, reviewer, note)
            except ValueError as exc:
                st.error(str(exc))

    st.caption(
        "OFFICIAL / 法人内正式ページ / URL正しい・アクセス制限を選ぶと、"
        "HP URLとHP確認状態を保存した直後に、そのURLをHuman本人確認済みとして再クロールします。"
        "治療カテゴリが1件以上なら「治療カテゴリ検出あり」、0件なら「治療カテゴリ検出なし」、"
        "取得失敗なら「Webサイト調査失敗」へ反映します。"
        "「公式HPではない」「判断できない」は教師ラベルだけを保存し、NOT_FOUNDには自動変更しません。"
    )


def _analytics_tab(repo):
    stats = repo.analytics()
    cols = st.columns(4)
    cols[0].metric("レビュー済み", f"{stats['reviewed']:,}件")
    cols[1].metric("正しいHP/URL", f"{stats['positive']:,}件")
    cols[2].metric("公式HPではない", f"{stats['negative']:,}件")
    cols[3].metric("判断不能", f"{stats['uncertain']:,}件")

    st.markdown("#### 判定条件別")
    if stats["by_bucket"]:
        frame = pd.DataFrame(stats["by_bucket"])
        frame = frame.rename(columns={
            "label": "条件",
            "reviewed": "レビュー数",
            "decided": "判定可能数",
            "positive": "正しいHP",
            "official_rate": "Human正解率",
        })
        st.dataframe(
            frame[["条件", "レビュー数", "判定可能数", "正しいHP", "Human正解率"]],
            hide_index=True,
            width="stretch",
            column_config={"Human正解率": st.column_config.NumberColumn(format="percent")},
        )
    else:
        st.info("Human Reviewデータがまだありません。")

    st.info(
        "この集計は自動判定ルールを勝手に変更しません。"
        "レビュー数が十分に溜まった条件だけを別テストで検証し、Precisionを確認してから自動確定条件へ昇格させます。"
    )

    rows = repo.training_rows()
    if rows:
        export_rows = []
        for row in rows:
            snapshot = row.get("auto_snapshot")
            if not isinstance(snapshot, dict):
                try:
                    snapshot = json.loads(snapshot or "{}")
                except (TypeError, ValueError):
                    snapshot = {}
            export_rows.append({
                "clinic_id": row.get("clinic_id"),
                "clinic_name": row.get("clinic_name"),
                "prefecture": row.get("prefecture"),
                "phone": row.get("phone"),
                "hp_checked_at": row.get("hp_checked_at"),
                "selected_url": row.get("selected_url"),
                "human_decision": row.get("human_decision"),
                "reviewer": row.get("reviewer"),
                "review_note": row.get("review_note"),
                "rule_version": row.get("rule_version"),
                "auto_score": snapshot.get("score"),
                "name_match": snapshot.get("name_match"),
                "phone_match": snapshot.get("phone_match"),
                "address_match": snapshot.get("address_match"),
                "manager_match": snapshot.get("manager_match"),
                "priority": snapshot.get("priority"),
                "bucket": snapshot.get("bucket"),
                "reviewed_at": row.get("reviewed_at"),
                "treatment_reanalysis_status": row.get("reanalysis_status"),
                "treatment_categories": " / ".join(row.get("reanalysis_treatment_categories") or []),
                "treatment_category_count": row.get("reanalysis_treatment_count"),
                "treatment_reanalysis_error": row.get("reanalysis_error"),
                "treatment_reanalysis_finished_at": row.get("reanalysis_finished_at"),
            })
        csv = pd.DataFrame(export_rows).to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "教師データCSVをダウンロード",
            csv,
            "hp_human_review_training.csv",
            mime="text/csv",
            use_container_width=True,
        )


def hp_human_review_page(store):
    st.subheader("HP Human Review")
    st.caption("自動判定がREVIEWになった医院を人間が確認し、正解ラベルを継続的に蓄積します。")

    repo = _repo_for(store)
    if repo is None:
        st.warning("HP Human ReviewはSupabase Runtime専用です。")
        return
    if not repo.available():
        st.warning(
            "HP Human Review用テーブルがまだ準備されていません。"
            "scripts/supabase_migration/hp_human_review_schema.sql を適用してから利用してください。"
        )
        return

    summary = repo.summary()
    cols = st.columns(4)
    cols[0].metric("未レビュー", f"{summary['unreviewed']:,}件")
    cols[1].metric("高確度候補", f"{summary['HIGH']:,}件")
    cols[2].metric("中確度候補", f"{summary['MEDIUM']:,}件")
    cols[3].metric("低確度候補", f"{summary['LOW']:,}件")

    review_tab, analytics_tab = st.tabs(["1件ずつレビュー", "精度分析"])
    with review_tab:
        _review_tab(store, repo)
    with analytics_tab:
        _analytics_tab(repo)
