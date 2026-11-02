# マイナビ記事URL・医院詳細URL・PR表記分類 アーキテクチャ調査

調査対象コミット: `8e9d2c3`。実行・DB書き込みは一切行わず、静的なコード読解のみで作成。

## 結論（先に要点）

**このリポジトリ内に「マイナビクリニックナビをスクレイピングして記事URL・医院詳細URLを自動取得するコード」は存在しない。**
URL取得・医院との紐づけ・PR/監修判定は、いずれも**リポジトリ外の「upstream article-join workflow（上流の記事JOINワークフロー）」が人手または別システムで用意した2種類のCSVファイル**を読み込むだけの、読み取り専用アダプタとして実装されている。これは`src/outreach/article_adapter.py:1-5`のモジュールdocstringに明記された設計方針であり、曖昧な推測・URL文字列からの自動推定は一切行わない（本人確認できない場合は必ず人手レビューに回す）。

唯一の例外として、一般のHP自動調査パイプラインが副次的に`mynavi.jp`ドメインへの言及を検出する機能があるが、これは独立した汎用シグナルであり、送付対象・PR判定ロジックには接続されていない（4節参照）。

## 1. 記事URLの取得元

- **ファイル**: `src/outreach/article_recommender.py`
- **関数**: `read_master(path)`（30〜44行目）
- 「マイナビ記事原本CSV（No./タイトル/URL/KW）」という、**Crestix社が手作業で管理する既存の記事マスターCSV**を読み込む（docstring: "Crestix's existing Mynavi article master"、1〜4行目）
- URLは`https://clinic.mynavi.jp/article/...`の形式であることをバリデーションするのみ（36行目: `parsed.scheme != "https" or parsed.hostname != "clinic.mynavi.jp" or not parsed.path.startswith("/article/")`）。**URLそのものを発見・生成する処理は無い**＝人間が既に知っているURLを列挙したCSVを読むだけ。
- このCSVの入力経路は`src/outreach_ui.py:48-57` `_upload_or_env()`：Streamlitの`st.file_uploader`で都度アップロードするか、環境変数`OUTREACH_ARTICLE_MASTER_CSV`（`outreach_ui.py:38`）で指定したローカルファイルパスを読む。

## 2. 医院詳細ページURLの取得元

- **ファイル**: `src/outreach/article_adapter.py`
- `ArticleLink`データクラス（19〜34行目）の`clinic_profile_url`フィールド（34行目）
- コメント（31〜32行目）: 「These are optional adapter fields until the upstream article system publishes them. Missing values remain UNKNOWN/blank; they are never inferred from URL text.（上流の記事システムがこの値を提供するまでの暫定フィールド。欠損時は空のまま保持し、URL文字列から推測することは絶対にしない）」
- 取得元は記事URLと同様、**「記事JOIN CSV」という別の外部提供ファイル**（環境変数`OUTREACH_ARTICLE_JOIN_CSV`、`outreach_ui.py:39`）の`clinic_profile_url`列。`load_article_links_csv()`（`article_adapter.py:83-117`、特に115行目）および`src/outreach_ui.py:_load_links()`（86行目）がこの列をそのまま転記するだけで、**生成・検証・推測は一切行わない**。

## 3. 記事と医院を紐づける方法

- **ファイル**: `src/outreach/article_adapter.py`
- **関数**: `lookup_article(clinic_uuid, records)`（59〜80行目）
- 紐づけは**Clinic UUIDの完全一致のみ**（61行目のdocstring: "Exact UUID-only lookup. Never fuzzy match, silently select, or fabricate URLs."）
- 判定フロー（`lookup_article`内）：
  1. UUID空 → `NEEDS_REVIEW`（63行目）
  2. 一致レコードなし → `NO_MAPPING`（65-66行目）
  3. 一致レコードが複数 → `NEEDS_REVIEW`（69-70行目、「同一Clinic UUIDの記事候補が複数存在」）
  4. `verified`列がFalse、または`source_row`/`article_id`が空 → `NEEDS_REVIEW`（72-73行目）
  5. `example_article_url`が`clinic.mynavi.jp/article/...`形式でない → `NEEDS_REVIEW`（74-75行目）
  6. `listing_status != "LISTED"`または`listing_kind != "VERIFIED_ARTICLE"` → `NEEDS_REVIEW`（76-77行目、「記事掲載未確認: 固定文面の断定を防止」）
  7. `listing_url`が正規URL形式でない → `NEEDS_REVIEW`（78-79行目）
  8. 全条件を満たして初めて`READY`（80行目）
- 上位の`src/outreach/targets.py` `classify_article(clinic_uuid, links)`（67〜84行目）が`lookup_article`の結果を`ArticleState`（LISTED/NOT_LISTED/UNCONFIRMED）に正規化し、`ClinicTarget.article`（`targets.py:55`）としてメール送付文章画面の対象医院情報に組み込まれる。

## 4. PR表記判定の仕組み

- **データソース**: `article_type`列（NORMAL / PR / SUPERVISED / UNKNOWN）。これも**記事JOIN CSVから読み込むだけ**で、記事本文を解析してPRかどうかを自動判定するコードは存在しない。
  - 読込: `src/outreach/article_adapter.py:114` `article_type=(row.get("article_type") or "UNKNOWN").strip().upper()`
  - バリデーション: `src/outreach/legacy_import_preview.py:71-75` — `article_type`が`{"NORMAL","PR","SUPERVISED","UNKNOWN"}`以外なら`LegacyImportInputError`を送出
