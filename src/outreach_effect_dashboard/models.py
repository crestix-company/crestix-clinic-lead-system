"""One row of the existing 案件管理 sheet, reduced to the columns this dashboard reads.

Field names/semantics follow docs/outreach_ab_tracking_design.md §2.2, derived from read-only
inspection of the real sheet. Column mapping is not confirmed by the pipeline's own source code
(unavailable -- see that doc's §1), so field meanings are best-effort from observed sheet data.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class SendRecord:
    case_id: str  # 既存シートの「案件ID」。このダッシュボードでは生成も変更もしない。
    is_staff: str  # IS担当
    template_number: str  # 既存の「テンプレート」番号。文字列のまま保持し、将来のvariant_idとは混在させない。
    sent: bool  # 送信有無 == 送信済み
    sent_at: datetime | None  # 送信日時
    replied: bool  # 返信有無
    reply_at: datetime | None  # 初回返信日時
    open_estimated: bool  # 開封推定レベルが「可能性あり」以上（広義）
    open_estimated_at: datetime | None  # 初回画像読込日時（開封推定の起点として採用）
    open_high_confidence: bool  # 高信頼開封
    open_high_confidence_at: datetime | None  # 初回高信頼シグナル日時
    clicked: bool  # 記事クリック有無
    clicked_at: datetime | None  # 初回クリック日時
    appointment: bool  # メール経由アポ
    appointment_at: datetime | None  # アポ日時
