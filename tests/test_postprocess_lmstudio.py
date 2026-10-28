import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.postprocess_lmstudio import (  # noqa: E402
    _build_response_format,
    _load_case_schema_text,
    _load_human_schema_text,
    _strip_signature,
)


SIGNATURE = (
    "━━━━━━━━━━━━━━━━\n"
    "株式会社サンプル 営業部 山田\n"
    "TEL: 03-0000-0000\n"
    "Mail: yamada@example.com\n"
)


class StripSignatureTest(unittest.TestCase):
    def test_signature_below_delimiter_is_removed(self):
        body = "■単価:70万\n■年齢:32歳\n\n" + SIGNATURE
        self.assertEqual(_strip_signature(body, 300), "■単価:70万\n■年齢:32歳\n\n")

    def test_body_without_signature_is_kept(self):
        body = "■単価:70万\n■年齢:32歳\n"
        self.assertEqual(_strip_signature(body, 300), body)

    def test_info_below_delimiter_is_kept(self):
        # 区切り線の下に単価・年齢がある場合は署名とみなさない
        body = "ご紹介です\n━━━━━━━━━━\n■単価:70万\n■年齢:32歳\nTEL: 03-0000-0000\n"
        self.assertEqual(_strip_signature(body, 300), body)

    def test_signature_longer_than_limit_is_kept(self):
        body = "■単価:70万\n" + SIGNATURE
        self.assertEqual(_strip_signature(body, 10), body)

    def test_disabled(self):
        body = "■単価:70万\n" + SIGNATURE
        self.assertEqual(_strip_signature(body, 0), body)


class ResponseFormatTest(unittest.TestCase):
    def test_schemas_require_all_properties(self):
        for schema_text in (_load_case_schema_text(), _load_human_schema_text()):
            items = json.loads(schema_text)["items"]
            self.assertEqual(set(items["required"]), set(items["properties"]))

    def test_price_and_age_are_extracted_as_text(self):
        case = json.loads(_load_case_schema_text())["items"]["properties"]
        human = json.loads(_load_human_schema_text())["items"]["properties"]
        for properties, key in ((case, "単価"), (case, "年齢"), (human, "希望単価"), (human, "年齢")):
            self.assertEqual(properties[key]["type"], ["string", "null"], key)

    def test_build_response_format(self):
        response_format = _build_response_format(_load_case_schema_text(), "ses_project")
        self.assertEqual(response_format["type"], "json_schema")
        self.assertEqual(response_format["json_schema"]["name"], "ses_project")
        self.assertNotIn("$schema", response_format["json_schema"]["schema"])
        self.assertEqual(response_format["json_schema"]["schema"]["type"], "array")


if __name__ == "__main__":
    unittest.main()
