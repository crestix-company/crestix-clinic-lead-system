"""Streamlit UI for dental sales priority tags and Human Review."""
from __future__ import annotations

import io
import json
import os
import zipfile

import pandas as pd
import streamlit as st

from src.master.dental_sales_tags import DEFINITION_BY_CODE, TAG_DEFINITIONS
from src.repository.write_backend import write_repositories_for


PRIORITY_LABELS = {
    1: "① マウスピース矯正 / インビザライン",
    2: "② 矯正歯科全般",
    3: "③ All-on-4",
    4: "④ インプラント全般",
    5: "⑤ セラミック治療",
    6: "⑥ 審美歯科全般",
    7: "⑦ 口腔外科・審美系個別",
    8: "⑧ 自費入れ歯・親知らず・根管治療",
    9: "⑨ 一般歯科・クリーニング・定期検診・虫歯治療",
}


CATEGORY_EXPORTS = (
    ("01", "① マウスピース矯正 / インビザライン", "01_マウスピース_インビザライン.csv"),
    ("02", "② 矯正歯科全般", "02_矯正歯科全般.csv"),
    ("03", "③ All-on-4", "03_All-on-4.csv"),
    ("04", "④ インプラント全般", "04_インプラント全般.csv"),
    ("05", "⑤ セラミック治療", "05_セラミック治療.csv"),
    ("06", "⑥ 審美歯科全般", "06_審美歯科全般.csv"),
    ("07a", "⑦ 口腔外科", "07a_口腔外科.csv"),
    ("07b", "⑦ 審美系（ホワイトニング・ジルコニア・ラミネートベニア）", "07b_審美系.csv"),
    ("08", "⑧ 入れ歯・義歯（自費）・親知らず・根管治療", "08_入れ歯_親知らず_根管治療.csv"),
    ("09", "⑨ 一般歯科・クリーニング・定期検診・虫歯治療", "09_一般歯科.csv"),
)
CATEGORY_LABEL_BY_KEY = {key: label for key, label, _filename in CATEGORY_EXPORTS}

HUMAN_DECISION_LABELS = {
    "CONFIRMED": "✅ 正しいタグ",
    "REJECTED": "❌ このタグではない",
    "UNCERTAIN": "❓ 判断できない",
}


def _repo_for(store):
    return write_repositories_for(store).dental_sales_tags


def _tag_option_label(code):
    definition = DEFINITION_BY_CODE.get(code)
    if definition is None:
        return code
    return f"{definition.priority_group}｜{definition.label}"


def _sales_frame(rows):
    return pd.DataFrame([
        {
            "営業優先順位": row["priority_group"],
            "管理番号": row["id"],
            "UUID": row.get("uuid") or "",
            "医院名": row.get("clinic_name") or "",
            "電話番号": row.get("phone") or "",
            "住所": row.get("address") or "",
            "都道府県": row.get("prefecture") or "",
            "HP URL": row.get("hp_url") or "",
            "最優先タグ": row.get("tag_label") or "",
            "最優先タグコード": row.get("tag_code") or "",
            "全歯科営業タグ": row.get("labels") or "",
            "確度": float(row.get("confidence") or 0),
            "判定ソース": row.get("source") or "",
            "判定ルール": row.get("rule_id") or "",
        }
        for row in rows
    ])


def _effective_codes(row):
    raw = str(row.get("codes") or "")
    return {item.strip() for item in raw.split("/") if item.strip()}


def _exclusive_category_key(row):
    """Assign each dental clinic to exactly one sales category in the approved ①→⑨ order."""
    codes = _effective_codes(row)
    if "ALIGNER_INVISALIGN" in codes:
        return "01"
    if "ORTHODONTICS_GENERAL" in codes:
        return "02"
    if "ALL_ON_4" in codes:
        return "03"
    if "IMPLANT_GENERAL" in codes:
        return "04"
    if "CERAMIC" in codes:
        return "05"
    aesthetic_specific = {"WHITENING", "ZIRCONIA", "LAMINATE_VENEER"}
    if "AESTHETIC_GENERAL" in codes and not (codes & aesthetic_specific):
        return "06"
    if "ORAL_SURGERY" in codes:
        return "07a"
    if codes & aesthetic_specific:
        return "07b"
    if codes & {"PRIVATE_DENTURE", "WISDOM_TOOTH", "ROOT_CANAL"}:
        return "08"
    return "09"


