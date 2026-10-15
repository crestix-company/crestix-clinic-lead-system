# Email Auto Research: Mac単一Runtime反映Runbook

対象Runtime: `/Users/maekawahiroyuki/ClinicLeadRuntime`
正式ポート: `8502`（旧環境 `8501` は触らない）
継続対象Run ID: `3f967987075240bba183205cccd3eb8b`

このRunbookは、変更がレビュー・CI・承認を経て `main` に入った後に人間が実施する。
この開発作業中にはProduction反映、プロセス停止、Run再開を行わない。

## 反映前ゲート

1. GitHub CIが成功し、承認済み変更が`main`へmerge済みであることを確認する。
2. Production DB migrationが不要であることを確認する。本変更は既存schemaだけを使用する。
3. HP Autoが`RUNNING`、`WAITING_NETWORK`、復旧中ではないことを確認する。HP Autoを止めて反映時間を作らず、自然な停止・既にPAUSEDの保守時間まで待つ。
4. Email Autoが次の状態であることをUIまたはSELECT-only確認で照合する。
   - Run ID: `3f967987075240bba183205cccd3eb8b`
   - status: `PAUSED`
   - `stop_requested=false`
   - FOUND / REVIEW / NO_EMAIL_FOUND / ERROR / NOT_RESEARCHEDの件数を記録する
5. Runtimeのworking treeがcleanで、originが正式repositoryであることを確認する。
6. `config/email_research.env.local`（mode `600`）を確認する。秘密値は画面やログへ出さない。
   - `EMAIL_RESEARCH_WORKERS=8`
   - `EMAIL_RESEARCH_CONNECT_TIMEOUT_SECONDS=10`
   - `EMAIL_RESEARCH_READ_TIMEOUT_SECONDS=20`
   - `EMAIL_RESEARCH_RETRY_COUNT=1`
   - `EMAIL_RESEARCH_CLAIM_LEASE_SECONDS=300`
   - `EMAIL_AUTO_RUN_ENABLED=true`

`EMAIL_AUTO_RUN_ENABLED=true`は操作ボタンを有効にするだけで、PAUSED Runを自動再開しない。

## 反映

1. 8502の既存Streamlitを終了してよい保守時間であることを再確認する。8501とHP Autoの別プロセスには触らない。
2. Desktopの正式launcher、またはRuntime内の次のlauncherを1回だけ実行する。

   ```bash
   /Users/maekawahiroyuki/ClinicLeadRuntime/scripts/update_and_launch_mac.sh
   ```

3. launcherはcleanな`main`だけをfast-forwardし、正式8502を起動する。二重起動時はprocess lockで拒否される。
4. `http://127.0.0.1:8502/` を開き、Clinic/HP画面とEmail画面がread-only表示できることを確認する。

## 再開前照合（まだ再開しない）

1. Email Autoが`PAUSED`のまま、Run IDが上記と一致することを確認する。
2. 反映前に記録した終端件数（FOUND / REVIEW / NO_EMAIL_FOUND）とPrimary件数が減っていないことを確認する。
3. Worker表示が`8`、controller表示が停止中、残件数が想定範囲であることを確認する。
4. 8502 launcherをもう一度実行した場合に「二重起動」として拒否されることを確認する（既存processは終了しない）。

## 明示承認後のRun再開

Production Email Auto再開の個別承認を得た人間だけが、8502 UIの「再開」を1回押す。
「開始」は新規Runを作るため押さない。再開直後に以下を確認する。

1. Run IDが`3f967987075240bba183205cccd3eb8b`のまま`RUNNING`になる。
2. controller lease / heartbeatが更新され、controllerが1つだけになる。
3. 8 Workerでclinic_idの重複Claimがない。
4. FOUND / REVIEW / NO_EMAIL_FOUNDの既存件数が減らず、NOT_RESEARCHEDだけが進む。
5. DBエラー、timeout、DNS、SSL、429、5xxは医院単位に記録され、他Workerが進み続ける。

異常時は新規Runを作らず「一時停止」を使用し、Run IDと終端結果を保持したまま原因を確認する。
