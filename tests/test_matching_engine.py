import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.matching_engine import (  # noqa: E402
    _normalize_price_man,
    _normalize_remote,
    _normalize_skill,
    _parse_project_price_range,
    _parse_talent_price_range,
    calculate_match_score,
)


def talent(**kw):
    base = {"開発言語": '["Java"]', "希望単価": "70"}
    base.update(kw)
    return base


def project(**kw):
    base = {"開発言語": '["Java"]', "単価下限": 60, "単価上限": 80}
    base.update(kw)
    return base


def excluded_rule(score, reason):
    return reason.get("excluded", {}).get("rule") if score == 0 else None


class RequiredTechTest(unittest.TestCase):
    def test_java_project_csharp_talent_excluded(self):
        score, reason = calculate_match_score(talent(開発言語='["C#"]'), project())
        self.assertEqual(score, 0)
        self.assertEqual(excluded_rule(score, reason), "required_tech_mismatch")

    def test_java_project_java_talent_passes(self):
        score, reason = calculate_match_score(talent(), project())
        self.assertGreater(score, 0)
        self.assertNotIn("excluded", reason)

    def test_java_and_javascript_are_different(self):
        score, _ = calculate_match_score(talent(開発言語='["JavaScript"]'), project())
        self.assertEqual(score, 0)

    def test_alias_csharp(self):
        self.assertEqual(_normalize_skill("C Sharp"), _normalize_skill("c#"))

    def test_skill_field_counts_as_talent_skill(self):
        score, _ = calculate_match_score(
            talent(開発言語=None, スキル='["Java", "Spring"]'), project()
        )
        self.assertGreater(score, 0)

    def test_talent_without_skill_info_passes(self):
        score, _ = calculate_match_score({"希望単価": "70"}, project())
        self.assertGreater(score, 0)

    def test_project_without_language_not_gated_by_db(self):
        score, _ = calculate_match_score(
            talent(), project(開発言語=None, データベース='["Oracle"]')
        )
        self.assertGreater(score, 0)


class PriceTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(_normalize_price_man("72万円"), 72)
        self.assertEqual(_normalize_price_man("45万前後"), 45)
        self.assertEqual(_normalize_price_man(950000), 95)
        self.assertEqual(_normalize_price_man(60), 60)
        self.assertEqual(_normalize_price_man("100万円"), 100)

    def test_anomaly_is_unknown(self):
        self.assertIsNone(_normalize_price_man(5))
        self.assertIsNone(_normalize_price_man(18))
        self.assertIsNone(_normalize_price_man(9500000))
        self.assertIsNone(_normalize_price_man("abc"))
        self.assertIsNone(_normalize_price_man(None))

    def test_project_range_swapped(self):
        self.assertEqual(
            _parse_project_price_range({"単価下限": 90, "単価上限": 70}), (70, 90)
        )

    def test_talent_range_string(self):
        self.assertEqual(_parse_talent_price_range("80-100万円"), (80, 100))
        self.assertEqual(_parse_talent_price_range("630000"), (63, 63))

    def test_inversion_excluded(self):
        score, reason = calculate_match_score(talent(希望単価="90"), project())
        self.assertEqual(excluded_rule(score, reason), "price_inversion")

    def test_unknown_price_passes(self):
        score, _ = calculate_match_score(talent(希望単価=None), project())
        self.assertGreater(score, 0)

    def test_project_pays_more_than_hope_not_excluded(self):
        score, _ = calculate_match_score(
            talent(希望単価="50"), project(単価下限=70, 単価上限=80)
        )
        self.assertGreater(score, 0)


class AgeTest(unittest.TestCase):
    def test_out_of_range_excluded(self):
        score, reason = calculate_match_score(
            talent(年齢="52"), project(年齢下限=20, 年齢上限=45)
        )
        self.assertEqual(excluded_rule(score, reason), "age_out_of_range")

    def test_too_young_not_excluded(self):
        score, _ = calculate_match_score(
            talent(年齢="22"), project(年齢下限=40, 年齢上限=None)
        )
        self.assertGreater(score, 0)

    def test_in_range_passes(self):
        score, _ = calculate_match_score(
            talent(年齢="30"), project(年齢下限=20, 年齢上限=45)
        )
        self.assertGreater(score, 0)

    def test_unknown_age_passes(self):
        score, _ = calculate_match_score(talent(), project(年齢下限=20, 年齢上限=45))
        self.assertGreater(score, 0)


class RemoteTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(_normalize_remote("フルリモート"), "full_remote")
        self.assertEqual(_normalize_remote("週2出社"), "hybrid")
        self.assertEqual(_normalize_remote("基本出社"), "onsite")
        self.assertEqual(_normalize_remote("フルリモート(出社も可)"), "hybrid")
        self.assertEqual(_normalize_remote("基本出社(リモート相談可)"), "hybrid")

    def test_unknown_values(self):
        for value in (None, "", "[]", "不明", "未記載", "例: フルリモート/週2出社 など"):
            self.assertEqual(_normalize_remote(value), "unknown", value)

    def test_full_remote_talent_onsite_project_excluded(self):
        score, reason = calculate_match_score(
            talent(リモート頻度="フルリモート"), project(リモート頻度="基本出社")
        )
        self.assertEqual(excluded_rule(score, reason), "remote_mismatch")

    def test_unknown_remote_passes(self):
        score, _ = calculate_match_score(
            talent(リモート頻度=None), project(リモート頻度="基本出社")
        )
        self.assertGreater(score, 0)

    def test_onsite_ok_talent_passes(self):
        score, _ = calculate_match_score(
            talent(リモート頻度="出社可"), project(リモート頻度="基本出社")
        )
        self.assertGreater(score, 0)


if __name__ == "__main__":
    unittest.main()