def _category_evidence_labels(row, category_key):
    codes = _effective_codes(row)
    by_category = {
        "01": ("ALIGNER_INVISALIGN",),
        "02": ("ORTHODONTICS_GENERAL",),
        "03": ("ALL_ON_4",),
        "04": ("IMPLANT_GENERAL",),
        "05": ("CERAMIC",),
        "06": ("AESTHETIC_GENERAL",),
        "07a": ("ORAL_SURGERY",),
        "07b": ("WHITENING", "ZIRCONIA", "LAMINATE_VENEER"),
        "08": ("PRIVATE_DENTURE", "WISDOM_TOOTH", "ROOT_CANAL"),
        "09": ("CLEANING", "CHECKUP", "CARIES"),
    }
    matched = [
        DEFINITION_BY_CODE[code].label
        for code in by_category[category_key]
        if code in codes and code in DEFINITION_BY_CODE
    ]
    if category_key == "09" and not matched:
        matched = ["一般歯科"]
    return " / ".join(matched)


def _category_sales_frame(rows, category_key):
    label = CATEGORY_LABEL_BY_KEY[category_key]
    return pd.DataFrame([
        {
            "営業カテゴリ": label,
            "カテゴリ根拠タグ": _category_evidence_labels(row, category_key),
            "管理番号": row["id"],
            "UUID": row.get("uuid") or "",
            "医院名": row.get("clinic_name") or "",
            "電話番号": row.get("phone") or "",
            "住所": row.get("address") or "",
            "都道府県": row.get("prefecture") or "",
            "HP URL": row.get("hp_url") or "",
            "最優先タグ": row.get("tag_label") or "",
            "最優先タグコード": row.get("tag_code") or "",
            "全歯科営業タグ": row.get("labels") or "",
            "全タグコード": row.get("codes") or "",
            "確度": float(row.get("confidence") or 0),
            "判定ソース": row.get("source") or "",
            "判定ルール": row.get("rule_id") or "",
        }
        for row in rows
    ])


def _build_category_zip(rows):
    grouped = {key: [] for key, _label, _filename in CATEGORY_EXPORTS}
    for row in rows:
        grouped[_exclusive_category_key(row)].append(row)

    summary = pd.DataFrame([
        {"カテゴリ": CATEGORY_LABEL_BY_KEY[key], "件数": len(grouped[key])}
        for key, _label, _filename in CATEGORY_EXPORTS
    ])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "00_カテゴリ件数.csv",
            summary.to_csv(index=False).encode("utf-8-sig"),
        )
        for key, _label, filename in CATEGORY_EXPORTS:
            frame = _category_sales_frame(grouped[key], key)
            archive.writestr(filename, frame.to_csv(index=False).encode("utf-8-sig"))
    return buffer.getvalue(), {key: len(grouped[key]) for key in grouped}


def _summary_panel(repo):
    summary = repo.priority_summary()
    counts = {i: 0 for i in range(1, 10)}
    for row in summary:
        counts[int(row["priority_group"])] += int(row["clinics"])
    for start in range(1, 10, 3):
        cols = st.columns(3)
        for col, priority in zip(cols, range(start, min(start + 3, 10))):
            col.metric(PRIORITY_LABELS[priority], f"{counts[priority]:,}件")
    return counts


