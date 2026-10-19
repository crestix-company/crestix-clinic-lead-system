"""Single-approval bounded Gmail draft save, reusing existing safety gates."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pandas as pd
import streamlit as st

from src.outreach.auto_draft_batch import MAX_AUTO_DRAFTS, plan_auto_drafts, run_approved_batch
from src.outreach.gmail_workflow import create_gmail_draft, gmail_drafts_enabled
from src.outreach.pre_send_review import DRAFT_SAVE_CHECKS, build_draft_save_review, configured_i3_reference
from src.outreach.pre_send_ui import render_pre_send_review_gate
from src.utils.date_utils import today_japan


def auto_draft_save_page(*, demo: bool = False, read_only: bool = False) -> None:
    """Render as fifth tab; never send or schedule an email."""
    st.subheader("READY医院のGmail下書き一括保存")
    if read_only:
        st.info("READ ONLY画面確認モードのため、Gmail下書き一括保存は無効です。")
    st.caption(
        "一度の承認で最大25医院のGmail下書きを順次保存します。メール送信・予約送信は行いません。"
        " 各医院のGmail/DB重複照合と予約処理は既存の安全な実装で毎回再実行します。"
    )
    stored = st.session_state.get("outreach_results")
    if not stored:
        st.info("まず「① 対象選択」「② 文章生成」を実行してください。")
        return
    settings, results = stored
    campaign_id = str(st.session_state.get("outreach_campaign_id", "")).strip()
    ready = sum(r.status == "READY" for r in results)
    cap = st.selectbox("1回で保存する上限", (5, 10, 25), index=1, key="auto_draft_cap")
    plan = plan_auto_drafts(results, limit=cap)
    a,b,c,d = st.columns(4)
    a.metric("生成済み", len(results))
    b.metric("READY", ready)
    c.metric("今回の保存候補", len(plan.eligible))
    d.metric("除外・要確認", len(plan.held))
    if plan.overflow:
        st.caption(f"上限外の対象 {plan.overflow}医院は今回保存しません。次のバッチで再照合してください。")
    if plan.held:
        with st.expander(f"自動保存を見送る医院：{len(plan.held)}件", expanded=False):
            st.dataframe(pd.DataFrame([{
                "医院名": item.result.target.clinic_name,
                "Clinic UUID": item.result.target.clinic_uuid,
                "判定": item.result.status,
                "理由": item.reason,
            } for item in plan.held]), hide_index=True, use_container_width=True)
    if not plan.eligible:
        st.warning("現在、一括保存できる医院がありません。要確認項目を解消してください。")
        return
    st.dataframe(pd.DataFrame([{
        "医院名":r.target.clinic_name,
        "Clinic ID":r.target.clinic_id,
        "Clinic UUID":r.target.clinic_uuid,
        "宛先":r.target.email,
        "記事":r.target.article.status,
        "件名":r.subject,
        "本文":r.body,
        "実掲載記事/類似記事URL": r.listed_url or r.example_url or (r.first.article.url if r.first else ""),
        "医院詳細URL": r.target.article.clinic_profile_url,
    } for r in plan.eligible]), hide_index=True, use_container_width=True)
    st.caption("画面上の件名・本文・宛先を一括確認してください。内容の修正が必要な場合は先に生成し直してください。")

    feature_enabled = gmail_drafts_enabled()
    history_verified = bool(st.session_state.get("outreach_gmail_history_verified"))
    delivery_ready = bool(st.session_state.get("outreach_delivery_ready"))
    calendar_confirmed = bool(settings.calendar_confirmed)
    provisional_approved = bool(settings.provisional_template_approved)
    if not feature_enabled:
        st.warning("Gmail下書き保存のFeature FlagはOFFです（OUTREACH_GMAIL_DRAFTS_ENABLED）。")
    if not history_verified or not delivery_ready:
        st.warning("Gmail/既存シート履歴または送付履歴DBを照合できていないため実行を停止します。")
    if not calendar_confirmed:
        st.warning("候補日時のGoogleカレンダー/本人予定確認が未完了です。")
    if not provisional_approved:
        st.warning("関連記事型テンプレートの最終確認が未完了です。")

    key = settings.generation_batch_id
    all_confirmed = st.checkbox(
        f"表示された{len(plan.eligible)}医院の宛先・本文・候補日時・記事根拠を一括確認しました",
        key=f"auto_draft_review_{key}",
    )
    history_confirmed = st.checkbox(
        "過去の下書き・予約送信・送信済み・削除済み・旧案件履歴の未解決HOLDを確認しました",
        key=f"auto_draft_history_{key}",
        help="手動確認の宣言です。自動で過去の全Gmail履歴を検査したという意味ではありません。",
    )
    reviewer = st.text_input("下書き保存確認担当者", key=f"auto_draft_reviewer_{key}")
    gmail_states = st.session_state.get("outreach_gmail_states", {})
    draft_reviews = {}
    review_blockers = {}
    with st.expander("医院別の下書き保存確認", expanded=True):
        for idx, result in enumerate(plan.eligible):
            clinic_id = result.target.clinic_id
            gmail_state = gmail_states.get(clinic_id, "未照合")
            st.markdown(f"**{result.target.clinic_name}｜Clinic {clinic_id}｜Gmail: {gmail_state}**")
            checks = {
                "clinic_identity": st.checkbox("医院名・院長名・宛先を確認", key=f"auto_identity_{key}_{idx}"),
                "template_body": st.checkbox("正式本文を全文確認", key=f"auto_body_{key}_{idx}"),
                "article_reference": st.checkbox("実掲載記事または類似記事を確認", key=f"auto_article_{key}_{idx}"),
                "clinic_detail_page": st.checkbox("医院詳細ページを確認", key=f"auto_profile_{key}_{idx}"),
                "duplicate_clear": st.checkbox("既存Gmail重複なしを確認", disabled=gmail_state != "CLEAR",
                                                key=f"auto_duplicate_{key}_{idx}"),
            }
            if not history_verified or not delivery_ready or gmail_state != "CLEAR":
                checks["duplicate_clear"] = False
            payload, blockers = build_draft_save_review(
                result=result, reviewer=reviewer, checks=checks,
                operation_key=f"clinic:{clinic_id}:campaign:{campaign_id}", campaign_id=campaign_id,
            )
            draft_reviews[id(result)] = payload
            review_blockers[id(result)] = blockers
            if blockers:
                st.caption("未完了: " + " / ".join(blockers))
    i3_reference = configured_i3_reference()
    if not i3_reference.configured:
        st.info("I3正式定義が未確定のため送信前確認は未承認です。これはGmail下書き保存とは別のゲートです。Clinic Leadから送信・予約送信はできません。")
    else:
        st.caption(f"I3正式定義参照が設定されています（{i3_reference.definition_id}）。送信判断はGmailでISリーダーが行います。")
    phrase = f"CREATE_{len(plan.eligible)}_GMAIL_DRAFTS"
    typed = st.text_input(f"実行確認語：{phrase}", key=f"auto_draft_phrase_{key}")
    st.caption("処理途中で結果不明または照合エラーを検出すると以降の医院は自動停止します。失敗分を自動再作成しません。")
    if today_japan() != settings.as_of:
        st.error("文章生成日から日付が変わっています。本文と候補日時を再生成してください。")
    if st.session_state.get(f"auto_draft_done_{key}"):
        st.warning("この一括生成バッチは処理を試行済みです。履歴を再照合して新たに文章生成するまで再実行できません。")
    can_save = (
        feature_enabled and not demo and not read_only and history_verified and delivery_ready
        and calendar_confirmed and provisional_approved and bool(campaign_id)
        and bool(settings.staff.strip()) and all_confirmed and history_confirmed
        and typed == phrase and today_japan() == settings.as_of
        and not st.session_state.get(f"auto_draft_done_{key}")
        and bool(reviewer.strip()) and all(not review_blockers[id(r)] for r in plan.eligible)
    )
    if st.button("確認済み医院の下書きを自動保存", key=f"auto_draft_execute_{key}",
                 type="primary", disabled=not can_save):
        repository = None
        try:
            from src.outreach.gmail_oauth import connect_existing
            from src.repository.outreach_delivery_repository import OutreachDeliveryRepository
            gmail = connect_existing(root=Path.cwd())
            repository = OutreachDeliveryRepository.connect()
            if not repository.health_check():
                raise RuntimeError("送付履歴DB接続が未確認")
            progress = st.progress(0, text="下書きを確認中")
            def create_one(result):
                response = create_gmail_draft(
                    result, campaign_id=campaign_id,
                    generation_batch_id=settings.generation_batch_id,
                    staff_name=settings.staff,
                    gmail=gmail, repository=repository,
                    as_of=today_japan(), enabled=feature_enabled,
                    draft_save_review=draft_reviews[id(result)],
                )
                # Each iteration runs the existing Gmail-state and ledger preflight.
                return response
            # Run only the pre-approved, bounded snapshot; never the full raw list.
            audit = run_approved_batch(plan.eligible, create_one=create_one)
            progress.progress(len(audit.outcomes) / len(plan.eligible),
                              text=f"処理済み {len(audit.outcomes)} / {len(plan.eligible)}")
            counts = Counter(o.status for o in audit.outcomes)
            st.session_state["auto_draft_audit_" + key] = audit
            st.success("下書き処理： " + " / ".join(f"{label}: {count}" for label,count in sorted(counts.items())))
            if audit.stopped_early:
                st.error(audit.stopped_reason + "。未処理医院は再照合後に判断してください。")
            # An earlier successful run must not be blindly repeated on another click.
            st.session_state[f"auto_draft_done_{key}"] = True
        except Exception as exc:
            st.error(f"一括保存を開始・継続できませんでした（{type(exc).__name__}）。再実行前にGmail/DB照合が必要です。")
            st.session_state[f"auto_draft_done_{key}"] = True
        finally:
            if repository is not None:
                repository.close()
    audit = st.session_state.get("auto_draft_audit_" + key)
    if audit:
        st.dataframe(pd.DataFrame([{
            "状態":r.status, "Outreach ID":r.outreach_id, "Draft ID":r.gmail_draft_id,
            "理由":r.reason,
        } for r in audit.outcomes]), hide_index=True, use_container_width=True)
        for idx, outcome in enumerate(audit.outcomes):
            if idx < len(plan.eligible):
                result = plan.eligible[idx]
                with st.expander(f"送信前確認：{result.target.clinic_name}", expanded=False):
                    render_pre_send_review_gate(
                        result=result, outcome=outcome, campaign_id=campaign_id,
                        key=f"auto_send_review_{key}_{idx}",
                    )
