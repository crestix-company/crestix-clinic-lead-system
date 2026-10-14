# Clinic Lead System Autonomous Pipeline

## Overall

**BLOCKED**

HP Auto、Email Auto、Outreach は local fixture / mock による受入と全回帰を通過した。Treatment は既存 taxonomy、増分候補選定、再解析判定、coverage KPI、既存 worker の失敗分離・再試行・冪等な置換を維持したが、現行 `treatment.*` schema には Human 確定由来を識別して保護する列、および要求された evidence / confidence の完全な provenance 列がない。Production migration と schema 変更が禁止されているため、安全でない自動永続化接続は行っていない。

Git worktree metadata への書込み権限を明示承認した環境で `index.lock` 問題を解消し、監査済み差分を Stage ごとの意味単位で commit した。

Stage 0 分類:

- KEEP: 完成済み領域と既存意味論
- FIX: HP Auto stale controller recovery、Email Auto durable controller / stale claim recovery
- EXTEND: Treatment 増分選定・KPI、PR #67 Outreach の current main への選択的 port
- BLOCKED: Treatment Human override / 完全 provenance を保護した自動書込み接続

## Git

- branch: `feat/autonomous-pipeline-hardening-20261014-213430`
- functional HEAD（status 文書 commit 前）: `c4b1e1b`
- base SHA (`origin/main`): `84c13ae1294b26322ed4c5655c7556489132646d`
- inspected Outreach source branch: `feat/outreach-email-template-v1` at `075f5999bf3bd06bfc4849c60888d10474e06567`
- commits created: 4（status 文書 commit 前）
  - `61983a8` fix: harden HP auto controller recovery
  - `999bfda` fix: make email auto runs recoverable
  - `7d700fd` feat: add incremental treatment coverage analysis
  - `c4b1e1b` feat: add read-only outreach email generation
- `git diff --check`: PASS

## KEEP

次の完成済み仕様は変更していない。

- 厚生局データ取得
- Clinic Master、UUID / Identity、重複整理
- Google Maps 調査、HP 候補取得、公式 HP 判定
- HP Human Review と Human Review semantics
- 既存診療科分類
- Email Candidate 判定、Email Human Review、Primary Email
- 営業対象 filter
- Comdesk CSV columns、Comdesk 出力、現在の架電運用
- HP Rank semantics
- 既存 Treatment taxonomy / evidence engine / status vocabulary
- 正式 Outreach 固定テンプレート v1 の本文

## HP Auto

### 変更内容

- persisted controller lease の期限を UI / state helper から `ACTIVE` / `RECOVERABLE` と判定可能にした。
- lease 期限切れ後は同じ `run_id` / `current_job_id` を維持して takeover できる既存設計を保持した。
- 期限切れ owner の heartbeat による lease 復活を拒否し、controller fencing を強化した。
- DONE item、未期限の他 worker lease、既存結果を維持する既存 job/item contract は変更していない。
- stale `RUNNING` を UI で「復旧待ち（controller lease期限切れ）」として表示する。
- 既存の pause / resume / cancel / `WAITING_NETWORK` recovery を維持した。

### tests

- focused HP suite: 37 passed
- expired heartbeat rejection
- expired lease takeover with same run / current job
- active lease health
- existing normal batch / pause / resume / crash / fencing / duplicate prevention / network recovery regression

### acceptance

PASS（local fixture / mock）。Production の HP Auto start / resume は 0 回。

## Email Auto

### 変更内容

- migration なしで既存 `email_auto_runs.metrics` JSONB に controller id、lease、heartbeat、epoch を永続化した。
- controller acquire / heartbeat / finish を owner と lease expiry で fence した。
- stale run は新規 run を作らず、同じ `run_id` / scope / cohort で recover する。
- worker ごとに専用 repository connection を所有して必ず close し、global connection を共有しない。
- claim 処理中は専用 heartbeat connection で `claim_token` を更新し、lease を失った worker の確定書込みを拒否する。
- bounded worker count、bounded retry、DONE / FOUND / REVIEW / NO_EMAIL_FOUND の既存終端状態を維持した。
- cohort file の missing / invalid は既存 fail-closed contract を維持し、unrestricted fallback を追加していない。
- controller lease が切れた `RUNNING` を UI で recoverable と表示し、同一 run の自動回復入口へ接続した。

### tests

- focused Email suite: 87 passed
- 8 workers
- controller acquire / heartbeat / expiry / fencing
- worker claim heartbeat / expired claim recovery
- same-run recovery
- existing crash / pause / resume / duplicate prevention / cohort fail-closed / timeout retry / retry exhaustion regression

### acceptance

PASS（local fixture / mock）。Production の Email Auto start / resume は 0 回。

## Treatment

### 変更内容

