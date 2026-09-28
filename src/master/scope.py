"""営業対象の母集団（scope）を一元管理する。

旧13,970件（国内展開＝全国append前に取り込まれた既存営業リスト）と、
全国Clinic Master（162,258件）を切り替えるための内部値・表示名・境界値をここに集約する。
同じcutoff timestampを複数fileへ重複記述しない。
"""

SCOPE_ALL = "ALL"
SCOPE_LEGACY_PRE_NATIONAL = "LEGACY_PRE_NATIONAL"

# 全国append分（148,288件）の first_seen_at と同一のタイムスタンプ。
# legacy cohort（13,970件）は "first_seen_at < この値" で一致する（境界は必ず"<"）。
LEGACY_PRE_NATIONAL_CUTOFF = "2026-09-28T02:26:53.465716+00:00"

SCOPE_LABELS = {
    SCOPE_ALL: "全国Clinic Master",
    SCOPE_LEGACY_PRE_NATIONAL: "既存営業リスト",
}

SCOPE_VALUES = tuple(SCOPE_LABELS)
