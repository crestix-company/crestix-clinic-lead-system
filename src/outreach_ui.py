"""Streamlit preview-only outreach workspace (CSV inputs, no DB or Gmail writes)."""
from __future__ import annotations

import csv
from datetime import timedelta
from io import StringIO

import pandas as pd
import streamlit as st

from src.outreach.article_adapter import ArticleLink
from src.outreach.batch_preview import ClinicContact, preview_batch
from src.outreach.email_template import five_weekdays
from src.utils.date_utils import today_japan

CONTACT_COLUMNS = {"clinic_uuid", "clinic_name", "primary_email", "recipient_role",
                   "recipient_name", "email_verified"}
ARTICLE_COLUMNS = {"clinic_uuid", "listing_status", "example_article_url", "listing_url",
                   "listing_kind", "source_row", "article_id", "verified"}


def _read_csv(upload, required):
    text = upload.getvalue().decode("utf-8-sig")
    reader = csv.DictReader(StringIO(text))
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        raise ValueError("必要列がありません: " + ", ".join(sorted(required - set(reader.fieldnames or []))))
    return list(reader)


def _bool(value):
    if str(value).strip().lower() not in {"true", "false"}:
        raise ValueError("確認済みフラグは true / false のみ許可します")
    return str(value).strip().lower() == "true"


def outreach_preview_page():
    st.subheader("営業メール｜一括プレビュー")
    st.info("開発プレビュー専用。ここではGmail下書き・送信・本番DB変更は一切行いません。")
    st.caption("記事原本CSV単体ではClinic UUIDと紐付きません。検証済み記事JOIN CSVが必要です。")
    st.divider()
    st.markdown("#### おすすめ記事の候補選定（プレビュー）")
    st.caption("地域・標榜科目から既存の記事マスターを検索。掲載中・販売枠の保証ではありません。")
    article_master = st.file_uploader("マイナビ記事原本または実在確認記事CSV（読み取り専用）",
                                      type=["csv"], key="outreach_article_master")
    region = st.text_input("都道府県", key="outreach_region")
    municipality = st.text_input("市区町村", key="outreach_municipality")
    department = st.text_input("標榜科目（複数は読点区切り）", key="outreach_department")
    treatment = st.text_input("確認済み治療・検査（任意・読点区切り）", key="outreach_treatment")
    if st.button("おすすめ記事候補を表示", key="outreach_recommend"):
        if article_master is None or not department.strip():
            st.warning("記事CSVと標榜科目の入力が必要です。")
        else:
            import tempfile
            from pathlib import Path
            from src.outreach.article_recommender import read_master, recommend
            try:
                # Streamlit upload remains in-memory; a temporary read-only copy is removed immediately.
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "article_master.csv"
                    path.write_bytes(article_master.getvalue())
                    articles = read_master(path)
                chosen = recommend(
                    articles,
                    prefecture=region,
                    municipality=municipality,
                    departments=tuple(x.strip() for x in department.replace("、", ",").split(",") if x.strip()),
                    treatments=tuple(x.strip() for x in treatment.replace("、", ",").split(",") if x.strip()),
                )
                if chosen:
                    for n, item in enumerate(chosen, 1):
                        st.write(f"{n}. {item.article.title}")
                        st.code(item.article.url)
                        st.caption(" / ".join(item.reasons))
                else:
                    st.warning("一致候補がありません。診療科の同義語・地域拡張を人間が確認してください。")
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                st.error(f"記事CSVを確認してください: {exc}")
    st.divider()
    contacts_file = st.file_uploader("医院・宛先CSV（Clinic UUID付き）", type=["csv"], key="outreach_contacts")
    articles_file = st.file_uploader("マイナビ記事JOIN CSV（Clinic UUID・検証根拠付き）", type=["csv"], key="outreach_articles")
    staff = st.text_input("IS担当者名", value="", key="outreach_staff")
    start = st.date_input("候補日時の開始日（平日5日分を表示）",
                          value=today_japan() + timedelta(days=2),
                          key="outreach_start")
    called = st.checkbox("全対象医院に本日電話し、メール送付を案内されたことを確認済み",
                         value=False, key="outreach_called")
    director = st.checkbox("院長参加依頼文を使うことを確認済み",
                           value=False, key="outreach_director")
    if not st.button("最大50件をプレビュー", type="primary", key="outreach_preview"):
        return
    if contacts_file is None or articles_file is None:
        st.warning("医院CSVと記事JOIN CSVの両方が必要です。")
        return
    try:
        contact_rows = _read_csv(contacts_file, CONTACT_COLUMNS)
        article_rows = _read_csv(articles_file, ARTICLE_COLUMNS)
        # Stop before generating any preview if the upload exceeds pilot scope.
        if len(contact_rows) > 50:
            st.warning("初回は50件以下の医院CSVに絞ってください。")
            return
        contacts = [ClinicContact(
            clinic_uuid=r["clinic_uuid"], clinic_name=r["clinic_name"],
            primary_email=r["primary_email"], recipient_role=r["recipient_role"],
            recipient_name=r["recipient_name"], email_verified=_bool(r["email_verified"]),
        ) for r in contact_rows]
        links = [ArticleLink(
            clinic_uuid=r["clinic_uuid"], listing_status=r["listing_status"],
            example_article_url=r["example_article_url"], listing_url=r["listing_url"],
            listing_kind=r["listing_kind"], source_row=r["source_row"],
            article_id=r["article_id"], verified=_bool(r["verified"]),
        ) for r in article_rows]
        results = preview_batch(
            contacts, links, is_staff_name=staff,
            candidate_dates=five_weekdays(start),
            called_today_and_told_to_email=called,
            director_attendance_requested=director,
        )
    except (ValueError, UnicodeDecodeError) as exc:
        st.error(f"入力を確認してください: {exc}")
        return
    counts = {state: sum(r.status == state for r in results)
              for state in ("READY", "NEEDS_REVIEW", "NO_MAPPING")}
    st.write(f"対象: {len(results)}件 / READY: {counts['READY']}件 / 要確認: {counts['NEEDS_REVIEW']}件 / 紐付けなし: {counts['NO_MAPPING']}件")
    st.dataframe(pd.DataFrame([{
        "Clinic UUID": r.clinic_uuid, "判定": r.status,
        "宛先": r.to, "件名": r.subject, "確認理由": r.reason,
    } for r in results]), hide_index=True, use_container_width=True)
    # Preview only: no export of unreviewed drafts or send/create buttons.
    ready = [r for r in results if r.status == "READY"]
    if ready:
        index = st.selectbox("プレビューする医院", options=range(len(ready)),
                             format_func=lambda i: ready[i].clinic_uuid)
        st.text_input("宛先", ready[index].to, disabled=True)
        st.text_input("件名", ready[index].subject, disabled=True)
        st.text_area("本文", ready[index].body, height=500, disabled=True)
