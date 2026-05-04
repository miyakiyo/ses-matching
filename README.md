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