- `treatment_taxonomy.py` の taxonomy / rule version / evidence engine を Source of Truth のまま利用する。
- HP ledger の成功時刻と Treatment checkpoint / version を比較する SELECT-only 増分候補選定を追加した。
- 新規、HP 更新、taxonomy 更新、evidence engine 更新のみを再解析対象にする。
- 既存 worker の医院単位 failure isolation、retry、成功時の clinic 単位 atomic replace、fetch failure 時の既存成功結果保護を維持した。
- `HP VERIFIED`、researched、found、review、error、unresearched の coverage KPI を SELECT-only で取得可能にした。
- repository が所有する connection の close 責務を追加した。

### tests

- focused Treatment suite: 189 passed
- new / unchanged / updated HP / version change
- no duplicate write path
- confidence routing
- source URL / version / researched_at provenance
- SELECT-only candidate / KPI query
- existing worker retry / failure isolation / atomicity regression

### acceptance

**BLOCKED**。増分選定と KPI は PASS。現行 schema だけでは Human 確定行を自動行と区別して上書き防止できず、要求された evidence / confidence の完全な provenance も保存できない。この状態で HP Auto / HP Human Review から canonical Treatment 書込みへ自動接続すると B-4 を保証できないため、接続していない。

## Outreach

### 変更内容

- PR #67 を blind merge せず、Outreach 実装、設定、SELECT-only canary / audit、tests だけを current main ベースへ port した。PR 内の migration / grant SQL は port していない。
- sidebar / navigation に「メール送付文章」を追加し、既存 navigation を維持した。
- 対象選択、文章生成、プレビュー、生成結果の 4 tabs を接続した。
- Clinic Master、Primary Email、記事原本、記事 JOIN は READ ONLY。
- 掲載確認済み記事だけを掲載済みと表現し、profile / 未確認は掲載済みと断定しない。
- 院長名未確認は「院長先生」+ `NEEDS_REVIEW`、email 未取得は `NEEDS_REVIEW`、Clinic UUID 重複は `FAILED`。
- 記事 URL は canonical source にある `https://clinic.mynavi.jp/article/` のみ許可する。
- 電話済み選択時だけ電話文言を使う。
- IS 担当者と候補日時を設定でき、土日、重複、不正日付、不正時刻を拒否する。
- 50 / 100 / 500 / 1000 件を含む上限を実装し、CSV に identity、宛先、recipient、article、subject、body、status、review reason、template version、batch id を含める。
- Gmail draft、メール送信、DB write は実装していない。出力は CSV download のみ。

### tests

- focused Outreach suite: 82 passed
- 1 / 50 / 100 / 500 / 1000 件
- UUID duplicate、missing email、missing director
- listed / not listed / unconfirmed / invalid URL / no fabricated article
- phone-called true / false、date validation、CSV columns
- no Gmail side effect、no DB write、SELECT-only reader / canary

### acceptance

PASS（local fixture / mock、READ-only design）。

## Full Regression

- passed: 1759
- failed: 0
- skipped: 55
- warnings: 118（既存 Streamlit / pandas deprecation warnings）
- command: `../clinic-email-human-review-v2/.venv/bin/python -m pytest -q`
- runtime: Python 3.12 test venv

Handoff 再検証（2026-10-15 JST）:

- HP changed-path suite: 23 passed
- Email broad suite: 257 passed
- Treatment broad suite: 327 passed
- Outreach focused suite: 82 passed
- full suite: 1759 passed, 55 skipped, 0 failed, 118 warnings

KEEP 領域を含む全 test suite が PASS。システム Python 3.9 は repository の Python 3.10+ type syntax を解釈できないため、依存導入済み Python 3.12 venv を使用した。

## Safety

- Production writes: 0
- Production migration: 0
- Gmail writes: 0
- Email sends: 0
- Comdesk writes / output: 0
- HP Auto start / resume: 0
- Email Auto start / resume: 0
- Production SELECT: 0（Production credential を使用していない）
- credential / secret / DSN exposure: 0
- destructive operations: 0
- completed KEEP features modified unnecessarily: 0

すべて local fixture / mock / isolated temporary DB で検証した。

## Remaining blockers

1. Treatment の Human 確定 provenance と confidence / evidence を既存 row と安全に共存させる canonical schema / policy の決定が必要。Production migration が禁止された今回の run では解消不可。

## Next production acceptance

1. 人間が Treatment の Human override Source of Truth と保存列を決定し、別途承認された migration / policy を適用する。
2. staging / isolated DB で Treatment の HP 更新 → 増分 claim → canonical write → Human override preservation を確認する。
3. staging で HP controller を PC-A / PC-B から同時起動し、Active 1 台 / Standby 1 台、process kill、lease expiry、同一 run takeover、DONE 非再実行を確認する。
4. staging で Email 8 worker kill、8 expired claims 回収、controller takeover、同一 run / scope / cohort、cohort missing fail-closed を確認する。
5. Production では権限を分離した SELECT-only Outreach canary を人間が確認し、CSV の記事 URL、宛名、電話済み表現、候補日時をレビューする。
6. Production の HP / Email Auto start / resume は、この run 外で明示承認を得た人間が実施する。
