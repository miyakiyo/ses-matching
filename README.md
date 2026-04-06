# ses-matching
SESのマッチングに利用

## 実行フラグ設定

`config.yaml` の `processes` で処理ごとの実行可否を設定できます。

- `mail_fetch`: メール取得
- `lm_postprocess_human`: LLM処理（人材）
- `lm_postprocess_case`: LLM処理（案件）
- `matching`: マッチング
- `delete_old`: 古いレコード削除
- `csv_export`: CSV出力

優先順位は `CLI引数 > config.yaml(processes) > デフォルト値(true)` です。

- CLI未指定時: `processes` の設定に従って実行
- CLI指定時: 指定した処理のみ実行（`-l` はLM処理のみ、`matching` は実行しない）
