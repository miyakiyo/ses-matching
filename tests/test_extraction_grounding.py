import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.extraction_grounding import (  # noqa: E402
    PROJECT_TECH_FIELDS,
    TALENT_TECH_FIELDS,
    ground_record,
)


ALIASES = {"js": "javascript", "golang": "go", "sqlserver": "sql server"}


def ground_talent(body, **fields):
    record = {"subject": "", "body": body, **fields}
    return ground_record(record, TALENT_TECH_FIELDS, ALIASES)


class TechGroundingTest(unittest.TestCase):
    def test_schema_example_values_are_removed(self):
        # 改善案 #18: インフラの人材に、フォーマットの例（Java, Python, C#）が書き込まれていた
        body = "【技術】Azure、Terraform、PowerShell、Bash、SQL Server"
        result = ground_talent(
            body,
            開発言語='["Java", "Python", "C#"]',
            データベース='["MySQL", "PostgreSQL", "Oracle", "SQL Server"]',
            スキル='["Spring Boot", "React", "Docker", "Terraform"]',
        )
        self.assertEqual(result["開発言語"], [])
        self.assertEqual(result["データベース"], ["SQL Server"])
        self.assertEqual(result["スキル"], ["Terraform"])

    def test_ascii_terms_are_matched_as_words(self):
        body = "JavaScript / C# の開発経験"
        result = ground_talent(body, 開発言語='["Java", "JavaScript", "C", "C#"]')
        self.assertEqual(result["開発言語"], ["JavaScript", "C#"])

    def test_width_and_spaces_are_ignored(self):
        body = "ＳＱＬＳｅｒｖｅｒ、Ｃ＃"
        result = ground_talent(body, データベース='["SQL Server"]', 開発言語='["C#"]')
        self.assertEqual(result["データベース"], ["SQL Server"])
        self.assertEqual(result["開発言語"], ["C#"])

    def test_alias_in_body_counts(self):
        result = ground_talent("JS と Golang の経験", 開発言語='["JavaScript", "Go"]')
        self.assertEqual(result["開発言語"], ["JavaScript", "Go"])

    def test_subject_counts_as_evidence(self):
        record = {"subject": "Python案件", "body": "詳細は面談にて", "必須スキル": '["Python"]'}
        result = ground_record(record, PROJECT_TECH_FIELDS, ALIASES)
        self.assertEqual(result["必須スキル"], ["Python"])

    def test_record_without_text_is_kept(self):
        record = {"subject": None, "body": None, "開発言語": '["Java"]'}
        self.assertIs(ground_record(record, TALENT_TECH_FIELDS, ALIASES), record)

    def test_non_array_values_are_kept(self):
        result = ground_talent("Java", 開発言語="Java")
        self.assertEqual(result["開発言語"], "Java")


class ChoiceGroundingTest(unittest.TestCase):
    def test_all_phases_are_reduced_to_written_ones(self):
        # 案件の工程の75%が6つ全部になっていた
        body = "■作業内容：詳細設計、製造、単体試験"
        result = ground_talent(body, 工程='["要件定義", "基本設計", "詳細設計", "開発", "テスト", "運用保守"]')
        self.assertEqual(result["工程"], ["基本設計", "詳細設計", "開発", "テスト"])

    def test_roles_are_matched_by_words(self):
        body = "PMOとしてSES契約で参画。PL/SQLの経験あり"
        result = ground_talent(body, 担当='["PM", "PMO", "リーダー", "SE", "PG", "テスト", "メンバー"]')
        self.assertEqual(result["担当"], ["PMO"])

    def test_role_synonyms(self):
        body = "プログラマとしてテスターも兼務、リーダー経験あり"
        result = ground_talent(body, 担当='["リーダー", "PG", "テスト", "PM"]')
        self.assertEqual(result["担当"], ["リーダー", "PG", "テスト"])


if __name__ == "__main__":
    unittest.main()
