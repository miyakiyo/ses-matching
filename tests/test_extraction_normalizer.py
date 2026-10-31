import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.extraction_normalizer import (  # noqa: E402
    REGION_KANTO,
    REGION_OTHER,
    classify_region,
    normalize_project_price_and_age,
    normalize_talent_price_and_age,
    parse_age_text,
    parse_price_text,
    parse_start_months,
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


RECEIVED_OCTOBER = "2026-10-29T01:04:00Z"


class StartMonthsTest(unittest.TestCase):
    def test_month_only(self):
        self.assertEqual(parse_start_months("11月～", RECEIVED_OCTOBER), ["2026-11"])
        self.assertEqual(parse_start_months("12月～長期", RECEIVED_OCTOBER), ["2026-12"])
        self.assertEqual(parse_start_months("11月 01日", RECEIVED_OCTOBER), ["2026-11"])
        self.assertEqual(parse_start_months("11/1～", RECEIVED_OCTOBER), ["2026-11"])

    def test_year_and_month(self):
        self.assertEqual(parse_start_months("2026年11月下旬頃から参画", RECEIVED_OCTOBER), ["2026-11"])

    def test_range_end_is_not_start(self):
        # 「12月～1月」の1月は期間の終わり
        self.assertEqual(parse_start_months("2026年12月～2027年1月 ※延長の可能性あり", RECEIVED_OCTOBER), ["2026-12"])

    def test_immediate_is_received_month(self):
        self.assertEqual(parse_start_months("即日", RECEIVED_OCTOBER), ["2026-10"])
        self.assertEqual(parse_start_months("来月から", RECEIVED_OCTOBER), ["2026-11"])
        for value in ("即~", "即 ~", "随時～長期", "確定後1週間～OK", "決まり次第"):
            self.assertEqual(parse_start_months(value, RECEIVED_OCTOBER), ["2026-10"], value)

    def test_alternatives(self):
        self.assertEqual(parse_start_months("即日 or 11月～", RECEIVED_OCTOBER), ["2026-10", "2026-11"])

    def test_next_year_month(self):
        # 10月受信の「1月～」は翌年1月
        self.assertEqual(parse_start_months("1月～", RECEIVED_OCTOBER), ["2027-01"])
        # 受信の前月は同じ年とみなす
        self.assertEqual(parse_start_months("9月～", RECEIVED_OCTOBER), ["2026-09"])

    def test_unknown(self):
        self.assertEqual(parse_start_months(None, RECEIVED_OCTOBER), [])
        self.assertEqual(parse_start_months("相談", RECEIVED_OCTOBER), [])
        # 受信日時が無いと年を決められないため、月だけの表記は読まない
        self.assertEqual(parse_start_months("11月～", None), [])


class RegionTest(unittest.TestCase):
    def test_kanto_words(self):
        for value in ("東京都＠五反田", "東京23区内", "大宮（リモート併用）", "横浜市内", "都内"):
            self.assertEqual(classify_region(value), REGION_KANTO, value)

    def test_other_region_words(self):
        for value in ("北野田駅(大阪)", "郡山駅", "天神駅", "名古屋駅周辺", "京都駅", "宮崎県"):
            self.assertEqual(classify_region(value), REGION_OTHER, value)

    def test_subject_is_used(self):
        self.assertEqual(classify_region("野田新町駅", "【11月／愛知】常駐可能｜Java"), REGION_OTHER)

    def test_kanto_wins_when_both_are_written(self):
        self.assertEqual(classify_region("大阪 or 東京"), REGION_KANTO)

    def test_station_only_is_unknown(self):
        # 「浜松町」「海老名駅」は関東（浜松・名駅と取り違えない）
        for value in ("茅場町", "溝の口", "飯田橋 or 水道橋", "浜松町", "海老名駅 徒歩10分", None, ""):
            self.assertIsNone(classify_region(value), value)

    def test_words_that_look_like_other_regions(self):
        # 「東京都」の京都、「東北沢」の東北、名字の山口・梅田は関東以外としない
        self.assertEqual(classify_region("東京都"), REGION_KANTO)
        for value in ("東北沢駅", "【山口】Java案件", "梅田さん"):
            self.assertIsNone(classify_region(value), value)

    def test_business_trip_is_ignored(self):
        self.assertIsNone(classify_region("茅場町（※九州出張あり）"))
        self.assertIsNone(classify_region("リモート ※月4回名古屋出張有り"))
        self.assertIsNone(classify_region("フルリモート（遠隔地からの参画も可）"))


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
