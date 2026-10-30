import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.classifier_utils import classify_ses_subject  # noqa: E402


PROJECT_KEYWORDS = ["案件", "募集", "急募"]
# config.yaml の talent_keywords の一部
TALENT_KEYWORDS = ["人材", "要員", "弊社社員", "フリーランス", "営業開始", "エンジニアのご紹介"]


def classify(subject):
    return classify_ses_subject(subject, PROJECT_KEYWORDS, TALENT_KEYWORDS)


class ClassifySubjectTest(unittest.TestCase):
    def test_project_looking_for_people_is_project(self):
        # 「要員」「人材」を含んでも、人を募集している件名は案件
        for subject in (
            "【GFD案件】インフラ 11月～ 上流NW要員募集／要件定義",
            "【Dynamics 365人材募集】",
            "【注力案件HB】Python要員探しています！11月開始",
            "★★【貴社要員様まで！】C言語／組み込み開発経験者募集！",
            "【弊社交代枠！】開発経験3年以上の方探しています！",
        ):
            self.assertEqual(classify(subject), "案件", subject)

    def test_talent_looking_for_projects_is_talent(self):
        # 人材側の「案件募集してます」は人材のまま
        for subject in (
            "☆【弊社独占フリーランスのご紹介】ゲーム系/プランナー/ディレクション案件募集してます！☆",
            "※本日営業開始※Rubyエンジニアのご紹介です！",
            "【直人材】★Ruby要員のご紹介★",
        ):
            self.assertEqual(classify(subject), "人材", subject)

    def test_project_keyword_only_is_project(self):
        self.assertEqual(classify("【急募】Java開発"), "案件")

    def test_no_keyword_is_unclassified(self):
        self.assertEqual(classify("ご挨拶"), "未分類")


if __name__ == "__main__":
    unittest.main()
