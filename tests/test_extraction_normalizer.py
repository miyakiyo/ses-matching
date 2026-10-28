import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.extraction_normalizer import (  # noqa: E402
    normalize_project_price_and_age,
    normalize_talent_price_and_age,
    parse_age_text,
    parse_price_text,
)


class PriceTextTest(unittest.TestCase):
    def test_range(self):
        self.assertEqual(parse_price_text("60~65万"), (60, 65))
        self.assertEqual(parse_price_text("60〜65万円"), (60, 65))
        self.assertEqual(parse_price_text("50から60万円想定"), (50, 60))
        self.assertEqual(parse_price_text("650,000~700,000円"), (65, 70))

    def test_single(self):
        self.assertEqual(parse_price_text("75万円"), (75, 75))
        self.assertEqual(parse_price_text("40万円前後"), (40, 40))
        self.assertEqual(parse_price_text("650,000円"), (65, 65))
        self.assertEqual(parse_price_text(70), (70, 70))

    def test_upper_only(self):
        self.assertEqual(parse_price_text("~65万程度"), (None, 65))
        self.assertEqual(parse_price_text("MAX58万"), (None, 58))
        self.assertEqual(parse_price_text("65万まで"), (None, 65))
        self.assertEqual(parse_price_text("65万程度まで"), (None, 65))
        self.assertEqual(parse_price_text("~68万(スキル見合い)"), (None, 68))

    def test_lower_only(self):
        self.assertEqual(parse_price_text("60万~"), (60, None))
        self.assertEqual(parse_price_text("60万以上"), (60, None))

    def test_settlement_hours_are_ignored(self):
        # 精算幅（140-180h）を単価と取り違えない
        self.assertEqual(parse_price_text("63万(140h)"), (63, 63))
        self.assertEqual(parse_price_text("60~65万(140-180h)"), (60, 65))
        self.assertEqual(parse_price_text("140-180h"), (None, None))

    def test_utilization_percent_is_ignored(self):
        self.assertEqual(parse_price_text("(100%稼働時):110万円"), (110, 110))

    def test_unknown(self):
        self.assertEqual(parse_price_text(None), (None, None))
        self.assertEqual(parse_price_text(""), (None, None))
        self.assertEqual(parse_price_text("スキル見合い"), (None, None))
        # 単位を取り違えたような値（7500万）は捨てる
        self.assertEqual(parse_price_text("7500000万"), (None, None))


class AgeTextTest(unittest.TestCase):
    def test_exact(self):
        self.assertEqual(parse_age_text("32歳"), (32, 32))
        self.assertEqual(parse_age_text("49歳男性"), (49, 49))
        self.assertEqual(parse_age_text("32"), (32, 32))
        self.assertEqual(parse_age_text(":24"), (24, 24))
        self.assertEqual(parse_age_text("25際"), (25, 25))

    def test_decade(self):
        self.assertEqual(parse_age_text("30代"), (30, 39))
        self.assertEqual(parse_age_text("30代前半"), (30, 34))
        self.assertEqual(parse_age_text("30代後半"), (35, 39))

    def test_upper(self):
        self.assertEqual(parse_age_text("40代まで"), (None, 49))
        self.assertEqual(parse_age_text("~50代"), (None, 59))
        self.assertEqual(parse_age_text("45歳以下"), (None, 45))
        self.assertEqual(parse_age_text("45歳未満"), (None, 44))
        self.assertEqual(parse_age_text("40代前半まで"), (None, 44))
        self.assertEqual(parse_age_text("60代可"), (None, 69))
        self.assertEqual(parse_age_text("55歳ぐらいまで"), (None, 55))
        self.assertEqual(parse_age_text("50代前半くらいまで"), (None, 54))

    def test_lower(self):
        self.assertEqual(parse_age_text("30歳以上"), (30, None))

    def test_range(self):
        self.assertEqual(parse_age_text("20~40代"), (20, 49))
        self.assertEqual(parse_age_text("35~45歳"), (35, 45))
        self.assertEqual(parse_age_text("20代〜30代前半"), (20, 34))

    def test_unknown(self):
        self.assertEqual(parse_age_text(None), (None, None))
        self.assertEqual(parse_age_text("不問"), (None, None))
        # 生年は年齢として扱わない
        self.assertEqual(parse_age_text("1993年生まれ"), (None, None))


class NormalizeRecordTest(unittest.TestCase):
    def test_project(self):
        result = normalize_project_price_and_age({"単価": "~65万(スキル見合い)", "年齢": "40代まで"})
        self.assertEqual(result["単価"], "~65万(スキル見合い)")
        self.assertIsNone(result["単価下限"])
        self.assertEqual(result["単価上限"], "65")
        self.assertIsNone(result["年齢下限"])
        self.assertEqual(result["年齢上限"], "49")

    def test_project_without_values(self):
        result = normalize_project_price_and_age({"単価": None, "年齢": None})
        for key in ("単価下限", "単価上限", "年齢下限", "年齢上限"):
            self.assertIsNone(result[key], key)

    def test_talent(self):
        result = normalize_talent_price_and_age({"希望単価": "60~65万円", "年齢": "30代後半"})
        self.assertEqual(result["希望単価"], "60-65")
        self.assertEqual(result["年齢"], "35-39")

    def test_talent_single_and_unknown(self):
        result = normalize_talent_price_and_age({"希望単価": "750,000円", "年齢": "不明"})
        self.assertEqual(result["希望単価"], "75")
        self.assertIsNone(result["年齢"])

    def test_original_dict_is_not_changed(self):
        original = {"希望単価": "70万"}
        normalize_talent_price_and_age(original)
        self.assertEqual(original, {"希望単価": "70万"})


if __name__ == "__main__":
    unittest.main()
