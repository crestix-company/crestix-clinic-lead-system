# Test Matrix — Cloud Run parity (Issue #126)

## 対象テストと結果（PR #127ブランチ、`.venv` Python 3.12 で実行）

| テストファイル | 内容 | 結果 |
|---|---|---|
| `tests/test_cloudrun_parity_menu_visibility.py`（新規） | NAV[:2]ピン留め不変、静的テキストのみ追加、write呼び出し不在、母集団help文言 | PASS (6) |
| `tests/test_simple_workflow_cleanup.py` | NAV[:2]/早期return/write_disabled等の既存ピン留め | PASS |
| `tests/test_outreach_read_only_isolation.py` | Gmail下書きの二重ゲート（NAV隠蔽＋Feature Flag） | PASS |
| `tests/test_cloud_run_read_only_isolation.py` | HP Job制御ボタンがREAD ONLYから到達不能 | PASS |
| `tests/test_cloud_run_read_only_export.py` | READ ONLY CSV経路の列・件数契約 | PASS |
| `tests/test_cloud_run_config.py` | Cloud Run環境フラグの解釈 | PASS |
| `tests/test_v2_app.py` | app_v2全般の既存回帰 | PASS |
| `tests/test_email_auto_run.py` | Email自動実行フラグ回帰 | PASS |

対象テスト合計: 82 passed, 0 failed。

## フルスイート

`python -m pytest -q` （PR #127ブランチ）: **2211 passed, 55 skipped, 0 failed**。
既存のskip（Supabase実接続が必要なテスト等）はPR作成前から存在し、本変更による新規skipはなし。

## 実施していないテスト

- 実Canary/本番ブラウザでのIAP許可・拒否、全国検索、Comdeskファイル実機照合（別ステップ、未確認）。
- P2のWriter/Job系コードは既存PR（#104-#106, #116）のものをそのまま使用し新規コードを追加しなかったため、
  新規のモック/合成データテストも追加していない。既存のテストスイートに追加変更なし。
