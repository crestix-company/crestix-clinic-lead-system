# Mac HP ERROR Canary runbook

この手順は、レビュー済みの10医院だけを対象に単発HP Jobを作成・実行するためのものです。各DB write操作は別の明示承認が必要です。この文書を追加したPR #146の段階では、Canary Job登録・Worker起動は行いません。

## 使う実装

- Job登録: `write_repositories_for(store).auto_hp.create_error_canary_job(clinic_ids)`。Supabaseの既存writer経路を使い、同一トランザクション内でAuto Run状態・既存HP Job・ERROR状態・対象claimを検査します。登録後のJob初期状態はDB既定の`PAUSED`です。Workerは起動しません。
- Worker: `JobRunner.start(store, job_id, provider)`。これは`app_v2.py`の通常Job開始・再開経路と同じ呼出しです。`run_job`を直接呼ぶ手順にはしません。
- Provider: 通常画面と同じ`TavilySearchProvider`です。Maps公式HP URLがある対象は既存のSafeFetcherで直接取得するためAPIキーは不要です。検索APIが必要な対象でキーが未設定なら外部検索せず、その医院をエラー記録して次の対象へ進みます。
- 対象10件はすべてMaps確認済みウェブサイトURLを持つため、HP調査コードは検索APIを使わずそのURLを直接取得します。URLがなく検索フォールバックが必要な医院では、Providerキー未設定なら外部通信せず医院単位のエラーとして記録します。

## 1. 実行前監査

作業ツリーと起動経路を確認します。作業中に既存PAUSED Job、Auto Run、医院UUIDは変更しません。

```bash
git status --short --branch
```

最新状態をREAD ONLY監査し、出力先に既存ファイルがないことを確認してから保存します。スナップショットには医院情報が入るため、リポジトリ外に保存します。

```bash
.venv/bin/python /private/tmp/clinic_hp_phase1_snapshot.py --output /private/tmp/clinic_hp_canary_before.json
chmod 600 /private/tmp/clinic_hp_canary_before.json
```

Job登録前に次を照合します。

1. Auto Runが`COMPLETED`または`IDLE`で、controller leaseがない。
2. HP `RUNNING` Jobがない。
3. 対象IDが全件ERRORで、Job候補プレビューの公式HP/Maps URLと今回承認されたID一覧が一致する。
4. 対象IDが既存HP Jobの`PENDING`/`RUNNING` itemに存在しない。既存PAUSED Jobの99件はそのまま保持する。
5. Supabase接続が正しいプロジェクトで、設定ファイルの権限が600。秘密値は表示しない。

不一致、DB接続失敗、Reader/Writer経路の異常、lease不明、別Jobの実行、または候補のERROR解除があれば停止し、対象一覧を作り直します。Canary対象IDは監査後に動的に差し替えません。

## 2. Job登録（DB write・別途承認後のみ）

通常起動時の環境読込を行い、固定したID 10件だけを渡します。Job IDは監査記録に保存します。ここではWorkerを起動しません。

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
from scripts.launch_v2 import load_runtime_env
from src.repository.runtime_store import SupabaseRuntimeStore
from src.repository.write_backend import write_repositories_for

load_runtime_env(Path.cwd())
store = SupabaseRuntimeStore()
clinic_ids = [133, 147, 149, 156, 162, 163, 178, 310, 317, 327]
job_id = write_repositories_for(store).auto_hp.create_error_canary_job(clinic_ids)
print(f"Canary job_id={job_id}")
PY
```

登録が拒否された場合はエラーを回避せず終了します。新しい候補を暗黙に選び直したり、既存Jobを変更したりしません。

登録後、Workerを開始する前に、返されたJob IDのstatusが`PAUSED`、item数が10、clinic ID集合が上記10件と一致し、全itemが`PENDING`であることをREAD ONLYで確認します。異なる場合はWorkerを起動しません。

## 3. Worker開始（別途承認後のみ）

Job IDと対象10件を再確認し、実行直前のREAD ONLY監査をやり直します。Maps公式HP URLを持つCanaryではProvider APIキーは不要です。検索APIが必要な場合だけ、すでに承認・設定済みのProviderを利用し、キー未設定なら外部検索しません。

```bash
.venv/bin/python - <<'PY'
import os
import time
from pathlib import Path
from scripts.launch_v2 import load_runtime_env
from src.enrichment.search_provider import TavilySearchProvider
from src.master.jobs import JobRunner, job_status
from src.repository.runtime_store import SupabaseRuntimeStore

