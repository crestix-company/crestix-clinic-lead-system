from __future__ import annotations

import html
import re
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from src.services.filters import Filters


from src.utils.paths import repo_path
_ASSET_DIR = repo_path("assets", "call_desk")
_DISPLAY_FIELDS = (
    "morning_start", "morning_end", "afternoon_start", "afternoon_end",
    "business_days", "regular_holiday",
)


def _display(value, default="未登録"):
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return " / ".join(map(str, value)) if isinstance(value, list) else str(value)
    return str(value)


def _departments(record):
    value = record.get("normalized_departments") or record.get("departments") or ""
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[/,、]", value) if part.strip()]
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def _render_clinic(record):
    source = (_ASSET_DIR / "call.html").read_text(encoding="utf-8")
    styles = (_ASSET_DIR / "call.css").read_text(encoding="utf-8")
    phone = str(record.get("phone") or "").strip()
    tel_value = re.sub(r"[^0-9+*#,;]", "", phone)
    call_action = (
        f'<a class="call-button" href="tel:{html.escape(tel_value, quote=True)}" '
        'aria-label="電話アプリを開く"><svg viewBox="0 0 24 24" aria-hidden="true">'
        '<path d="m7.1 3.5 2.4 4.7-1.7 1.7c1.1 2.2 3 4.1 5.2 5.2l1.7-1.7 4.7 2.4-.7 3.2c-.2.8-.9 1.4-1.8 1.4A14.6 14.6 0 0 1 2.5 6c0-.9.6-1.6 1.4-1.8l3.2-.7Z" />'
        '</svg></a>'
        if tel_value else '<button class="call-button" disabled aria-label="電話番号未登録">☎</button>'
    )
    departments = _departments(record)
    replacements = {
        "{{clinic_name}}": _display(record.get("clinic_name"), "医院を検索してください"),
        "{{facility_type}}": _display(record.get("facility_type"), "医療機関"),
        "{{manager_name}}": _display(record.get("manager_name")),
        "{{phone}}": _display(phone),
        "{{address}}": _display(record.get("address")),
        "{{morning_hours}}": _display(
            " - ".join(str(record.get(field) or "") for field in _DISPLAY_FIELDS[:2]).strip(" -")
        ),
        "{{afternoon_hours}}": _display(
            " - ".join(str(record.get(field) or "") for field in _DISPLAY_FIELDS[2:4]).strip(" -")
        ),
        "{{business_days}}": _display(record.get("business_days")),
        "{{regular_holiday}}": _display(record.get("regular_holiday")),
        "{{clinic_url}}": _display(record.get("hp_url") or record.get("maps_website_url")),
        "{{department_1}}": departments[0] if len(departments) > 0 else "未登録",
        "{{department_2}}": departments[1] if len(departments) > 1 else "未登録",
        "{{department_3}}": departments[2] if len(departments) > 2 else "未登録",
        "{{call_action}}": call_action,
    }
    for token, value in replacements.items():
        source = source.replace(token, html.escape(value) if token != "{{call_action}}" else value)
    source = source.replace(
        '<link rel="stylesheet" href="/call.css" />', f"<style>{styles}</style>"
    )
    source = source.replace(
        '<script src="/call.js"></script>',
        """<script>
        document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
          document.querySelectorAll(".tab").forEach((item) => item.classList.remove("is-active"));
          document.querySelectorAll(".tab-content").forEach((item) => item.classList.remove("is-active"));
          tab.classList.add("is-active");
          document.getElementById(tab.dataset.tab)?.classList.add("is-active");
        }));
        </script>""",
    )
    return source


def call_desk_page(store, *, read_only=False):
    st.header("コール")
    st.caption("Clinic Masterから医院を検索します。通話記録・音声・要約の保存機能は未接続です。")
    query = st.text_input("医院名・電話番号・住所で検索", key="call_desk_search")
    records = []
    if query.strip():
        query_method = (
            store.query_read_only_ui if read_only and hasattr(store, "query_read_only_ui")
            else store.query
        )
        records = query_method(
            Filters(active_only=False, hp_only=False, keyword=query.strip()),
            limit=30,
        )
        if not records:
            st.info("一致する医院がありません。検索語を変更してください。")
            return
        selected = st.selectbox(
            "医院",
            range(len(records)),
            format_func=lambda record: (
                f"{records[record].get('clinic_name') or '名称未登録'}"
                f" — {records[record].get('phone') or '電話番号未登録'}"
            ),
            key="call_desk_clinic",
        )
        st.text_input("コール担当者（画面表示のみ）", key="call_desk_assignee")
        st.button(
            "メール確認待ちに追加",
            disabled=True,
            help="コール記録・担当者との連携は未実装です。",
            key="call_desk_queue_email",
        )
        st.caption("上のボタンは画面案です。通話・DB記録・メールキューへの追加は行いません。")
        components.html(_render_clinic(records[selected]), height=950, scrolling=True)
    else:
        st.info("医院名や電話番号で検索すると、Clinic Masterの登録情報を表示します。")