- **除外ロジック（実際の「判定」が行われる場所）**: `src/outreach_ui.py:334-337`
  ```python
  if target.article.status == ARTICLE_LISTED and target.article.article_type in {"PR", "SUPERVISED"}:
      ...
      exclusion_reasons[int(clinic_id)] = "PR/監修記事のため除外"
  ```
  記事が掲載確認済み（LISTED）かつ`article_type`がPRまたは監修（SUPERVISED）の場合、その医院を営業送付対象から除外する。除外件数は`src/outreach_ui.py:204`のKPI表示「PR・監修除外」に集計される。
- 同様の除外は`src/outreach/auto_draft_batch.py:82` `if t.article.status == ARTICLE_LISTED and t.article.article_type != "NORMAL":`、`src/outreach/gmail_workflow.py:359`でも行われている（下書き自動生成・Gmail送信ワークフローでも同じ除外ルールを適用）。

## 5. CSV・DB保存先

**データベースへの保存は一切行わない。** 全て2種類のCSVファイルの読み込みのみで完結する、読み取り専用のサブシステム。

| ファイル種別 | 列構成 | 入力元 |
|---|---|---|
| 記事原本CSV | No. / タイトル / URL / KW | `OUTREACH_ARTICLE_MASTER_CSV`環境変数 または画面アップロード |
| 記事JOIN CSV | clinic_uuid, listing_status, example_article_url, listing_url, listing_kind, source_row, article_id, verified, evidence, checked_at, processing_version, article_type, clinic_profile_url | `OUTREACH_ARTICLE_JOIN_CSV`環境変数 または画面アップロード |

`src/outreach_ui.py:211`のUI説明文「アップロードがない場合は環境変数...のCSVを読み取ります。**原本は変更しません**。」の通り、書き込み経路は実装されていない。

## 6. 外部ワークフローへの依存

- 記事原本CSVの作成（マイナビへの掲載依頼・記事タイトル/URL/キーワードの管理）は、Crestix社の既存の社内運用（リポジトリ外）に依存。
- 記事と医院の紐づけ（clinic_uuidとの対応付け、掲載有無の確認、PR/監修かどうかの判定、医院詳細ページURLの特定）は、**「upstream article-join workflow」という外部の確認済みJOIN処理**（具体的な実装はこのリポジトリに含まれない）に完全依存。このリポジトリのコードは「既に検証済みの行だけを信用する」ゲートキーパーとしてのみ機能する。

## 7. 自動取得が実装されている範囲（まとめ）

| 項目 | 自動化の有無 |
|---|---|
| マイナビ記事URLの発見 | **なし**（手作業で維持される原本CSVを読むのみ） |
| 医院詳細ページURLの発見 | **なし**（外部JOIN CSVの値をそのまま転記） |
| 記事と医院の紐づけ | **部分的**：UUID完全一致の照合ロジック自体は自動（`lookup_article`）だが、照合に使う「verified=true」なデータそのものは外部ワークフローが用意したものに依存 |
| PR/監修表記の判定 | **なし**（JOIN CSVの`article_type`列をそのまま使うのみ。記事本文解析による自動PR検出は未実装） |
| 一般HP調査でのmynavi.jp検知（副次的・別系統） | **あり**：`src/scoring/research_scoring.py:13,25,29-30`のMEDIA辞書が、HP自動調査中に`mynavi.jp`/`mynavi-ms.jp`ドメインへの言及を見つけると「マイナビ記事掲載」というスコアリングシグナルとして自動記録する。ただしこれは営業送付対象の記事紐づけ・PR判定とは**接続されていない独立した汎用メディア検出機能**であり、上記1〜6節のマイナビ記事連携機能とは別物（`scripts/perf/golden/expected.json`内の`"マイナビ記事掲載"`エントリはこの一般検出の実例）。

## 8. 関連ファイル一覧

| ファイル | 役割 |
|---|---|
| `src/outreach/article_adapter.py` | `ArticleLink`データクラス・`lookup_article`（UUID完全一致照合）・`load_article_links_csv`（JOIN CSV読込） |
| `src/outreach/article_recommender.py` | `Article`データクラス・`read_master`（記事原本CSV読込）・推薦スコアリング |
| `src/outreach/targets.py` | `classify_article`（LISTED/NOT_LISTED/UNCONFIRMED判定）・`ClinicTarget`組立 |
| `src/outreach/legacy_import_preview.py` | JOIN CSV相当データの別経路でのimport時のバリデーション（article_type許容値チェック） |
| `src/outreach_ui.py` | Streamlit UI。アップロード/環境変数からの読込、PR/監修除外ロジック（334-337行目）、KPI表示 |
| `src/outreach/auto_draft_batch.py`, `src/outreach/gmail_workflow.py` | 下書き自動生成・Gmail送信時に同じPR/監修除外ルールを適用 |
| `src/scoring/research_scoring.py` | （別系統）一般HP調査でのmynavi.jpドメイン検知シグナル |

## 9. 今回の制約事項の遵守

本調査はコード読解のみで完結し、メール調査・記事マスターの取込実行・DBへの書き込みは一切行っていない。
