"""LLM が本文から書き写した単価・年齢の原文を、マッチングで使う数値へ変換します。

小さいモデルに単位換算（円→万円）や「40代まで→上限49」のような解釈をさせると誤りが多いため、
モデルには表記をそのまま抜き出させ、数値化はこのモジュールで決まった規則に従って行う。
"""
import re
import unicodedata
from typing import Any, Optional, Tuple


# 単価の妥当範囲（万円）。範囲外は単位ミス・抽出ミスとみなして捨てる（matching_engine と同じ値）。
PRICE_MAN_MIN = 30.0
PRICE_MAN_MAX = 300.0
# 年齢の妥当範囲。範囲外（生年の 1993 など）は年齢として扱わない。
AGE_MIN = 15
AGE_MAX = 80

_RANGE_SEPARATORS = r"[-~〜～ー－]"
# 精算幅（140-180h、160時間）と稼働率（100%稼働時）は単価と取り違えやすいため、単価の解析前に取り除く
_HOURS_PATTERN = re.compile(
    rf"\d{{2,3}}\s*(?:{_RANGE_SEPARATORS}\s*\d{{2,3}})?\s*(?:h|H|時間|hr)|\d+(?:\.\d+)?\s*%"
)
_PRICE_TOKEN_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(万円|万|円)?")
_PRICE_UPPER_PREFIX = re.compile(rf"(?:{_RANGE_SEPARATORS}|max|MAX|Max|上限|最大)\s*$")
# 「55歳ぐらいまで」「65万程度まで」のように、目安の語を挟む書き方も上限として扱う
_APPROXIMATE_WORDS = r"(?:ぐらい|くらい|位|程度|前後|ほど)?\s*"
_PRICE_UPPER_SUFFIX = re.compile(rf"^\s*(?:円)?\s*{_APPROXIMATE_WORDS}(?:まで|迄|以下|以内)")
_PRICE_LOWER_SUFFIX = re.compile(rf"^\s*(?:{_RANGE_SEPARATORS}|以上|から|〜)\s*(?:\D|$)")

_AGE_RANGE_PATTERN = re.compile(
    rf"(\d{{2}})\s*(歳|才|代)?\s*(?:{_RANGE_SEPARATORS}|から)\s*(\d{{2}})\s*(歳|才|代)(前半|半ば|後半)?"
)
_AGE_TOKEN_PATTERN = re.compile(r"(\d{2})\s*(歳|才|代)(前半|半ば|後半)?")
_AGE_UPPER_SUFFIX = re.compile(rf"^\s*{_APPROXIMATE_WORDS}(?:まで|迄|以下|以内|可|OK|ok)")
_AGE_EXCLUSIVE_SUFFIX = re.compile(r"^\s*未満")
_AGE_LOWER_SUFFIX = re.compile(rf"^\s*(?:以上|から|{_RANGE_SEPARATORS}\s*$)")
_AGE_UPPER_PREFIX = re.compile(rf"(?:{_RANGE_SEPARATORS}|上限|最大)\s*$")
# 「:24」「25際」のように単位が無い・誤字の年齢。数値が1つだけで前後が数文字の場合に限る
_AGE_BARE_PATTERN = re.compile(r"^\D{0,3}(\d{2})\D{0,3}$")


def _normalize_text(value: Any) -> str:
    """全角半角を統一し、桁区切りのカンマを除いた文字列にします。"""
    if value is None or isinstance(value, bool):
        return ""
    return unicodedata.normalize("NFKC", str(value)).replace(",", "").strip()


def _price_token_to_man(number_text: str, unit: Optional[str]) -> Optional[float]:
    """単価の数値1つを万円へ換算し、妥当範囲外なら None を返します。"""
    number = float(number_text)
    # 「円」表記と、単位なしの 10000 以上（650000 など）は円とみなして換算する
    if unit == "円" or (unit is None and number >= 10000):
        number = number / 10000
    if number < PRICE_MAN_MIN or number > PRICE_MAN_MAX:
        return None
    return number


def parse_price_text(value: Any) -> Tuple[Optional[float], Optional[float]]:
    """単価の原文を (下限, 上限) の万円へ変換します。

    「60~65万」は (60, 65)、「~65万」「MAX65万」「65万まで」は (None, 65)、
    「60万~」「60万以上」は (60, None)、「60万」「60万前後」は (60, 60)。
    「スキル見合い」のように数値が無い場合は (None, None)。

    :param value: 単価の原文。
    :return: (下限, 上限) のタプル。読み取れない側は None。
    """
    text = _HOURS_PATTERN.sub(" ", _normalize_text(value))
    if not text:
        return (None, None)

    tokens: list[tuple[float, int, int]] = []
    for match in _PRICE_TOKEN_PATTERN.finditer(text):
        price = _price_token_to_man(match.group(1), match.group(2))
        if price is not None:
            tokens.append((price, match.start(), match.end()))

    if not tokens:
        return (None, None)
    if len(tokens) >= 2:
        low, high = sorted((tokens[0][0], tokens[1][0]))
        return (low, high)

    price, start, end = tokens[0]
    if _PRICE_UPPER_PREFIX.search(text[:start]) or _PRICE_UPPER_SUFFIX.search(text[end:]):
        return (None, price)
    if _PRICE_LOWER_SUFFIX.search(text[end:]):
        return (price, None)
    return (price, price)


