"""IS-lead pre-send review UI, separate from Gmail draft creation and send APIs."""
from __future__ import annotations

import streamlit as st

from src.outreach.pre_send_review import (
    REQUIRED_CHECKS, build_pre_send_review, configured_i3_reference,
)


def render_pre_send_review_gate(*, result, outcome, campaign_id: str, key: str) -> None:
    if outcome.status not in {"DRAFT_CREATED", "DRAFT_RECOVERED"} or not outcome.outreach_id:
        return
    st.markdown("**送信前確認（Gmail下書き保存後・送信操作なし）**")
    st.caption("この確認は送信準備の記録のみです。送信・予約送信はGmailでISリーダーが行います。")
    st.text_input("医院名", result.target.clinic_name, disabled=True, key=key + "_clinic")
    st.text_input("院長名", result.target.director_name, disabled=True, key=key + "_director")
    st.text_input("宛先", result.target.email, disabled=True, key=key + "_recipient")
    st.text_area("Gmail下書き本文", result.body, height=220, disabled=True, key=key + "_body")
    article_url = result.listed_url or result.example_url or (result.first.article.url if result.first else "")
    st.text_input("実掲載記事または類似記事URL", article_url, disabled=True, key=key + "_article")
    st.text_input("医院詳細ページURL", result.target.article.clinic_profile_url,
                  disabled=True, key=key + "_profile")
    st.caption(f"Draft ID: {outcome.gmail_draft_id or '未確認'}｜送信状態: 未送信（下書き）")
    i3_reference = configured_i3_reference()
    if not i3_reference.configured:
        st.warning("I3の正式定義が未確定です。承認済みID/参照先がないため送信前確認は未承認です。")
        return
    st.markdown(f"I3正式定義参照：[{i3_reference.definition_id}]({i3_reference.definition_url})")
    reviewer = st.text_input("送信前確認担当者（ISリーダー）", key=key + "_reviewer")
    checks = {
        "clinic_identity": st.checkbox("医院名・院長名・宛先を確認", key=key + "_identity"),
        "template_body": st.checkbox("正式テンプレート本文をGmail下書きと照合", key=key + "_template"),
        "article_reference": st.checkbox("記事URLを確認", key=key + "_article_check"),
        "clinic_detail_page": st.checkbox("医院詳細ページを確認", key=key + "_profile_check"),
        "duplicate_clear": st.checkbox("このDraft以外に重複下書き・予約・送信済みがないことを再確認",
                                        key=key + "_duplicate"),
    }
    i3_acknowledged = st.checkbox("I3正式定義に基づく確認を完了", key=key + "_i3")
    review, blockers = build_pre_send_review(
        result=result, reviewer=reviewer, checks=checks,
        i3_reference=i3_reference, i3_acknowledged=i3_acknowledged,
        operation_key=f"clinic:{result.target.clinic_id}:campaign:{campaign_id}",
        campaign_id=campaign_id,
    )
    if blockers:
        st.caption("未完了: " + " / ".join(blockers))
    if st.button("送信前確認を記録（送信しない）", key=key + "_record",
                 disabled=bool(blockers)):
        repository = None
        try:
            from src.repository.outreach_delivery_repository import OutreachDeliveryRepository
            repository = OutreachDeliveryRepository.connect()
            repository.record_pre_send_review(outreach_id=outcome.outreach_id, review=review)
            st.success(f"送信前確認を記録しました: {review['reviewed_at']}")
        except Exception as exc:
            st.error(f"送信前確認を記録できませんでした（{type(exc).__name__}）。送信準備状態は未確認です。")
        finally:
            if repository is not None:
                repository.close()


__all__ = ["render_pre_send_review_gate"]
