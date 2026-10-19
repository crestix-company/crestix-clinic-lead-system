# Cloud Run HP Job制御用Postgresロール設計（計画のみ・未実行）

最終更新: 2026-10-19

## この文書の位置づけ

本文書は**設計・計画のみ**であり、以下は一切実行していない。

- `CREATE ROLE` / `GRANT` / `REVOKE` の実行
- Migration・DDLの実行
- Supabase/Postgresへの実際の接続・変更

実際のロール作成・権限付与は人間がSupabase管理画面またはDBA権限を持つ接続から
別途実施すること。下記SQLは**レビュー用の案**であり、そのままコピー＆ペーストして
実行する前に、対象環境（Staging/本番）でテーブル・スキーマ名が一致するか、
他の用途でのロール再利用がないかを必ず確認すること。

## 背景・対応するコード

`src/cloud_run/job_control.py`（`CLINIC_HP_EXECUTOR=cloud_run`時にHP Job作成・
一時停止・再開が使う接続）は`SUPABASE_JOB_CONTROL_DB_URL`を**フェイルクローズ**で
要求する（未設定時に一般DB接続`SUPABASE_RUNTIME_DB_URL`へ暗黙フォールバックしない）。
この接続に紐づくPostgresロールの権限設計を以下に示す。

既存の3ロール（現行コードが前提とする、Phase Aまでで確立済み）と、今回追加するロールの関係：

| ロール（想定名） | 用途 | 接続元 | 権限レベル |
|---|---|---|---|
| `clinic_web_readonly` | Phase A: Cloud Run Web（READ ONLY画面） | `CLINIC_READ_ONLY_UI=true`時の`SUPABASE_RUNTIME_DB_URL` | SELECT専用（セッションレベルでも`default_transaction_read_only=on`を強制。src/repository/supabase_adapter.py） |
| `clinic_write_general`（既存） | Mac版・通常のCloud Run Write（Comdesk取込・age model更新等） | `CLINIC_WRITE_BACKEND=supabase`時の`SUPABASE_RUNTIME_DB_URL` | 既存の広い書込権限（本設計の対象外、変更しない） |
| `clinic_hp_worker`（既存、Mac版と共用） | `scripts/cloud_hp_worker.py` / Mac版Job実行 | Worker自身の`SUPABASE_RUNTIME_DB_URL` | 調査結果・Job進捗の書込（本設計の対象外、変更しない） |
| **`clinic_hp_job_control`（新規・本文書の対象）** | Cloud Run Web（`CLINIC_HP_EXECUTOR=cloud_run`時のJob作成・一時停止・再開のみ） | `SUPABASE_JOB_CONTROL_DB_URL` | 下記の最小権限 |

## `clinic_hp_job_control` が実際に実行するSQL（コード調査に基づく）

`src/cloud_run/job_control.py`経由で呼ばれる既存リポジトリメソッドが発行するSQLを
列挙する（推測ではなく、以下のファイルを実際に確認した結果）。

### `auto_hp.create_single_job()` （`src/repository/hp_auto_run_repository.py`）
```sql
-- ロック（候補確保の直列化、Auto Run状態の排他） -- テーブルアクセスではなく組込関数
SELECT pg_advisory_xact_lock(hashtext(%s));

-- Auto Run状態の確認
SELECT value FROM app_config.settings WHERE key=%s FOR UPDATE;

-- 候補医院の確保（FOR UPDATEでの行ロック取得にはSELECT権限に加えUPDATE権限が必要）
SELECT c.id FROM public.clinics c WHERE ... ORDER BY ... LIMIT %s FOR UPDATE OF c SKIP LOCKED;

-- Job作成
INSERT INTO research.research_jobs(id,kind,options_json,max_searches,created_at,updated_at)
  VALUES(%s,'hp',%s,0,%s,%s);
INSERT INTO research.research_job_items(job_id,clinic_id) VALUES(%s,%s);
```

### `jobs.pause_job()` / `jobs.job_status()` / `jobs.recent_jobs()` （`src/repository/supabase_write_adapter.py`）
```sql
UPDATE research.research_jobs SET status='PAUSED',updated_at=%s WHERE id=%s AND status<>'COMPLETED';
SELECT * FROM research.research_jobs WHERE id=%s;
SELECT state,count(*) FROM research.research_job_items WHERE job_id=%s GROUP BY state;
SELECT result,count(*) FROM research.research_job_items WHERE job_id=%s AND state='DONE' GROUP BY result;
SELECT * FROM research.research_jobs WHERE status<>'RESET' ORDER BY created_at DESC,id DESC LIMIT 20;
```