def _age_bounds(number: int, unit: Optional[str], part: Optional[str]) -> Tuple[int, int]:
    """年齢1つ（32歳、30代、30代後半）を (下限, 上限) にします。"""
    if unit != "代":
        return (number, number)
    if part == "前半":
        return (number, number + 4)
    if part == "半ば":
        return (number + 3, number + 6)
    if part == "後半":
        return (number + 5, number + 9)
    return (number, number + 9)


def _is_valid_age(number: int) -> bool:
    """年齢として妥当な数値か判定します。"""
    return AGE_MIN <= number <= AGE_MAX


def parse_age_text(value: Any) -> Tuple[Optional[int], Optional[int]]:
    """年齢の原文を (下限, 上限) へ変換します。

    「32歳」は (32, 32)、「30代」は (30, 39)、「40代まで」「~40代」は (None, 49)、
    「45歳以下」は (None, 45)、「45歳未満」は (None, 44)、「30歳以上」は (30, None)、
    「20~40代」は (20, 49)。「60代可」は上限の目安として (None, 69) とする。

    :param value: 年齢の原文。
    :return: (下限, 上限) のタプル。読み取れない側は None。
    """
    text = _normalize_text(value)
    if not text:
        return (None, None)
    # 数値だけが書かれている場合（「32」）は年齢そのものとみなす
    if text.isdigit():
        number = int(text)
        if _is_valid_age(number):
            return (number, number)
        return (None, None)

    range_match = _AGE_RANGE_PATTERN.search(text)
    if range_match:
        low_number, high_number = int(range_match.group(1)), int(range_match.group(3))
        if _is_valid_age(low_number) and _is_valid_age(high_number):
            # 「20~40代」のように前側の単位が省略された場合は、後ろ側の単位に合わせる
            low_unit = range_match.group(2) or range_match.group(4)
            low, _ = _age_bounds(low_number, low_unit, None)
            _, high = _age_bounds(high_number, range_match.group(4), range_match.group(5))
            return (min(low, high), max(low, high))

    token = _AGE_TOKEN_PATTERN.search(text)
    if not token:
        bare = _AGE_BARE_PATTERN.match(text)
        if bare and _is_valid_age(int(bare.group(1))):
            number = int(bare.group(1))
            return (number, number)
        return (None, None)
    if not _is_valid_age(int(token.group(1))):
        return (None, None)

    low, high = _age_bounds(int(token.group(1)), token.group(2), token.group(3))
    prefix, suffix = text[:token.start()], text[token.end():]
    if _AGE_EXCLUSIVE_SUFFIX.search(suffix):
        # 「45歳未満」は 44 まで、「50代未満」は 49 まで
        return (None, low - 1)
    if _AGE_UPPER_PREFIX.search(prefix) or _AGE_UPPER_SUFFIX.search(suffix):
        return (None, high)
    if _AGE_LOWER_SUFFIX.search(suffix):
        return (low, None)
    return (low, high)


def _format_number(value: Optional[float]) -> Optional[str]:
    """数値をDB保存用の文字列にします（60.0 は "60"）。"""
    if value is None:
        return None
    if float(value).is_integer():
        return str(int(value))
    return str(value)


def _format_range(low: Optional[float], high: Optional[float]) -> Optional[str]:
    """(下限, 上限) を "60" や "60-65" の形の文字列にします。片側のみなら、その値だけを返します。"""
    if low is None and high is None:
        return None
    if low is None or high is None or low == high:
        return _format_number(low if low is not None else high)
    return f"{_format_number(low)}-{_format_number(high)}"


def normalize_project_price_and_age(properties: dict[str, Any]) -> dict[str, Any]:
    """案件の「単価」「年齢」の原文から、単価下限/上限・年齢下限/上限を求めて追加します。

    原文の列（単価・年齢）は表示用にそのまま残す。

    :param properties: LLM の抽出結果（案件1件分）。
    :return: 下限/上限を追加した新しい辞書。
    """
    normalized = dict(properties)
    price_low, price_high = parse_price_text(properties.get("単価"))
    age_low, age_high = parse_age_text(properties.get("年齢"))
    normalized["単価下限"] = _format_number(price_low)
    normalized["単価上限"] = _format_number(price_high)
    normalized["年齢下限"] = _format_number(age_low)
    normalized["年齢上限"] = _format_number(age_high)
    return normalized


def normalize_talent_price_and_age(properties: dict[str, Any]) -> dict[str, Any]:
    """人材の「希望単価」「年齢」の原文を、"60" や "60-65" の形の数値文字列へ置き換えます。

    読み取れない場合は None にする（原文は json_data に残る）。

    :param properties: LLM の抽出結果（人材1件分）。
    :return: 希望単価・年齢を数値化した新しい辞書。
    """
    normalized = dict(properties)
    normalized["希望単価"] = _format_range(*parse_price_text(properties.get("希望単価")))
    normalized["年齢"] = _format_range(*parse_age_text(properties.get("年齢")))
    return normalized
