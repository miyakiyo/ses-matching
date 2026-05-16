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
```

優先順位: `CLIオプション > config.yaml(processes) > デフォルト値(true)`

## status 定義

メールテーブル（mails_talent / mails_project / mails_unclassified）の `status` は以下の意味です。

| status | 意味 |
|---|---|
| `0` | 未処理（LM処理対象） |
| `1` | LM処理済み |
| `2` | mails_talent 保存時に `ses.status2_keywords` に一致したレコード |
| `3` | mails_talent / mails_project 保存時に、同一テーブル内の `status=1` レコードと重複条件（folder+subject または sender_addr+subject）に一致し、`ses.status3_window_hours` 以内だったレコード |

補足:
- LM処理は `status=0` のみを対象にします。
- 実行結果の件数ログ（`logs/result_counts.log`）には、メール取得成功時に `status2` と `status3` の新規保存件数も出力されます。