job_id = "JOB_IDを登録結果で置換"
load_runtime_env(Path.cwd())
# Maps HP URLを直接取得するCanaryではTAVILY_API_KEYは不要です。
# URLがなく検索が必要な対象は、キー未設定なら医院単位で失敗記録し次へ進みます。

store = SupabaseRuntimeStore()
provider = TavilySearchProvider()
runner = JobRunner()
runner.start(store, job_id, provider)
while runner.running():
    time.sleep(1)

status = job_status(store, job_id)
print({"status": status["status"], "counts": status["counts"], "total": status["total"]})
if status["status"] != "COMPLETED":
    raise SystemExit("Canaryは完了していません。自動再起動せず監査してください。")
PY
```

完了までプロセスを開いたままにします。Ctrl-Cやターミナル終了で強制停止しません。例外、認証エラー、権限エラー、lease異常、対象10件以外のitemが見えた場合は停止し、再開しません。

## 4. 一時停止・再開

一時停止は対象Canary Job IDだけに実行します。Job statusを`PAUSED`にするとWorkerは現在の処理境界で停止します。進行中の医院が1件完了してから停止する場合があるため、停止要求だけで停止完了とみなしません。

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
from scripts.launch_v2 import load_runtime_env
from src.master.jobs import job_status
from src.repository.runtime_store import SupabaseRuntimeStore
from src.repository.write_backend import write_repositories_for

job_id = "CANARY_JOB_ID"
load_runtime_env(Path.cwd())
store = SupabaseRuntimeStore()
repos = write_repositories_for(store)
repos.jobs.pause_job(job_id)
print({"status": job_status(store, job_id)["status"], "job_id": job_id})
PY
```

Workerプロセスが終了し、Jobが`PAUSED`、`RUNNING` itemが0件、対象のleaseが残っていないことを確認します。プロセス異常終了後にleaseが残る場合は、最長15分のclaim leaseが期限切れになるまで再開しません。

再開には同じJob IDと同じProvider設定を使い、実行前監査と別途承認をもう一度行ってから、上のWorker開始手順を再実行します。`run_job`内部の既存回復処理が期限切れclaimを回収します。`reset_job`は未処理itemをCANCELLEDにするため、このCanaryの停止・再開に使いません。

## 5. 実行後監査

Workerプロセス終了後に、Job IDのstatus/countsが完了を示し、処理対象が固定した10医院のみであることを照合します。`DONE`以外、10件以外のitem、lease残存、UUID差分があれば結果を確定扱いにしません。

```bash
.venv/bin/python /private/tmp/clinic_hp_phase1_snapshot.py --output /private/tmp/clinic_hp_canary_after.json
chmod 600 /private/tmp/clinic_hp_canary_after.json
```

個別item IDとHP ledger値を照合するには、下のSELECTを新しいREAD ONLYセッションで実行します。`transaction_read_only=on`を確認してから照会してください。Writerロールしか使えない場合もセッションをREAD ONLYに強制し、設定値を確認してから実行します。

```sql
SELECT c.id, i.state, (NULLIF(i.lease_until,'')::timestamptz > clock_timestamp()) AS lease_active,
       h.fetch_status, h.attempts, h.researched_at, h.hp_url, h.final_url,
       h.machine_hp_rank, h.error_detail
FROM research.research_job_items i
JOIN public.clinics c ON c.id=i.clinic_id
LEFT JOIN hp_research.clinic_hp_research h ON h.clinic_id=c.id
WHERE i.job_id = :canary_job_id
ORDER BY c.id;
```

前後のDB全体件数、Auto Run状態、Job状態、対象10件のledger状態・試行回数・URL・rankを比較し、対象外医院に差分がないことを確認します。比較レポートとJob IDはリポジトリ外に権限600で保存します。監査が終わるまで次のJobを作成しません。