### `src/cloud_run/dispatch_state.py` / `src/cloud_run/usage_budget.py`（本PRで追加）
```sql
INSERT INTO app_config.settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO NOTHING;
INSERT INTO app_config.settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value;
SELECT value FROM app_config.settings WHERE key=%s [FOR UPDATE];
UPDATE app_config.settings SET value=%s WHERE key=%s;
-- dispatch_state.may_dispatch()のJob状態確認
SELECT status FROM research.research_jobs WHERE id=%s;
```

## 必要な最小権限（案）

| スキーマ.テーブル | 必要な操作 | 理由 |
|---|---|---|
| `public.clinics` | `SELECT`、**`UPDATE`（候補列、例: `id`列のみに限定することを推奨）** | `create_single_job()`の`FOR UPDATE OF c SKIP LOCKED`は行ロック取得のためUPDATE権限を要求する（Postgres仕様）。クリニック本体データへの実際のUPDATEは行わない。**要検証**: 列限定`GRANT UPDATE (id) ON public.clinics`で行ロックに十分かは、実環境のPostgresバージョンで確認が必要（本文書では未検証のため断定しない）。 |
| `research.research_jobs` | `SELECT`, `INSERT`, `UPDATE` | Job作成・一時停止・状態取得 |
| `research.research_job_items` | `SELECT`, `INSERT` | Job item作成・件数集計（pause/resumeはitem単位の書込を行わない） |
| `app_config.settings` | `SELECT`, `INSERT`, `UPDATE` | Auto Run状態・dispatch_state・usage_budgetの読み書き（既存キーとの衝突を避けるため、本ロールが書くキーは`hp_auto_run_state`・`hp_cloud_dispatch:*`・`hp_cloud_usage:*`に限定されるが、テーブル単位の権限しかPostgresでは表現できないため、テーブル全体へのアクセスは避けられない） |
| それ以外すべて（`provenance.*`, `hp_research.*`, `treatment.*`, `outreach.*`, `research.research_results`等） | **権限なし** | Job制御はこれらに一切アクセスしない |

### 明示的に許可しないもの
- `public.clinics`への`DELETE`、`id`以外の列への`UPDATE`
- `research.research_results`、`hp_research.clinic_hp_research`等、実際の調査結果書込（これは`clinic_hp_worker`ロールの責務であり、Job制御ロールには不要）
- Comdesk・Email・Gmail・GAS関連スキーマへの一切のアクセス

## 参考GRANT案（未実行・レビュー用）

```sql
-- NOT EXECUTED. Human review + manual execution only.
-- CREATE ROLE clinic_hp_job_control LOGIN PASSWORD '...';

-- GRANT USAGE ON SCHEMA public, research, app_config TO clinic_hp_job_control;

-- GRANT SELECT ON public.clinics TO clinic_hp_job_control;
-- GRANT UPDATE (id) ON public.clinics TO clinic_hp_job_control;  -- 要検証: 行ロックに十分か

-- GRANT SELECT, INSERT, UPDATE ON research.research_jobs TO clinic_hp_job_control;
-- GRANT SELECT, INSERT ON research.research_job_items TO clinic_hp_job_control;

-- GRANT SELECT, INSERT, UPDATE ON app_config.settings TO clinic_hp_job_control;
```

## 既知の未解決事項

1. `public.clinics`への列限定UPDATE権限（`id`列のみ）がPostgresの`SELECT ... FOR UPDATE`
   行ロック取得に実際に十分かどうかは、Staging環境で実地検証が必要（本文書は断定しない）。
   不十分な場合は`public.clinics`全体へのUPDATE権限が必要になり、その場合のリスク
   （誤って広い書込権限を持つ）を別途評価すること。
2. `app_config.settings`はAuto Run状態・dispatch_state・usage_budgetが同じテーブルを
   共有しているため、テーブル単位の権限しかGRANTできないPostgresの制約上、Job制御ロールは
   このテーブルの全キーに技術的にはアクセス可能になる（アプリケーションコード側でキー
   プレフィックスにより論理的に分離しているのみで、DB権限による強制ではない）。
3. 実際のロール作成・接続文字列発行・`SUPABASE_JOB_CONTROL_DB_URL`への設定は、
   本PRの範囲外の人間作業として残っている。
