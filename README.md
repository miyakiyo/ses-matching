# ses-matching
SESのマッチングに利用

## 実行方法

```powershell
python main.py [オプション]
```

## CLIパラメーター

互いに排他的なオプションです（同時に1つのみ指定可）。

| オプション | 実行される処理 |
|---|---|
| `-m` | メール取得のみ |
| `-lt` | LM後処理のみ（人材） |
| `-lp` | LM後処理のみ（案件） |
| `-n` | マッチング処理のみ |
| `-d` | 古いレコード削除のみ |
| `-c` | CSV出力のみ |
| `-a` | 全処理を実行 |
| （省略） | `config.yaml` の `processes` 設定に従って実行 |

## config.yaml の processes 設定

CLIオプション未指定時に、処理ごとの実行可否を設定できます。

```yaml
processes:
  mail_fetch: true           # メール取得
  lm_postprocess_talent: true # LLM処理（人材）
  lm_postprocess_project: true  # LLM処理（案件）
  matching: true             # マッチング
  delete_old: true           # 古いレコード削除
  csv_export: true           # CSV出力

delete_old:
  days: 7                    # この日数より古いレコードを削除

mailbox:
  export_matches_joined_csv: true   # matches_joined.csv の出力可否
  csv_copy_destinations: []         # CSV出力後のコピー先ディレクトリ（複数指定可）
  db_copy_destinations: []          # CSV出力後のDBコピー先ディレクトリ（複数指定可）
```

CSVコピー設定の補足:
- `mailbox.csv_copy_destinations` に指定した複数ディレクトリへ、CSV出力後に同名ファイルを上書きコピーします。
- `mailbox.db_copy_destinations` に指定した複数ディレクトリへ、CSV出力後にDBファイル（`mailbox.db_path`）を同名でコピーします。
- コピー先ディレクトリが存在しない場合は自動作成せず、警告ログを出してそのコピー先をスキップします。
- 一部ファイルのコピーが失敗しても、他ファイル・他コピー先の処理は継続します。

## マッチング設定

```yaml
matching:
  multiprocess_enabled: false  # true の場合、案件評価ループをマルチプロセスで並列実行
  num_workers: 4               # 並列ワーカー数。1 の場合は逐次実行
  chunk_size: 50               # 1タスクあたりの人材件数
  talent_log_interval: 100     # talent_id 完了ログの出力間隔（処理件数ベース）
```

マッチングの並列化を有効にした場合も、`matches` テーブルへの更新は親プロセスが最後に一括で実行します。
子プロセスは SQLite を読み取り専用で使い、スコア計算結果だけを親プロセスへ返します。

優先順位: `CLIオプション > config.yaml(processes) > デフォルト値(true)`

## status 定義

メールテーブル（mails_talent / mails_project / mails_unclassified）の `status` は以下の意味です。

| status | 意味 |
|---|---|
| `0` | 未処理（LM処理対象） |
| `1` | LM処理済み |
| `2` | mails_talent 保存時に `ses.status2_keywords` に一致したレコード |
| `3` | mails_talent / mails_project 保存時に、同一テーブル内の `status=1` レコードと重複条件（folder+subject または sender_addr+subject）に一致し、`ses.status3_window_hours` 以内だったレコード |
| `4` | mails_talent / mails_project の `status=1` レコードのうち、`received_at` から `ses.matching_expire_hours` を超過し、マッチング対象外になったレコード |
| `5` | mails_project の LM処理後に、`subject` または `body` が `ses.status5_keywords` に一致し、マッチング対象外になったレコード |

補足:
- LM処理は `status=0` のみを対象にします。
- mails_project の LM処理後、`subject` または `body` が `ses.status5_keywords` に部分一致した場合は `status=5` で保存します。
- マッチング処理は `status=1` のみを対象にし、実行前に `received_at` が `ses.matching_expire_hours`（既定120時間）を超過したものを `status=4` に更新して対象外にします。
- 実行結果の件数ログ（`logs/result_counts.log`）には、主に以下が出力されます。
  - メール取得成功時: `status2` と `status3` の新規保存件数
  - マッチング成功時: `matching_total`（スキャン総件数）と `matching_added`（保存件数: 新規+更新）
  - LM処理・削除処理: 各処理の件数とステータス

## status4 設定

```yaml
ses:
  matching_expire_hours: 120  # status=1 を status=4 にするまでの経過時間（時間）
```

## status5 設定

```yaml
ses:
  status5_keywords: [貴社まで, 貴社所属まで, 派遣契約]  # mails_project の LM処理後、subject/body 部分一致で status=5
```