def _sales_list_tab(repo):
    st.markdown("#### 営業優先順位リスト")
    st.caption("医院は複数タグを保持し、最も優先度が高いタグを営業順位として表示します。Human Reviewの最新結果を反映します。")

    priority_options = list(range(1, 10))
    selected_priorities = st.multiselect(
        "営業優先順位",
        priority_options,
        default=priority_options,
        format_func=lambda value: PRIORITY_LABELS[value],
        key="dental_sales_priority_filter",
    )
    codes = [item.code for item in TAG_DEFINITIONS]
    selected_codes = st.multiselect(
        "歯科営業タグ（任意）",
        codes,
        default=[],
        format_func=_tag_option_label,
        key="dental_sales_tag_filter",
        help="複数選択はORです。未選択ならタグでは絞り込みません。",
    )

    count = repo.effective_count(
        priority_groups=selected_priorities,
        tag_codes=selected_codes,
    )
    st.metric("該当歯科医院", f"{count:,}件")

    preview = repo.list_effective_clinics(
        priority_groups=selected_priorities,
        tag_codes=selected_codes,
        limit=500,
    )
    frame = _sales_frame(preview)
    if frame.empty:
        st.info("この条件に該当する歯科医院はありません。")
    else:
        st.dataframe(
            frame,
            hide_index=True,
            width="stretch",
            column_config={
                "HP URL": st.column_config.LinkColumn(),
                "確度": st.column_config.NumberColumn(format="percent"),
            },
        )
        if count > len(frame):
            st.caption(f"画面は先頭{len(frame):,}件を表示しています。CSVは全{count:,}件を出力できます。")

    signature = json.dumps(
        {"priority": selected_priorities, "codes": selected_codes, "count": count},
        sort_keys=True,
        ensure_ascii=False,
    )
    if st.button(
        "①→⑨順のCSVを作成",
        type="primary",
        key="dental_sales_export_create",
        disabled=count <= 0,
    ):
        rows = repo.list_effective_clinics(
            priority_groups=selected_priorities,
            tag_codes=selected_codes,
            limit=100000,
        )
        export = _sales_frame(rows)
        st.session_state["_dental_sales_export"] = {
            "signature": signature,
            "csv": export.to_csv(index=False).encode("utf-8-sig"),
            "rows": len(export),
        }
    output = st.session_state.get("_dental_sales_export")
    if output and output.get("signature") == signature:
        st.download_button(
            f"歯科営業リストCSVをダウンロード（{output['rows']:,}件）",
            output["csv"],
            "dental_sales_priority.csv",
            mime="text/csv",
            use_container_width=True,
            key="dental_sales_export_download",
        )


    st.divider()
    st.markdown("##### カテゴリ別CSV一括出力")
    st.caption(
        "全有効歯科医院を①→⑨の優先順で1医院1カテゴリに振り分け、"
        "10個のCSV＋件数サマリーをZIPで作成します。現在の画面フィルタには依存しません。"
    )
    if st.button(
        "カテゴリ別CSVをZIPで一括作成",
        type="primary",
        key="dental_category_zip_create",
    ):
        all_rows = repo.list_effective_clinics(
            priority_groups=list(range(1, 10)),
            tag_codes=[],
            limit=100000,
        )
        zip_bytes, category_counts = _build_category_zip(all_rows)
        st.session_state["_dental_category_zip_export"] = {
            "zip": zip_bytes,
            "counts": category_counts,
            "rows": len(all_rows),
        }
    category_output = st.session_state.get("_dental_category_zip_export")
    if category_output:
        count_text = " / ".join(
            f"{key}:{category_output['counts'].get(key, 0):,}件"
            for key, _label, _filename in CATEGORY_EXPORTS
        )
        st.caption(f"振り分け結果：{count_text}")
        st.download_button(
            f"カテゴリ別歯科営業CSV ZIPをダウンロード（{category_output['rows']:,}医院）",
            category_output["zip"],
            "dental_sales_category_csv.zip",
            mime="application/zip",
            use_container_width=True,
            key="dental_category_zip_download",
        )


