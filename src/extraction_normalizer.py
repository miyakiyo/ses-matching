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


# 「即~」「随時」「確定後1週間～」「決まり次第」も、すぐ開始できる意味として受信月にする
_IMMEDIATE_PATTERN = re.compile(r"即日|即時|即参画|即稼働|即\s*[~〜～-]|asap|すぐ|今月|随時|確定後|決まり次第|決定後")
_NEXT_MONTH_PATTERN = re.compile(r"来月|翌月")
_YEAR_MONTH_PATTERN = re.compile(r"(\d{4})\s*(?:年|/|-|\.)\s*(\d{1,2})(?:\s*月)?")
_MONTH_PATTERN = re.compile(r"(?<!\d)(\d{1,2})\s*(?:月|/\s*\d{1,2})")
# 「2026年12月～2027年1月」の 1月 のように、期間の終わりの月は開始月に含めない
_RANGE_END_PREFIX = re.compile(rf"{_RANGE_SEPARATORS}\s*(?:\d{{4}}\s*年)?\s*$")
# 受信月より前の月が書かれていたら翌年とみなす（10月受信の「1月～」は翌年1月）。
# 直前の月（受信の前月）は、書き手が受信日をまたいだだけとみなして同じ年にする
_PAST_MONTH_TOLERANCE = 1
MONTHS_PER_YEAR = 12


def _parse_received_month(received_at: Any) -> Optional[Tuple[int, int]]:
    """受信日時（"2026-10-27T00:03:23Z" など）から (年, 月) を取り出します。"""
    match = re.match(r"(\d{4})-(\d{2})", str(received_at or ""))
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)))


def _add_months(year: int, month: int, months: int) -> Tuple[int, int]:
    """(年, 月) に月数を足します。"""
    index = year * MONTHS_PER_YEAR + (month - 1) + months
    return (index // MONTHS_PER_YEAR, index % MONTHS_PER_YEAR + 1)


def _infer_year(month: int, received: Optional[Tuple[int, int]]) -> Optional[Tuple[int, int]]:
    """年の書かれていない月に、受信日から年を補います。受信日が無ければ判断できないため None。"""
    if received is None:
        return None
    year, received_month = received
    if month < received_month - _PAST_MONTH_TOLERANCE:
        year += 1
    return (year, month)


def parse_start_months(value: Any, received_at: Any) -> list[str]:
    """開始時期の原文から、開始できる月を "YYYY-MM" の一覧で返します。

    「即日」「今月」は受信月、「来月」は受信月の翌月とする。
    「即日 or 11月～」のように複数書かれていれば、すべてを開始できる月とする。
    「2026年12月～2027年1月」の終わりの月のように、期間の終わりは含めない。

    :param value: 案件の「時期」または人材の「稼働可能日」の原文。
    :param received_at: メールの受信日時（年の補完と「即日」の解釈に使う）。
    :return: 開始できる月の一覧（昇順）。読み取れなければ空。
    """
    text = _normalize_text(value).lower()
    if not text:
        return []
    received = _parse_received_month(received_at)
    months: set[Tuple[int, int]] = set()

    if received is not None and _IMMEDIATE_PATTERN.search(text):
        months.add(received)
    if received is not None and _NEXT_MONTH_PATTERN.search(text):
        months.add(_add_months(*received, 1))

    consumed: list[Tuple[int, int]] = []
    for match in _YEAR_MONTH_PATTERN.finditer(text):
        consumed.append(match.span())
        month = int(match.group(2))
        if not 1 <= month <= MONTHS_PER_YEAR or _RANGE_END_PREFIX.search(text[:match.start()]):
            continue
        months.add((int(match.group(1)), month))
    for match in _MONTH_PATTERN.finditer(text):
        if any(start <= match.start() < end for start, end in consumed):
            continue
        month = int(match.group(1))
        if not 1 <= month <= MONTHS_PER_YEAR or _RANGE_END_PREFIX.search(text[:match.start()]):
            continue
        inferred = _infer_year(month, received)
        if inferred is not None:
            months.add(inferred)

    return [f"{year:04d}-{month:02d}" for year, month in sorted(months)]


REGION_KANTO = "kanto"
REGION_OTHER = "other"

# 関東を示す語。駅名だけの記載が多く、関東の駅をすべて列挙はできないため、明示的な語だけを見る
_KANTO_PATTERN = re.compile(r"東京|都内|23区|神奈川|横浜|川崎|埼玉|さいたま|大宮|千葉|茨城|栃木|群馬|関東|首都圏")
# 関東以外を示す語（都道府県・主要都市・主要駅・地方名）。
# 名字と紛らわしい語（山口・宮崎・松本・福井・香川・富山・梅田など）は「県」「市」「駅」付きでだけ見る。
# 「東京都」の「京都」、「東北沢」の「東北」、「東海道線」の「東海」を拾わないよう、地方名は「地方」付きにする。
# 「浜松町」（東京）は浜松に含めない。「名駅」は「海老名駅」に当たるため使わない。
_OTHER_REGION_PATTERN = re.compile(
    r"北海道|札幌|青森|岩手|盛岡|宮城県|仙台|秋田|山形|福島県|郡山|いわき|新潟|富山県|石川県|金沢|福井県"
    r"|山梨|甲府|長野県|松本市|岐阜|静岡|浜松(?!町)|愛知|名古屋|刈谷|豊田市|三重|四日市|滋賀|(?<!東)京都"
    r"|大阪|梅田駅|難波|なんば|心斎橋|淀屋橋|兵庫|神戸|三宮|三ノ宮|姫路|奈良県|和歌山|鳥取|島根|岡山|広島"
    r"|山口県|徳島|香川県|高松市|愛媛|松山市|高知|福岡|博多|天神駅|北九州|佐賀|長崎|熊本|大分|宮崎県|鹿児島"
    r"|沖縄|那覇|九州|関西|東海地方|北陸|東北地方|四国"
)
# 「九州出張あり」「名古屋出張有り」「遠隔地からの参画も可」のような、勤務地そのものではない記載は見ない
_REGION_IGNORED_CLAUSE = re.compile(r"出張|遠隔地|地方可|全国")
_REGION_CLAUSE_SEPARATORS = re.compile(r"[、。,\n()（）※/／]")


def classify_region(*values: Any) -> Optional[str]:
    """最寄駅・作業場所・件名などから、関東か関東以外かを判定します。

    関東を示す語があれば関東、なければ関東以外を示す語があれば関東以外とする。
    どちらも無ければ None（駅名だけの記載が多く、判断できない）。

    :param values: 判定に使う文字列（最寄駅・作業場所・件名など）。
    :return: REGION_KANTO / REGION_OTHER / None。
    """
    clauses = []
    for value in values:
        text = _normalize_text(value)
        clauses.extend(
            clause for clause in _REGION_CLAUSE_SEPARATORS.split(text)
            if clause and not _REGION_IGNORED_CLAUSE.search(clause)
        )
    text = "\n".join(clauses)
    if _KANTO_PATTERN.search(text):
        return REGION_KANTO
    if _OTHER_REGION_PATTERN.search(text):
        return REGION_OTHER
    return None


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
