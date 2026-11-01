# Comdesk最新情報同期のリリース手順

この手順は候補コードのレビュー後に、別途承認を得て実施する。本書作成時点では本番変更、Migration、CSV取込、Runtime切替を行っていない。

## 反映前ゲート

1. GitHub mainの最新HEADを再確認し、この統合候補を新しいPRとしてレビューする。PR #138/#139の変更を含むため、両PRの扱いを整理してから統合する。
2. ClinicLeadRuntimeの作業ツリーが使用者の未コミット変更を保持していることを確認する。既存退避先は`/private/tmp/ClinicLeadRuntime-preserve-20261022/`。この退避は当日の状態の保護用であり、切替直前にもtracked/untrackedを別の権限制限付き退避先へ保存する。
3. `clinic-lead-shadow`は名前にshadowを含むが、ClinicLeadRuntimeの設定と運用文書では本番対象として扱われている。接続URL等の秘密値をログ・報告へ出さず、接続先project refが`hwdvyhunozporfuefteb`であることだけを確認する。
4. 反映直前にHP JobとEmail Jobを読み取り専用で確認する。HPがRUNNINGなら切替をHOLDし、Jobを停止・再開しない。本確認時はHP JobがRUNNINGであったため、現時点の本番切替はHOLD。
5. Step7同期に必要な`clinics`、`provenance.comdesk_original_rows`、`provenance.templates`、`provenance.change_history`、`provenance.import_batches`およびComdesk UUID対応テーブルの列・権限を読み取り専用で再確認する。Step7自身のための新規Migrationは不要。

## #139 Importer Migration

PR #139の従来Importerには`provenance.source_records`上の`comdesk_project_case_unique_idx`が必須。Writerはindexの存在、unique/valid/ready状態、式を検査し、条件を満たさない場合は書込を開始せず失敗する。本番確認時はindexが存在しなかったため、ImporterはMigration承認・適用までfail-closedとなる。

Migration適用前に重複を確認する。

```sql
SELECT btrim(record_json->>'comdesk_project_id') AS project_id,
       btrim(record_json->>'comdesk_case_id') AS case_id,
       count(*) AS rows
FROM provenance.source_records
WHERE source = 'コムデスク'
  AND nullif(btrim(record_json->>'comdesk_project_id'), '') IS NOT NULL
  AND nullif(btrim(record_json->>'comdesk_case_id'), '') IS NOT NULL
GROUP BY 1, 2
HAVING count(*) > 1;
```

結果が空であることを確認し、Migrationを別途承認した後に適用する。適用後は`pg_index`でindexの`indisunique`, `indisvalid`, `indisready`と式を確認する。既存の重複があれば自動削除・統合せず、ImporterをHOLDする。index作成はDDLであり、本番適用前に書込影響と保守時間を承認する。

## Mac Runtime切替

1. HP/Email JobがいずれもRUNNINGでないことを確認し、実行主体・サービス名・プロセスを読み取り専用で照合する。RUNNINGなら切替しない。
2. ClinicLeadRuntimeのHEAD、branch、tracked diff、untracked path一覧を保存し、秘密情報を含む設定ファイルは出力しない。コード、設定、起動定義を権限制限付きの時刻付きバックアップへ保存する。Runtimeの使用者が作成した差分を候補コードで上書きしない。
3. 候補はRuntimeの隣に新しい版ディレクトリとして展開し、現行Runtimeはそのまま保持する。既存の正式起動定義を変更する前に、隔離PostgreSQLと合成30列データでStep7プレビュー・承認・保存・再出力、同一CSV再取込、Writer Migration未適用時の停止、Step6・Step7・既存Importer回帰を再確認する。
4. 承認後、利用者の保守手順に従って起動先を候補ディレクトリへ切り替える。実CSV取込・本番DB書込は別の明示承認後に実施する。切替直後は合成データまたは読み取り専用の画面確認までとし、監視者が保存先・30列出力・Job状態を確認する。

## 復旧

- 起動・画面・出力の問題があれば、直ちに起動先を事前退避した旧Runtimeへ戻す。旧版のファイル群と起動定義を上書きせず保持する。
- 本番Migration適用後のコードロールバックは可能だが、Importerはunique indexがある状態のまま安全に停止できる。通常の復旧ではindexを削除しない。
- Step7の本番反映後にデータを戻す必要があれば、`provenance.change_history`のbefore/after、同期元、file hash、対象Clinic内部IDを用いて人が対象と値を照合し、別途承認を得て復旧する。自動逆適用は用意されていない。
- Clinic UUID、Comdesk UUID、Clinic内部IDを同一視した更新が見つかった場合は通常の再取込を止め、ログと変更履歴を保全して個別調査する。

## 今回確認した状態

- 本番相当project `clinic-lead-shadow` のHP JobはRUNNING（2 running、121 pending、10 done）。Email Auto RunにRUNNINGはなく、5 COMPLETED/4 STOPPED。
- `provenance.comdesk_project_case_unique_idx`は未作成。
- ClinicLeadRuntimeには未コミット変更とuntracked成果物がある。HEADは`f106a19c873f768bbda238ee6a324631efd4dd57`で、退避ファイルを`/private/tmp/ClinicLeadRuntime-preserve-20261022/`に作成済み。
- 以上から、この候補は隔離検証済みだが、現在のMac正式Runtime切替はHOLD。
