# 必要なライブラリをインストール
# pip install tiktoken

import tiktoken

# GPT-4 / GPT-3.5 系モデルと同じエンコーディング
enc = tiktoken.get_encoding("cl100k_base")

# 解析したいテキストをここに貼り付ける
text = """
あなたはSES営業メールから「案件情報」または「人材情報」または「その他情報」を抽出するアシスタントです。
必ずJSONスキーマに従って出力してください。

「案件情報」であれば以下JSONスキーマに従う
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "SES案件情報",
  "type": "array",
  "items": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "分類": {
        "type": "string",
        "enum": ["案件"]
      },
      "案件名": {
        "type": "string",
        "minLength": 1,
        "description": "案件のタイトル"
      },
      "概要": {
        "type": "string",
        "description": "案件の概要"
      },
      "業種": {
        "type": "string",
        "description": "例: 金融, 製造, 流通 など"
      },
      "担当": {
        "type": "string",
        "description": "メンバー, テスト, リーダー, PM, PMO など"
      },
      "工程": {
        "type": "string",
        "description": "例: 要件定義/基本設計/詳細設計/開発/テスト/運用保守"
      },
      "業務内容": {
        "type": "string"
      },
      "作業場所": {
        "type": "string",
        "description": "常駐先や最寄駅など"
      },
      "リモート頻度": {
        "type": "string",
        "description": "例: フルリモート/週2出社/基本出社 など"
      },
      "必須スキル": {
        "type": "array",
        "items": {
          "type": "string"
        },
        "description": "例: 上流工程, 設計書作成, Java, Python, TypeScript など"
      },
      "尚可スキル": {
        "type": "array",
        "items": {
          "type": "string"
        },
        "description": "例: 上流工程, 設計書作成, Java, Python, TypeScript など"
      },
      "開発言語": {
        "type": "array",
        "items": {
          "type": "string"
        },
        "description": "例: Java, Python, TypeScript, COBOL など"
      },
      "データベース": {
        "type": "array",
        "items": {
          "type": "string"
        },
        "description": "例: MySQL, PostgreSQL, MongoDB など"
      },
      "OS/クラウド": {
        "type": "array",
        "items": {
          "type": "string"
        },
        "description": "例: AWS, Azure, GCP など"
      },
      "求める人物像": {
        "type": "string"
      },
      "時期": {
        "type": "string",
        "description": "開始目安。例: 即日, 10月 など"
      },
      "契約期間": {
        "type": "string",
        "description": "例: 3ヶ月更新/長期 など"
      },
      "単価": {
        "oneOf": [
          {
            "type": "number",
            "minimum": 0,
            "description": "例: 90 (単位=万円)"
          },
          {
            "type": "string",
            "pattern": "^[0-9]+(-[0-9]+)?万?円?$",
            "description": "例: 80-100, 90万, 90万円"
          }
        ],
        "description": "単価（数値 or 範囲表記を許容）"
      },
      "稼働率": {
        "oneOf": [
          {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "百分率（例: 80）"
          },
          {
            "type": "string",
            "pattern": "^[0-9]{1,3}(-[0-9]{1,3})?%?$",
            "description": "表記例: 80-100% / 100%"
          }
        ],
        "description": "整数% または『80-100%』などの文字列を許容"
      },
      "精算": {
        "type": "string",
        "description": "例: 140-180h/上下割/中割 など"
      },
      "面談": {
        "type": "string",
        "description": "想定回数"
      },
      "募集人数": {
        "type": "string",
        "description": "例: 1名/2名/複数名 など"
      },
      "年齢": {
        "type": "string",
        "description": "例: 〜45歳/制限なし など（数値に限定せず表現を許容）"
      },
      "外国籍可否": {
        "type": "boolean",
        "description": "true=可 / false=不可"
      },
      "個人事業主可否": {
        "type": "boolean",
        "description": "true=可 / false=不可"
      },
      "契約形態": {
        "type": "string",
        "enum": [
          "準委任",
          "派遣",
          "請負",
          "その他"
        ],
        "description": "必要なら選択肢を増減してください"
      },
      "商流制限_貴社所属迄": {
        "type": "boolean"
      },
      "商流制限_営業支援費可": {
        "type": "boolean"
      },
      "商流制限_浅い方優先": {
        "type": "boolean"
      },
      "服装": {
        "type": "string",
        "description": "例: 自由/オフィスカジュアル/スーツ"
      },
      "備考": {
        "type": "string"
      }
    }
  }
}

「人材情報」であれば以下JSONスキーマに従う
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "SES人材情報",
  "type": "array",
  "items": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "分類": {
        "type": "string",
        "enum": ["人材"]
      },
      "名前": {
        "type": "string",
        "minLength": 1,
        "description": "イニシャルまたは匿名化した氏名"
      },
      "性別": {
        "type": "string",
        "description": "例: 男性/女性/その他"
      },
      "年齢": {
        "oneOf": [
          {
            "type": "integer",
            "minimum": 0,
            "maximum": 120
          },
            {
              "type": "string",
              "description": "数値以外の表現も許容（例: 20代後半）"
            }
        ],
        "description": "整数または文字列表記"
      },
      "所属": {
        "type": "string",
        "description": "弊社所属 / １社下所属 など"
      },
      "外国籍": {
        "type": "boolean",
        "description": "true=外国籍 / false=日本国籍"
      },
      "個人事業主": {
        "type": "boolean",
        "description": "true=個人事業主 / false=法人所属"
      },
      "最寄駅": {
        "type": "string",
        "description": "主要路線や駅名"
      },
      "希望単価": {
        "oneOf": [
          {
            "type": "number",
            "minimum": 0,
            "description": "例: 90 (単位=万円)"
          },
          {
            "type": "string",
            "pattern": "^[0-9]+(-[0-9]+)?万?円?$",
            "description": "例: 80-100, 90万, 90万円"
          }
        ],
        "description": "希望単価（数値または範囲）"
      },
      "稼働可能日": {
        "type": "string",
        "description": "稼働開始可能日（例: 即日/翌月/2025-10-01）"
      },
      "稼働率": {
        "oneOf": [
          {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "百分率（例: 80）"
          },
          {
            "type": "string",
            "pattern": "^[0-9]{1,3}(-[0-9]{1,3})?%?$",
            "description": "範囲または表記例: 50-80% / 100%"
          }
        ],
        "description": "稼働率"
      },
      "リモート頻度": {
        "type": "string",
        "description": "例: フルリモート/週2出社 など"
      },
      "担当": {
        "type": "string",
        "description": "想定ロール（例: メンバー/リーダー/SE/PG）"
      },
      "工程": {
        "type": "string",
        "description": "例: 要件定義/基本設計/詳細設計/開発/テスト/運用保守"
      },
      "開発言語": {
        "type": "array",
        "items": { "type": "string" },
        "description": "例: Java, Python, TypeScript"
      },
      "OS/クラウド": {
        "type": "array",
        "items": {
          "type": "string"
        },
        "description": "例: AWS, Azure, GCP など"
      },
      "データベース": {
        "type": "array",
        "items": {
          "type": "string"
        },
        "description": "例: MySQL, PostgreSQL, MongoDB など"
      },
      "スキル": {
        "type": "array",
        "items": { "type": "string" },
        "description": "その他技術/業務スキル"
      },
      "資格": {
        "type": "array",
        "items": { "type": "string" },
        "description": "保有資格（例: 基本情報技術者, Oracle, AWS など）"
      },
      "備考": {
        "type": "string"
      }
    }
  }
}


「その他」であれば以下JSONスキーマに従う
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "その他営業情報",
  "type": "array",
  "items": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "分類": {
        "type": "string",
        "enum": ["その他"]
      },
      "件名": {
        "type": "string",
        "minLength": 1,
        "description": "情報のタイトル"
      },
      "概要": {
        "type": "string",
        "description": "情報の概要"
      }
    }
  }
}


ルール:
- JSON以外の文章は出力しない
- 未記載の項目は必ず null または [] にする
- 日本語で、構造は必ずJSON形式に従う

[
{
"名前": "MT",
"性別": "男性",
"年齢": 28,
"所属": "弊社プロパー",
"最寄駅": "京成松戸線 上本郷駅",
"希望単価": 550000,
"稼働可能日日": "10月～",
"稼働率": null,
"リモート頻度": null,
"担当": "テスト計画補助/テスト設計（シナリオ・ケース）/テスト実施（機能・性能・API）/不具合起票・報告/エビデンス整備/PMO（進捗管理・会議体運営・議事録・日程調整）/多端末検証（スマホ・タブレット・PC）",
"工程": null,
"開発言語": ["Android Studio", "GitHub"],
"OS/クラウド": ["Windows 10/11", "QASE", "JIRA"],
"データベース": [],
"スキル": [
"デバイス／OS：Android, iPad OS, Windows 10/11（一部OpenSTFでの仮想端末利用）",
"テスト関連：QASE, JIRA／エビデンス・報告書作成",
"その他：Android Studio, GitHub"
],
"資格": [],
"備考": [
"マルチデバイス検証（Android／iPad OS／Windows 10/11）",
"APIテスト、AI機能を用いたテストケース作成の実務あり",
"英語ドキュメントの読み書き・翻訳対応あり",
"PMO（会議運営・議事録・進捗管理・調整／チーム規模：7名）"
]
},
{
"名前": "YM",
"性別": null,
"年齢": 32,
"所属": "弊社社員",
"最寄駅": "小田急線 相模大野駅",
"希望単価": 550000,
"稼働可能日日": "2025年9月～",
"稼働率": null,
"リモート頻度": "[通勤40分以内] リモート併用",
"担当": "テスト計画補助/テスト設計（ケース作成）/テスト実施（機能・性能）/不具合起票・報告/エビデンス作成/フロント改修（HTML/CSS/JavaScript／Vue.js）/既存APIのGraphQL移行補助/運用保守補助",
"工程": null,
"開発言語": ["Java", "Vue.js"],
"OS/クラウド": ["Windows", "Android", "iOS"],
"データベース": [],
"スキル": [
"HTML／CSS／JavaScript：1年半（企業HP制作・更新／軽微な改修）",
"Vue.js：実務約5か月（2025/04～08、教育系アプリのフロント改修）",
"Java：2年半（コード読解に基づくテストケース設計・検証）",
"GraphQL：実務約5か月（既存API→GraphQL移行改修、読み込み速度改善の一環）",
"Excel：ケース管理・集計、報告資料作成"
],
"資格": [],
"備考": [
"公共／保険／教育（研修アプリ）",
"教育：大手人材広告企業 研修アプリ（2025/04～2025/08）",
"- 役割：テスター／フロント改修補助",
"- 業務：既存プログラムのGraphQL移行改修に参画、テストケースに基づく検証実施／Vue.jsを用いたUI改修支援",
"官公庁向け入札管理システム",
"- 役割：テスター",
"- 業務：仕様変更に伴うテストケース作成・実施、エビデンス取得、インシデント報告",
"保険会社向け業務アプリ検証",
"- 役割：テスター",
"- 業務：テスト設計～実施、報告書取りまとめ"
]
}
]
"""

# トークン化
tokens = enc.encode(text)

print("トークン数:", len(tokens))