def _human_review_tab(repo):
    st.markdown("#### 歯科タグ Human Review")
    st.caption("誤判定を修正すると教師データとして履歴保存されます。自動再計算がHuman判定を上書きすることはありません。")

    mode = st.radio(
        "レビュー対象",
        ["要確認タグのみ", "自動確定タグも監査"],
        horizontal=True,
        key="dental_tag_review_mode",
    )
    include_reviewed = st.checkbox(
        "レビュー済みも表示",
        value=False,
        key="dental_tag_review_include_reviewed",
    )
    queue = repo.review_queue(
        only_review=mode == "要確認タグのみ",
        include_reviewed=include_reviewed,
        limit=200,
    )
    if not queue:
        if mode == "要確認タグのみ":
            st.success("現在、歯科タグの要確認候補はありません。HP調査が進むと曖昧なタグがここに入ります。")
        else:
            st.info("この条件でレビューできるタグはありません。")
        return

    option_keys = [(row["clinic_id"], row["tag_code"]) for row in queue]
    selected = st.selectbox(
        "確認するタグ",
        option_keys,
        format_func=lambda key: next(
            f"{row['priority_group']}｜{row['clinic_name']}｜{row['tag_label']}｜{row['confidence']:.0%}"
            for row in queue
            if (row["clinic_id"], row["tag_code"]) == key
        ),
        key="dental_tag_review_selected",
    )
    row = next(
        row for row in queue
        if (row["clinic_id"], row["tag_code"]) == selected
    )

    st.markdown(f"### {row['clinic_name']}")
    cols = st.columns(4)
    cols[0].metric("営業順位", str(row["priority_group"]))
    cols[1].metric("候補タグ", row["tag_label"])
    cols[2].metric("自動確度", f"{row['confidence']:.0%}")
    cols[3].metric("自動状態", row["auto_status"])
    st.write(f"**電話番号：** {row.get('phone') or '-'}")
    st.write(f"**住所：** {row.get('address') or '-'}")
    if row.get("hp_url"):
        st.link_button("公式HP候補を開く ↗", row["hp_url"], use_container_width=True)

    with st.container(border=True):
        st.markdown("##### 自動判定の根拠")
        st.write(f"**一致語：** {row.get('matched_alias') or '-'}")
        st.write(f"**ソース：** {row.get('source') or '-'}")
        st.write(f"**Rule：** {row.get('rule_id') or '-'}")
        st.caption(row.get("evidence_text") or "根拠テキストなし")

    if row.get("human_decision"):
        st.info(
            "最新Human Review："
            + HUMAN_DECISION_LABELS.get(row["human_decision"], row["human_decision"])
            + f" ／ 確認者 {row.get('reviewer') or '-'}"
        )
        if row.get("review_note"):
            st.caption("前回メモ：" + row["review_note"])

    reviewer_default = os.environ.get("CLINIC_REVIEWER_NAME", "")
    reviewer = st.text_input(
        "確認者",
        value=st.session_state.get("dental_tag_reviewer", reviewer_default),
        key="dental_tag_reviewer",
        placeholder="例：前川",
    )
    note = st.text_area(
        "確認メモ（任意）",
        key=f"dental_tag_review_note_{row['clinic_id']}_{row['tag_code']}",
        placeholder="正しい/誤りと判断した根拠",
    )

    buttons = st.columns(3)
    decisions = [
        ("CONFIRMED", "✅ 正しいタグ"),
        ("REJECTED", "❌ このタグではない"),
        ("UNCERTAIN", "❓ 判断できない"),
    ]
    for col, (decision, label) in zip(buttons, decisions):
        if col.button(
            label,
            key=f"dental_tag_decision_{row['clinic_id']}_{row['tag_code']}_{decision}",
            use_container_width=True,
            disabled=not reviewer.strip(),
        ):
            repo.save_review(
                clinic_id=row["clinic_id"],
                tag_code=row["tag_code"],
                human_decision=decision,
                reviewer=reviewer,
                review_note=note,
            )
            st.session_state["_dental_tag_flash"] = (
                f"{row['clinic_name']} / {row['tag_label']} を"
                f"{HUMAN_DECISION_LABELS[decision]}として保存しました。"
            )
            st.rerun()


def _analytics_tab(repo):
    st.markdown("#### 教師データ・精度")
    rows = repo.training_stats()
    if not rows:
        st.info("歯科タグのHuman Reviewデータはまだありません。")
        return
    frame = pd.DataFrame(rows)
    denominator = frame["confirmed"] + frame["rejected"]
    frame["Human正解率"] = [
        (confirmed / decided) if decided else None
        for confirmed, decided in zip(frame["confirmed"], denominator)
    ]
    frame = frame.rename(columns={
        "tag_code": "タグ",
        "rule_id": "Rule",
        "reviewed": "レビュー数",
        "confirmed": "正しい",
        "rejected": "誤り",
        "uncertain": "判断不能",
    })
    st.dataframe(
        frame[["タグ","Rule","レビュー数","正しい","誤り","判断不能","Human正解率"]],
        hide_index=True,
        width="stretch",
        column_config={"Human正解率": st.column_config.NumberColumn(format="percent")},
    )
    st.info(
        "レビュー結果は教師データとして蓄積します。新しい自動判定ルールは本番へ即時反映せず、"
        "Shadow評価でPrecisionを確認してからAUTO_ACCEPTへ昇格させます。"
    )


def dental_sales_tags_page(store):
    st.subheader("歯科営業タグ")
    st.caption("HP調査 → 歯科タグ → 営業優先順位 → リスト出力を一元管理します。")

    if not getattr(store, "is_supabase_runtime", False):
        st.warning("歯科営業タグ画面はSupabase Runtime専用です。")
        return
    repo = _repo_for(store)
    if repo is None or not repo.available():
        st.warning("歯科営業タグSidecarがまだ準備されていません。")
        return

    flash = st.session_state.pop("_dental_tag_flash", None)
    if flash:
        st.success(flash)

    _summary_panel(repo)
    sales_tab, review_tab, analytics_tab = st.tabs([
        "①→⑨ 営業リスト",
        "Human Review",
        "学習・精度",
    ])
    with sales_tab:
        _sales_list_tab(repo)
    with review_tab:
        _human_review_tab(repo)
    with analytics_tab:
        _analytics_tab(repo)
