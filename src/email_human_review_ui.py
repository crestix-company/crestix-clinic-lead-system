"""Streamlit UI for Email Human Review; intentionally independent of HP review."""
from __future__ import annotations

import os
import streamlit as st

from src.master.email_human_review import DECISION_LABELS, true_positive_allowed
from src.normalizer.address import extract_municipality
from src.repository.email_database import email_repositories_for_ui


EMAIL_HUMAN_REVIEW_UNAVAILABLE_MESSAGE = (
    "Email Human Reviewを現在利用できません。Clinic Master / HP Researchは引き続き利用できます。"
)


def _repo(store):
    repositories = email_repositories_for_ui()
    return repositories.email_human_review if repositories is not None else None


def _open():
    st.session_state["_open_email_human_review_dialog"] = True
    st.rerun()


def email_human_review_card(store):
    repo = _repo(store)
    if repo is None:
        st.caption(EMAIL_HUMAN_REVIEW_UNAVAILABLE_MESSAGE)
        return
    if not repo.available():
        return
    summary = repo.summary()
    st.markdown("#### ✉️ Email要確認")
    left, right = st.columns(2)
    left.metric("Email確認待ちClinic", f"{summary.get('unreviewed_clinics', summary['unreviewed']):,}件")
    right.metric("レビュー済み", f"{summary['reviewed']:,}件")
    if st.button("メールを確認する", key="simple_email_review", type="primary",
                 use_container_width=True, disabled=not summary["unreviewed"]):
        _open()


def email_human_review_sidebar(store):
    repo = _repo(store)
    if repo is None:
        with st.sidebar:
            st.caption(EMAIL_HUMAN_REVIEW_UNAVAILABLE_MESSAGE)
        return
    if not repo.available():
        return
    summary = repo.summary()
    with st.sidebar:
        st.markdown("### ✉️ Email Human Review")
        st.caption(f"未レビュー {summary['unreviewed']:,}件")
        if st.button("次のEmailをレビュー", key="sidebar_email_review",
                     use_container_width=True, disabled=not summary["unreviewed"]):
            _open()


def _render_one(store, *, dialog=False, clinic_id=None):
    repo = _repo(store)
    if repo is None or not repo.available():
        st.warning("Email Human Reviewを利用できません。")
        return
    flash = st.session_state.pop("_email_human_review_flash", None)
    if flash:
        st.success(flash)
    clinic_id = clinic_id or st.session_state.pop("_email_human_review_clinic_id", None)
    queue = repo.queue(include_reviewed=False, limit=1, clinic_id=clinic_id)
    if not queue:
        st.success("未レビューのEmail Candidateはありません。")
        return
    row = queue[0]
    st.markdown(f"### {row['clinic_name']}")
    city = extract_municipality(row.get("address") or "")
    st.caption(
        f"Clinic ID={row['clinic_id']} / UUID={row.get('clinic_uuid') or '未紐付け'} / "
        f"Candidate ID={row['candidate_id']} / "
        f"{row.get('prefecture') or '-'} {city or '-'}"
    )
    st.write(f"📍 {row.get('address') or '-'}")
    st.write(f"📞 {row.get('phone') or '-'}")
    if row.get("hp_url"):
        st.link_button("確認済み公式HPを開く ↗", row["hp_url"])
    st.code(row["email"], language=None)
    cols = st.columns(3)
    cols[0].metric("AI Decision", row["auto_decision"])
    cols[1].metric("Confidence", row["confidence"])
    cols[2].metric("Verification", row["verification_status"])
    st.write(f"**Role:** {row['role']}　 **Email Ownership:** {row.get('email_ownership') or row['ownership']}")
    st.caption(f"Source Owner: {row.get('source_owner_type') or 'UNKNOWN'}")
    st.write(
        f"医院名一致: {row.get('clinic_name_match')} / 都道府県一致: "
        f"{row.get('prefecture_match')} / 市区町村一致: {row.get('city_match')}"
    )
    st.caption(f"Discovery: {row['discovery_source']} / Query: {row.get('search_query') or '-'}")
    if row.get("verification_reason"):
        st.info(row["verification_reason"])
    if row.get("rejection_reason"):
        st.warning(row["rejection_reason"])
    blocks = list(row.get("hard_block_codes") or [])
    if blocks:
        st.error("HARD BLOCK: " + ", ".join(blocks))
    if row.get("source_url"):
        st.link_button("取得元URLを開く ↗", row["source_url"], use_container_width=True)
    reviewer = st.text_input(
        "確認者", value=st.session_state.get("email_human_reviewer", os.environ.get("CLINIC_REVIEWER_NAME", "")),
        key="email_human_reviewer",
    )
    note = st.text_area("確認メモ", key=f"email_review_note_{row['candidate_id']}")
    buttons = st.columns(3)
    for column, decision in zip(buttons, ("TRUE_POSITIVE", "FALSE_POSITIVE", "UNCERTAIN")):
        disabled = not reviewer.strip() or (decision == "TRUE_POSITIVE" and not true_positive_allowed(row))
        if column.button(DECISION_LABELS[decision], key=f"email_review_{row['candidate_id']}_{decision}",
                         disabled=disabled, use_container_width=True):
            repo.save_review(candidate_id=row["candidate_id"], human_decision=decision,
                             reviewer=reviewer, review_note=note)
            st.session_state["_email_human_review_flash"] = (
                f"{row['email']} を {decision} として保存しました。"
            )
            st.rerun(scope="fragment" if dialog else "app")


@st.dialog("Email要確認レビュー", width="large")
def email_human_review_dialog(store):
    clinic_id = st.session_state.pop("_email_human_review_clinic_id", None)
    _render_one(store, dialog=True, clinic_id=clinic_id)


def email_human_review_page(store):
    st.header("Email Human Review")
    st.caption("Identity教師ラベルと営業利用可否は別管理です。HARD BLOCKは解除できません。")
    _render_one(store)
