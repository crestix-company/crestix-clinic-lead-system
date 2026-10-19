# Clinic Lead System - Gmail下書き一括保存（Phase 1）

## 概要
メール送付文章の新しい⑤「下書き一括保存」で、条件を満たす医院を自動選別し、明示的な一括承認後に最大25医院を順次Gmail下書きへ保存する。
既存の①〜④タブは変更せず維持。Gmail送信・予約送信・無人の定期実行は行わない。

## 自動選別ルール
- READYかつ件名・本文あり
- Clinic ID / UUID / 医院名が確認済み、院長名確認済み
- Email Research FOUND、正規の宛先メールアドレス
- 記事はLISTEDかつNORMAL、またはNOT_LISTEDが確認済み
- UNCONFIRMED、PR、SUPERVISED、その他未確認は除外
- 候補全件で医院ID、UUID、宛先メールが重複しない
- 最大25件。超過分は次回の履歴再照合・生成へ
- 生成日が当日で、日程候補・テンプレート・送付履歴を確認済み

## 安全な操作
1. ①対象選択でClinic Master/Email Research/記事JOIN/外部履歴CSV/Gmail送付履歴を照合。
2. ②文章生成で候補日時・内容を確定。
3. ⑤下書き一括保存で候補一覧と保留理由を閲覧。
4. 日時・本文・宛先・記事根拠と過去履歴照合を確認し、CREATE_N_GMAIL_DRAFTSを入力。
5. 一度のクリックで既存の create_gmail_draft を1件ずつ実行（毎医院Gmail/DBを再照合）。
6. UNKNOWN・REVIEW・DISABLED・例外が発生したら停止。同じ生成バッチは再実行禁止。

## 実装
- src/outreach/auto_draft_batch.py: 純粋な事前選別、重複検知、1～25件の安全な順次実行。
- src/outreach/auto_draft_ui.py: 1回承認のStreamlit UI。
- src/outreach_ui.py: ⑤タブ追加のみ。既存④タブの手動機能はそのまま。
- tests/test_auto_draft_batch.py: 重複、宛先衝突、記事未確認、上限、例外停止のモックテスト。

## 限界と残課題
- 既存 Gmail workflow が自動照合するSentは当日JSTであり、過去の送信済み全期間、Trash、旧GAS案件、59件のHOLDを、この変更だけで網羅したことにはならない。人手で全履歴を確認し、履歴CSVに反映すること。
- Gmail OAuth、送付履歴DB専用ロール、リリース権限が整うまではライブ保存不可。
- OUTREACH_GMAIL_DRAFTS_ENABLED は既定OFFのまま。勝手にONにしない。
- Production DB変更・Migration・GAS本番変更・既存Jobs操作・実Gmail作成/送信はこのPRでは実行しない。

## 次工程
過去全期間の重複チェック、Gmail 59 HOLDの解消、専用DBロール検証、許可済み小規模Canary。無人定期実行はその後に別途設計する。
