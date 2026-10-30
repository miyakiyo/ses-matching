from concurrent.futures import ProcessPoolExecutor, as_completed
import json
import os
import sqlite3
import re
import logging
import unicodedata
from typing import Optional, Tuple
from .database_utils import (
    get_talent_record,
    get_project_record,
    add_matches_bulk,
    delete_matches_for_non_active_records,
    delete_matches_not_in_pairs,
    delete_low_score_matches,
)
from .extraction_grounding import PROJECT_TECH_FIELDS, TALENT_TECH_FIELDS, ground_record
from .extraction_normalizer import REGION_OTHER, classify_region, parse_start_months


logger = logging.getLogger(__name__)

SKILL_ALIASES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "skill_aliases.csv",
)
_skill_aliases_cache: Optional[dict[str, str]] = None

# 単価の妥当範囲（万円）。範囲外は単位ミス・抽出ミスとみなして不明扱いにする。
PRICE_MAN_MIN = 30.0
PRICE_MAN_MAX = 300.0

# スキルの点数: 案件の技術名がすべて一致 / 半分以上一致
SKILL_SCORE_ALL = 50
SKILL_SCORE_HALF = 30
# 必須スキルが全部揃っていないペアの総合点の上限
MAX_SCORE_WITHOUT_ALL_SKILLS = 99
# 各項目の満点の合計（スキル50・単価25・担当10・工程10・立場制約25・年齢15・勤務形態15・開始時期15・勤務地15）。
# 項目ごとの満点はアプリの表示が前提にしているため変えず、総合点はこの合計を100点に換算する。
RAW_SCORE_MAX = 180
START_SCORE = 15
LOCATION_SCORE = 15

DEFAULT_MATCHING_CHUNK_SIZE = 50
DEFAULT_TALENT_LOG_INTERVAL = 100


def _normalize_list_value(field, split_plain_text: bool = True) -> list[str]:
    """配列値を正規化します（list と JSON文字列の両対応）。"""
    if field is None:
        return []

    if isinstance(field, list):
        return [str(v).strip() for v in field if str(v).strip()]

    text = str(field).strip()
    if not text:
        return []

    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
        except (json.JSONDecodeError, TypeError):
            pass

    if split_plain_text:
        parts = re.split(r"[、,，/／・;]+", text)
        normalized = [p.strip() for p in parts if p.strip()]
        if normalized:
            return normalized

    return [text]


def _parse_json_array(field) -> list:
    """配列項目をPython listに変換します（list と JSON文字列の両対応）。"""
    return _normalize_list_value(field, split_plain_text=True)


def _load_skill_aliases() -> dict[str, str]:
    """config/skill_aliases.csv（alias,canonical）を読み込みます。ファイルがなければ空。"""
    global _skill_aliases_cache
    if _skill_aliases_cache is not None:
        return _skill_aliases_cache

    aliases: dict[str, str] = {}
    try:
        with open(SKILL_ALIASES_PATH, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 2 and parts[0] and parts[1]:
                    aliases[unicodedata.normalize("NFKC", parts[0]).lower()] = (
                        unicodedata.normalize("NFKC", parts[1]).lower()
                    )
    except OSError:
        logger.warning("skill_aliases.csv を読み込めませんでした: %s", SKILL_ALIASES_PATH)

    _skill_aliases_cache = aliases
    return aliases


def _normalize_skill(skill: str) -> str:
    """スキル名を正規化します（全角半角統一、小文字化、前後の空白削除、別名の統一）。

    :param skill: スキル名。
    :return: 正規化されたスキル名。
    """
    if not skill:
        return ""
    normalized = unicodedata.normalize("NFKC", str(skill)).strip().lower()
    return _load_skill_aliases().get(normalized, normalized)


def _parse_price_range(price_str: Optional[str]) -> Tuple[Optional[float], Optional[float]]:
    """給与表記をパースして最小値と最大値を返します。

    :param price_str: 給与文字列（例: "80-100"、"90"、"80-100万円"）。
    :return: (最小値, 最大値) のタプル。パース失敗時は (None, None)。
    """
    if not price_str:
        return (None, None)
    
    price_str = str(price_str).strip()
    
    # "万円"や"円"などの単位を除去
    for unit in ["万円", "円", "%"]:
        price_str = price_str.replace(unit, "")
    
    price_str = price_str.strip()
    
    # 範囲表記をチェック（例: "80-100"）
    if "-" in price_str:
        parts = price_str.split("-")
        if len(parts) == 2:
            try:
                min_val = float(parts[0].strip())
                max_val = float(parts[1].strip())
                return (min_val, max_val)
            except ValueError:
                pass
    
    # 単一値
    try:
        val = float(price_str)
        return (val, val)
    except ValueError:
        pass
    
    return (None, None)


_ASCII_WORD_PATTERN = re.compile(r"[a-z0-9+#.]+")
# 日本語の技術名・用語を部分一致で探すときの最小文字数（短すぎる語の誤一致を防ぐ）
_MIN_JP_SUBSTRING_LENGTH = 3
_MIN_JP_PHASE_SUBSTRING_LENGTH = 4


def _skill_found(required: str, talent_skills: set, talent_jp_skills: list[str]) -> bool:
    """案件の必須スキル1件を、人材が持っているか判定します。

    完全一致のほか、「AWSにおける設計構築経験」のような文章形式の要件は、
    英数字の語（AWS）や日本語の用語（要件定義）が人材のスキルにあれば一致とみなす。
    英数字の語は単語として比較するため、Java と JavaScript は区別される。
    """
    if required in talent_skills:
        return True
    words = {w.strip(".") for w in _ASCII_WORD_PATTERN.findall(required)}
    if words & talent_skills:
        return True
    return any(skill in required for skill in talent_jp_skills)


def _requirement_items(items: list) -> set:
    """案件の要件（配列の各要素）を、分割せずに正規化した集合にします。"""
    normalized = {_normalize_skill(item) for item in items}
    normalized.discard("")
    return normalized


def _match_skills(
    talent_skills_dict: dict,
    project_required_skills: list,
    project_preferred_skills: list,
) -> Tuple[bool, int, dict]:
    """スキルマッチング判定を実施します。

    案件の必須スキル・開発言語・DB・OS/クラウドのうち、人材が持っている割合で点数を決める。
    すべて持っていれば 50 点、半分以上なら 30 点、それ未満は 0 点。尚可スキルは点数に含めない。

    :param talent_skills_dict: 人材スキル情報（"開発言語", "OS/クラウド", "データベース", "スキル", "資格", "工程"キーを持つ辞書）。
    :param project_required_skills: 案件の必須スキル・技術名（リスト）。
    :param project_preferred_skills: 案件の尚可スキル（リスト。詳細表示用で、点数には含めない）。
    :return: (マッチ判定, スコア加点, 詳細情報) の3要素タプル。
    """
    talent_all_skills = set()
    talent_jp_skills: list[str] = []
    for key in _SKILL_POOL_KEYS:
        tokens = _tech_tokens(talent_skills_dict.get(key))
        talent_all_skills |= tokens
        # 工程（開発・テストなど）は短い語が多く、部分一致だと誤って一致しやすいため、長い語だけを対象にする
        min_length = _MIN_JP_PHASE_SUBSTRING_LENGTH if key == "工程" else _MIN_JP_SUBSTRING_LENGTH
        talent_jp_skills.extend(t for t in tokens if len(t) >= min_length and not t.isascii())

    # 案件側は、文章形式の要件を分割しないよう、1件を1要件として扱う
    required = _requirement_items(project_required_skills)
    preferred = _requirement_items(project_preferred_skills)
    missing = sorted(r for r in required if not _skill_found(r, talent_all_skills, talent_jp_skills))
    required_matches = len(required) - len(missing)
    preferred_matches = sum(1 for r in preferred if _skill_found(r, talent_all_skills, talent_jp_skills))

    # 必須が無い案件は、割合を計算できないため 0 点
    ratio = required_matches / len(required) if required else 0.0
    if ratio >= 1.0:
        skill_score = SKILL_SCORE_ALL
    elif ratio >= 0.5:
        skill_score = SKILL_SCORE_HALF
    else:
        skill_score = 0
    required_ok = skill_score > 0

    details = {
        "required_match": required_ok,
        "required_matches": required_matches,
        "required_count": len(required),
        "missing": missing[:10],
        "preferred_matches": preferred_matches,
        "preferred_count": len(preferred),
        "talent_skill_count": len(talent_all_skills),
    }

    return (required_ok, skill_score, details)


def _parse_optional_number(value) -> Optional[float]:
    """数値または数値文字列を float へ変換します。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_optional_bool(value) -> Optional[bool]:
    """真偽値/真偽値文字列を bool へ変換します。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value == 1:
            return True
        if value == 0:
            return False

    text = str(value).strip().lower()
    if not text or text in ("none", "null"):
        return None
    if text in ("true", "1", "yes", "y", "はい", "可", "ok"):
        return True
    if text in ("false", "0", "no", "n", "いいえ", "不可", "ng"):
        return False
    return None


def _normalize_price_man(value) -> Optional[float]:
    """単価を万円単位へ正規化します。

    円表記（950000）は万円へ換算し、「72万円」「45万前後」のような文字列は数値部分を使う。
    正規化後に PRICE_MAN_MIN〜PRICE_MAN_MAX の範囲外なら異常値として None を返す。
    """
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = unicodedata.normalize("NFKC", str(value)).replace(",", "").strip()
        match = re.search(r"\d+(?:\.\d+)?", text)
        if not match:
            return None
        number = float(match.group(0))

    if number >= 10000:
        number = number / 10000

    if number < PRICE_MAN_MIN or number > PRICE_MAN_MAX:
        return None
    return number


def _parse_project_price_range(project_record: dict) -> Tuple[Optional[float], Optional[float]]:
    """案件レコードから単価下限/単価上限を取得し、万円単位へ正規化します。"""
    project_min = _normalize_price_man(project_record.get("単価下限"))
    project_max = _normalize_price_man(project_record.get("単価上限"))

    if project_min is None and project_max is not None:
        project_min = project_max
    if project_max is None and project_min is not None:
        project_max = project_min

    # 下限と上限が逆転している場合は入れ替えて補正する。
    if project_min is not None and project_max is not None and project_min > project_max:
        project_min, project_max = project_max, project_min

    return (project_min, project_max)


def _parse_talent_price_range(talent_price) -> Tuple[Optional[float], Optional[float]]:
    """人材の希望単価を万円単位の min/max レンジへ正規化します。"""
    if talent_price is None or isinstance(talent_price, bool):
        return (None, None)

    if isinstance(talent_price, str):
        text = unicodedata.normalize("NFKC", talent_price)
        numbers = re.findall(r"\d[\d,]*(?:\.\d+)?", text)
        # 「80-100万円」「80〜100」のような範囲表記
        if len(numbers) >= 2 and re.search(r"\d\s*(?:[-~〜～ー])\s*\d", text):
            low = _normalize_price_man(numbers[0])
            high = _normalize_price_man(numbers[1])
            if low is not None and high is not None:
                return (min(low, high), max(low, high))

    single_value = _normalize_price_man(talent_price)
    if single_value is not None:
        return (single_value, single_value)
    return (None, None)


def _match_salary(talent_price: Optional[str], project_record: dict) -> Tuple[bool, int, dict]:
    """給与マッチング判定を実施します。

    :param talent_price: 人材の希望単価。
    :param project_record: 案件レコード辞書。
    :return: (マッチ判定, スコア加点, 詳細情報) の3要素タプル。
    """
    talent_min, talent_max = _parse_talent_price_range(talent_price)
    project_min, project_max = _parse_project_price_range(project_record)
    
    # どちらかが parse に失敗したら、スキップ（条件なし）
    if talent_min is None or project_min is None:
        return (True, 0, {"status": "unknown_price"})
    
    # 案件予算が人材要望内かチェック
    # 人材の要望 min≦ 案件 max かつ 人材の要望 max≧ 案件 min なら重複範囲あり
    match = talent_min <= project_max and talent_max >= project_min

    salary_score = 25 if match else 0

    details = {
        "match": match,
        "talent_range": (talent_min, talent_max),
        "project_range": (project_min, project_max),
    }

    # 人材の希望単価の下限が案件の上限を超える（単価逆転）場合は除外対象とする。
    if talent_min > project_max:
        details["excluded"] = "price_inversion"
    
    return (match, salary_score, details)


def _match_constraints(talent_record: dict, project_record: dict) -> Tuple[bool, int, dict]:
    """立場制約マッチング判定を実施します（外国籍、個人事業主等）。

    :param talent_record: 人材レコード辞書。
    :param project_record: 案件レコード辞書。
    :return: (マッチ判定, スコア加点, 詳細情報) の3要素タプル。
    """
    constraints_ok = True
    details = {}
    
    # 外国籍チェック
    talent_foreign = _parse_optional_bool(talent_record.get("外国籍"))
    project_foreign_ok = _parse_optional_bool(project_record.get("外国籍可否"))

    if project_foreign_ok is not None:
        # 案件が外国籍可否を指定している場合
        if talent_foreign is True:
            if project_foreign_ok is not True:
                constraints_ok = False
            details["foreign"] = "talent_is_foreign_but_project_not_ok"
        elif talent_foreign is False:
            details["foreign"] = "talent_domestic_ok"
        else:
            details["foreign"] = "talent_foreign_unknown"
    else:
        details["foreign"] = "project_no_restriction"
    
    # 個人事業主チェック
    talent_freelance = _parse_optional_bool(talent_record.get("個人事業主"))
    project_freelance_ok = _parse_optional_bool(project_record.get("個人事業主可否"))

    if project_freelance_ok is not None:
        if talent_freelance is True:
            if project_freelance_ok is not True:
                constraints_ok = False
            details["freelance"] = "talent_is_freelance_but_project_not_ok"
        elif talent_freelance is False:
            details["freelance"] = "talent_employee_ok"
        else:
            details["freelance"] = "talent_freelance_unknown"
    else:
        details["freelance"] = "project_no_restriction"

    # 商流制限_貴社所属迄チェック
    # True の場合、人材の所属が弊社直接雇用（「弊社」「直」を含む）である必要がある
    project_direct_only = _parse_optional_bool(project_record.get("商流制限_貴社所属迄"))
    if project_direct_only is True:
        talent_belonging = str(talent_record.get("所属") or "").strip()
        is_direct = any(kw in talent_belonging for kw in ["弊社", "直属", "直雇", "直接"])
        if not is_direct:
            constraints_ok = False
        details["direct_only"] = "match" if is_direct else "talent_not_direct"
    else:
        details["direct_only"] = "no_restriction"

    # 派遣案件と1社下所属の不適合チェック
    # 派遣案件で人材が「1社下所属」の場合は NG
    contract_type = str(project_record.get("契約形態") or "").strip()
    talent_belonging = str(talent_record.get("所属") or "").strip()
    
    if contract_type == "派遣" and "1社下" in talent_belonging:
        constraints_ok = False
        details["dispatch_secondment"] = "ng_dispatch_with_secondment"
    else:
        details["dispatch_secondment"] = "ok" if contract_type == "派遣" else "not_dispatch"

    constraint_score = 25 if constraints_ok else 0
    
    return (constraints_ok, constraint_score, details)


def _parse_multi_value_field(field) -> list[str]:
    """複数値フィールド（list / JSON配列文字列 / 区切り文字列）を正規化して返します。"""
    values = _normalize_list_value(field, split_plain_text=True)
    return [value.lower() for value in values]


def _role_phase_detail(status: str, talent_values: list[str], project_values: list[str], overlap: list[str]) -> dict:
    """担当・工程の判定詳細を作成します。値は不一致のときだけ含めます。"""
    detail: dict = {"status": status, "overlap": overlap}
    if status != "matched":
        detail["talent_values"] = talent_values
        detail["project_values"] = project_values
    return detail


def _match_role_and_phase(talent_record: dict, project_record: dict) -> Tuple[bool, int, dict]:
    """担当と工程のマッチング判定を実施します（ソフト制約）。"""
    talent_roles = _parse_multi_value_field(talent_record.get("担当"))
    project_roles = _parse_multi_value_field(project_record.get("担当"))
    role_overlap = sorted(set(talent_roles) & set(project_roles))

    if talent_roles and project_roles:
        role_status = "matched" if role_overlap else "not_matched"
        role_score = 10 if role_overlap else 0
    else:
        role_status = "unknown"
        role_score = 0

    talent_phases = _parse_multi_value_field(talent_record.get("工程"))
    project_phases = _parse_multi_value_field(project_record.get("工程"))
    phase_overlap = sorted(set(talent_phases) & set(project_phases))

    if talent_phases and project_phases:
        phase_status = "matched" if phase_overlap else "not_matched"
        phase_score = 10 if phase_overlap else 0
    else:
        phase_status = "unknown"
        phase_score = 0

    # reason を DB に保存するときのサイズを抑えるため、双方の値は一致しなかった場合だけ残す
    # （一致した場合は overlap で足りる）。
    details = {
        "role": _role_phase_detail(role_status, talent_roles, project_roles, role_overlap),
        "phase": _role_phase_detail(phase_status, talent_phases, project_phases, phase_overlap),
    }

    return (True, role_score + phase_score, details)


def _parse_optional_int(value) -> Optional[int]:
    """整数または整数文字列を整数へ変換します。"""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if not s:
        return None
    if s.lower() in ('null', 'none'):
        return None
    import re
    m = re.search(r'\d+', s)
    if m:
        return int(m.group(0))
    return None


def _parse_talent_age_range(talent_record: dict) -> Tuple[Optional[int], Optional[int]]:
    """人材レコードから単一年齢を取得し、年齢レンジへ正規化します。"""
    age_value = _parse_optional_int(talent_record.get('年齢'))
    if age_value is not None:
        return age_value, age_value

    # 互換性のため、旧データの年齢下限/上限がある場合は利用する。
    talent_age_min = _parse_optional_int(talent_record.get('年齢下限'))
    talent_age_max = _parse_optional_int(talent_record.get('年齢上限'))

    # 片側のみ指定された場合は同値で補完する。
    if talent_age_min is None and talent_age_max is not None:
        talent_age_min = talent_age_max
    if talent_age_max is None and talent_age_min is not None:
        talent_age_max = talent_age_min

    return talent_age_min, talent_age_max


def _parse_project_age_range(project_record: dict) -> Tuple[Optional[int], Optional[int]]:
    """案件レコードから年齢下限/上限を取得します。"""
    age_min = _parse_optional_int(project_record.get('年齢下限'))
    age_max = _parse_optional_int(project_record.get('年齢上限'))

    return age_min, age_max


def _match_age(talent_record: dict, project_record: dict) -> Tuple[bool, int, dict]:
    """年齢マッチング判定を実施します（ソフト制約）。

    :param talent_record: 人材レコード辞書。
    :param project_record: 案件レコード辞書。
    :return: (True, スコア, 詳細) の3要素タプル。
    """
    talent_age_min, talent_age_max = _parse_talent_age_range(talent_record)
    project_age_min, project_age_max = _parse_project_age_range(project_record)

    # 範囲の逆転があれば年齢判定は無効化する。
    if (
        talent_age_min is not None and talent_age_max is not None and talent_age_min > talent_age_max
    ):
        return (True, 0, {'status': 'invalid_talent_age_range'})
    if (
        project_age_min is not None and project_age_max is not None and project_age_min > project_age_max
    ):
        return (True, 0, {'status': 'invalid_project_age_range'})

    # 案件に年齢制限が無い場合は、どの年齢でも条件を満たすため満点とする。
    # 以前は「不明」として 0 点にしていたため、案件の約半数で 100 点にならなかった。
    if project_age_min is None and project_age_max is None:
        return (
            True,
            15,
            {
                'status': 'no_limit',
                'talent_age_min': talent_age_min,
                'talent_age_max': talent_age_max,
                'within_limit': True,
            },
        )

    # 案件に年齢制限があり、人材の年齢が不明な場合は判定できないため 0 点とする
    if talent_age_min is None and talent_age_max is None:
        return (True, 0, {'status': 'unknown_age'})

    # 人材年齢レンジと案件制約レンジの重なりで判定する。
    within_lower = True if project_age_min is None else talent_age_max >= project_age_min
    within_upper = True if project_age_max is None else talent_age_min <= project_age_max
    within = within_lower and within_upper
    score = 15 if within else 0
    return (
        True,  # ソフト制約なので常に True
        score,
        {
            'talent_age_min': talent_age_min,
            'talent_age_max': talent_age_max,
            'project_age_min': project_age_min,
            'project_age_max': project_age_max,
            'within_limit': within,
        },
    )


REMOTE_FULL = "full_remote"
REMOTE_HYBRID = "hybrid"
REMOTE_ONSITE = "onsite"
REMOTE_UNKNOWN = "unknown"

_REMOTE_UNKNOWN_TOKENS = {"", "[]", "不明", "未記載", "記載なし", "なし", "none", "null", "-", "－"}
# 「リモート不可」「在宅なし」は出社。リモートの語を含むため、他の判定より先に見る
_REMOTE_NEGATION_PATTERN = re.compile(r"(?:リモート|在宅|テレワーク)(?:ワーク)?\s*(?:不可|なし|無し|ng|できません)")
_REMOTE_WEEKLY_OFFICE_PATTERN = re.compile(r"週\s*(\d)\s*(?:日)?\s*(?:程度|以上|以下|ほど)?\s*(?:の)?\s*(?:出社|出勤)")
_REMOTE_HYBRID_PATTERN = re.compile(r"一部|ハイブリッド|併用|月\s*\d\s*回")
_REMOTE_FULL_PATTERN = re.compile(
    r"フルリモ|完全リモート|全日リモート|リモート\s*100|100\s*%\s*リモート|完全在宅|フル在宅|リモートのみ|リモート希望"
    r"|リモート必須|原則リモート|原則在宅|在宅希望|在宅のみ|基本リモート|リモート中心"
)
# 「初日・緊急時・月1回なら出社可」のような例外的な出社。フルリモートの判定を妨げないよう除いてから見る
_REMOTE_EXCEPTIONAL_ONSITE_PATTERN = re.compile(
    r"(?:初日|初回|緊急時?|必要に応じて?|mtg|会議|打ち合わせ|研修|入場|キックオフ|月\s*\d\s*回?|年\s*\d\s*回?|たまに|稀に|まれに)"
    r"[^、。/()]*?(?:出社|出勤|訪問)[^、。/()]*"
)
_REMOTE_ONSITE_PATTERN = re.compile(r"出社|常駐|オンサイト|客先|現地")
_REMOTE_ANY_PATTERN = re.compile(r"リモート|在宅|テレワーク")

# 週あたりの出社日数。日数の書かれていない「一部リモート」は、人材側は週4日まで出社できる、
# 案件側は週1日以上の出社が必要、とみなす（どちらも一致しやすい側に倒す）。
OFFICE_DAYS_FULL_REMOTE = 0
OFFICE_DAYS_ONSITE = 5
OFFICE_DAYS_TALENT_HYBRID = 4
OFFICE_DAYS_PROJECT_HYBRID = 1
REMOTE_SCORE = 15


def _normalize_remote_text(value) -> str:
    """リモート頻度の値を、判定用の小文字の文字列にします。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value)
    return unicodedata.normalize("NFKC", str(value)).strip().lower()


def _normalize_remote(value) -> str:
    """リモート頻度の自由文を full_remote / hybrid / onsite / unknown に正規化します。

    判断できない場合は unknown とし、除外には使わない。
    """
    text = _normalize_remote_text(value)
    if text in _REMOTE_UNKNOWN_TOKENS:
        return REMOTE_UNKNOWN
    # プロンプトの例文（「例: フルリモート/週2出社 など」）がそのまま抽出されたもの
    if "例:" in text or ("など" in text and "/" in text):
        return REMOTE_UNKNOWN

    if _REMOTE_NEGATION_PATTERN.search(text):
        return REMOTE_ONSITE
    if _REMOTE_WEEKLY_OFFICE_PATTERN.search(text):
        return REMOTE_HYBRID

    if _REMOTE_FULL_PATTERN.search(text):
        # 「フルリモート（出社も可）」のように、例外以外の出社にも触れている場合は断定しない
        rest = _REMOTE_EXCEPTIONAL_ONSITE_PATTERN.sub("", text)
        return REMOTE_HYBRID if _REMOTE_ONSITE_PATTERN.search(rest) else REMOTE_FULL
    if _REMOTE_HYBRID_PATTERN.search(text):
        return REMOTE_HYBRID

    has_onsite = bool(_REMOTE_ONSITE_PATTERN.search(text))
    has_remote = bool(_REMOTE_ANY_PATTERN.search(text))
    if has_onsite and has_remote:
        return REMOTE_HYBRID
    if has_onsite:
        return REMOTE_ONSITE
    if has_remote:
        return REMOTE_HYBRID
    return REMOTE_UNKNOWN


def _office_days(value, hybrid_days: int) -> Optional[int]:
    """リモート頻度から、週あたりの出社日数を求めます。判断できない場合は None。

    :param value: リモート頻度の値。
    :param hybrid_days: 日数の書かれていない「一部リモート」のときに使う日数。
    :return: 週あたりの出社日数（0〜5）。
    """
    category = _normalize_remote(value)
    if category == REMOTE_UNKNOWN:
        return None
    if category == REMOTE_FULL:
        return OFFICE_DAYS_FULL_REMOTE
    if category == REMOTE_ONSITE:
        return OFFICE_DAYS_ONSITE
    weekly = _REMOTE_WEEKLY_OFFICE_PATTERN.search(_normalize_remote_text(value))
    if weekly:
        return min(OFFICE_DAYS_ONSITE, int(weekly.group(1)))
    return hybrid_days


def _remote_display_value(value) -> Optional[str]:
    """リモート頻度を、判定理由に残す表示用の文字列にします。無ければ None。"""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value)
    text = str(value).strip()
    if not text:
        return None
    return text


def _check_remote_exclusion(talent_record: dict, project_record: dict) -> Optional[dict]:
    """人材がフルリモート希望で、案件が出社（週1日以上を含む）の場合に除外理由を返します。"""
    talent_remote = _normalize_remote(talent_record.get("リモート頻度"))
    project_remote = _normalize_remote(project_record.get("リモート頻度"))
    if talent_remote == REMOTE_FULL and project_remote in (REMOTE_ONSITE, REMOTE_HYBRID):
        return {
            "rule": "remote_mismatch",
            "detail": {"talent": talent_remote, "project": project_remote},
        }
    return None


def _match_remote(talent_record: dict, project_record: dict) -> Tuple[bool, int, dict]:
    """勤務形態のマッチング判定を実施します（ソフト制約）。

    案件が求める週の出社日数が、人材の出社できる日数以内なら一致。
    どちらかが不明な場合は判定せず 0 点とする（不一致には含めない）。
    """
    talent_remote = talent_record.get("リモート頻度")
    project_remote = project_record.get("リモート頻度")
    talent_days = _office_days(talent_remote, OFFICE_DAYS_TALENT_HYBRID)
    project_days = _office_days(project_remote, OFFICE_DAYS_PROJECT_HYBRID)
    # 日数は「一部リモート」を仮の日数で置いた値を含むため、画面表示用に原文も残す
    details: dict = {
        "talent_office_days": talent_days,
        "project_office_days": project_days,
        "talent_value": _remote_display_value(talent_remote),
        "project_value": _remote_display_value(project_remote),
    }
    if talent_days is None or project_days is None:
        details["status"] = "unknown"
        return (True, 0, details)

    match = project_days <= talent_days
    details["status"] = "matched" if match else "not_matched"
    return (match, REMOTE_SCORE if match else 0, details)


def _match_start(talent_record: dict, project_record: dict) -> Tuple[bool, int, dict]:
    """開始時期のマッチング判定を実施します（ソフト制約）。

    案件の開始月と人材の稼働可能月が同じなら一致。営業の判断では、人材が11月から稼働できても
    12月開始の案件には提案しにくいため、月が違えば不一致とする（除外まではしない）。
    どちらかが読み取れない場合は判定せず 0 点とする（不一致には含めない）。
    """
    talent_value = _remote_display_value(talent_record.get("稼働可能日"))
    project_value = _remote_display_value(project_record.get("時期"))
    talent_months = parse_start_months(talent_value, talent_record.get("received_at"))
    project_months = parse_start_months(project_value, project_record.get("received_at"))
    details: dict = {
        "talent_months": talent_months,
        "project_months": project_months,
        "talent_value": talent_value,
        "project_value": project_value,
    }
    if not talent_months or not project_months:
        details["status"] = "unknown"
        return (True, 0, details)

    match = bool(set(talent_months) & set(project_months))
    details["status"] = "matched" if match else "not_matched"
    return (match, START_SCORE if match else 0, details)


def _match_location(talent_record: dict, project_record: dict) -> Tuple[bool, int, dict]:
    """勤務地のマッチング判定を実施します（ソフト制約）。

    取引の対象は関東のため、人材の最寄駅・案件の作業場所のどちらかが関東以外なら 0 点とする。
    案件がフルリモートなら、人材の場所は問わない。
    地名から判断できない場合は関東とみなす（最寄駅・作業場所は駅名だけの記載が大半で、
    関東の駅をすべて列挙できないため。関東以外の地名は件名にも書かれることが多い）。
    """
    talent_value = _remote_display_value(talent_record.get("最寄駅"))
    project_value = _remote_display_value(project_record.get("作業場所"))
    talent_region = classify_region(talent_value, talent_record.get("subject"))
    project_region = classify_region(project_value, project_record.get("subject"))
    details: dict = {
        "talent_region": talent_region,
        "project_region": project_region,
        "talent_value": talent_value,
        "project_value": project_value,
    }
    if _normalize_remote(project_record.get("リモート頻度")) == REMOTE_FULL:
        details["status"] = "remote"
        return (True, LOCATION_SCORE, details)

    match = talent_region != REGION_OTHER and project_region != REGION_OTHER
    details["status"] = "matched" if match else "not_matched"
    return (match, LOCATION_SCORE if match else 0, details)


# 足切りは開発言語のみ。DB・OS/クラウドは人材側の記載が疎で、正しいペアを落とす率が高い（実データで確認）。
_TECH_GATE_KEYS = ("開発言語",)
_TALENT_SKILL_KEYS = ("開発言語", "OS/クラウド", "データベース", "スキル", "資格")
# スキル点数の照合では、必須スキルに工程（要件定義など）が書かれることがあるため、人材の工程も含める
_SKILL_POOL_KEYS = _TALENT_SKILL_KEYS + ("工程",)


def _tech_tokens(field) -> set:
    """技術名の配列項目を正規化したトークン集合へ変換します（「MySQL/MariaDB」のような連結も分割）。"""
    tokens = set()
    for item in _parse_json_array(field):
        for part in re.split(r"[/／・,，、;]+", str(item)):
            normalized = _normalize_skill(part)
            if normalized:
                tokens.add(normalized)
        whole = _normalize_skill(item)
        if whole:
            tokens.add(whole)
    return tokens


def _check_required_tech(talent_record: dict, project_record: dict) -> Optional[dict]:
    """案件の開発言語に対する足切り判定を行います。

    案件が開発言語を指定しているのに、人材側のスキル情報（言語/スキル/資格など）に
    重なりがない場合に除外理由を返す（案件内の複数言語はOR）。
    人材側のスキル情報が全く無い場合、案件の開発言語が空の場合は判断できないため通す。
    """
    talent_skills = set()
    for key in _TALENT_SKILL_KEYS:
        talent_skills.update(_tech_tokens(talent_record.get(key)))
    if not talent_skills:
        return None

    project_by_key = {}
    for key in _TECH_GATE_KEYS:
        project_by_key[key] = _tech_tokens(project_record.get(key))

    for key in _TECH_GATE_KEYS:
        required = project_by_key[key]
        if required and not (required & talent_skills):
            return {
                "rule": "required_tech_mismatch",
                "detail": {"category": key, "project": sorted(required)},
            }
    return None


def _excluded_result(rule: dict, reason: dict) -> Tuple[int, dict]:
    """除外時の返り値（スコア0と除外理由）を作成します。"""
    reason = dict(reason)
    reason["excluded"] = rule
    return (0, reason)


def calculate_match_score(talent_record: dict, project_record: dict) -> Tuple[int, dict]:
    """人材と案件のマッチングスコアを計算します。

    除外条件（必須技術名の不一致・単価逆転・年齢範囲外・リモート不一致・立場制約の違反）に
    該当する場合はスコア 0 を返し、reason["excluded"] に理由を入れます。

    :param talent_record: 人材レコード辞書。
    :param project_record: 案件レコード辞書。
    :return: (総合スコア 0-100, 詳細理由 JSON) のタプル。
    """
    total_score = 0
    reason = {}

    # 0. 除外条件（ハードフィルタ）。安価な判定から順に行う。
    for check in (_check_remote_exclusion, _check_required_tech):
        excluded = check(talent_record, project_record)
        if excluded:
            return _excluded_result(excluded, reason)

    # 1. スキルマッチング
    # 案件の必須スキルに、開発言語・データベース・OS/クラウドを加えたものを、一致を見る対象とする。
    project_required = _parse_json_array(project_record.get("必須スキル"))
    for key in ["開発言語", "データベース", "OS/クラウド"]:
        project_required.extend(_parse_json_array(project_record.get(key)))
    project_preferred = _parse_json_array(project_record.get("尚可スキル"))
    
    skill_match, skill_score, skill_details = _match_skills(
        {
            "開発言語": talent_record.get("開発言語"),
            "OS/クラウド": talent_record.get("OS/クラウド"),
            "データベース": talent_record.get("データベース"),
            "スキル": talent_record.get("スキル"),
            "資格": talent_record.get("資格"),
            "工程": talent_record.get("工程"),
        },
        project_required,
        project_preferred,
    )
    total_score += skill_score
    reason["skills"] = {
        "match": skill_match,
        "score": skill_score,
        "details": skill_details,
    }
    # 案件の技術名の半分未満しか人材が持っていない場合は除外する。
    # 案件に技術名が無い、または人材のスキル情報が無い場合は判断できないので除外しない。
    if skill_details["required_count"] > 0 and skill_details["talent_skill_count"] > 0 and not skill_match:
        return _excluded_result({"rule": "skills_below_half", "detail": skill_details}, reason)
    
    # 2. 給与マッチング
    salary_match, salary_score, salary_details = _match_salary(
        talent_record.get("希望単価"),
        project_record,
    )
    total_score += salary_score
    reason["salary"] = {
        "match": salary_match,
        "score": salary_score,
        "details": salary_details,
    }
    if salary_details.get("excluded"):
        return _excluded_result(
            {"rule": salary_details["excluded"], "detail": salary_details},
            reason,
        )
    
    # 3. 担当・工程マッチング
    role_phase_match, role_phase_score, role_phase_details = _match_role_and_phase(
        talent_record,
        project_record,
    )
    total_score += role_phase_score
    reason["role_phase"] = {
        "match": role_phase_match,
        "score": role_phase_score,
        "details": role_phase_details,
    }

    # 4. 立場制約チェック
    constraint_match, constraint_score, constraint_details = _match_constraints(
        talent_record,
        project_record,
    )
    total_score += constraint_score
    reason["constraints"] = {
        "match": constraint_match,
        "score": constraint_score,
        "details": constraint_details,
    }
    # 外国籍不可・個人事業主不可・貴社所属まで・派遣の1社下は、提案できないため除外する。
    if not constraint_match:
        return _excluded_result({"rule": "constraints_mismatch", "detail": constraint_details}, reason)

    # 4.5 勤務形態（リモート頻度）
    remote_match, remote_score, remote_details = _match_remote(talent_record, project_record)
    total_score += remote_score
    reason["remote"] = {
        "match": remote_match,
        "score": remote_score,
        "details": remote_details,
    }

    # 4.6 開始時期
    start_match, start_score, start_details = _match_start(talent_record, project_record)
    total_score += start_score
    reason["start"] = {
        "match": start_match,
        "score": start_score,
        "details": start_details,
    }

    # 4.7 勤務地
    location_match, location_score, location_details = _match_location(talent_record, project_record)
    total_score += location_score
    reason["location"] = {
        "match": location_match,
        "score": location_score,
        "details": location_details,
    }

    # 5. 年齢
    age_match, age_score, age_details = _match_age(
        talent_record,
        project_record,
    )
    total_score += age_score
    reason["age"] = {
        "match": age_match,
        "score": age_score,
        "details": age_details,
    }
    # 人材が案件の年齢上限を超えている場合のみ除外する。年齢は抽出のぶれが大きいため、
    # 下限側の判定と、不明・上限なし（0/120 等の見かけ上の上限を含む）は除外に使わない。
    talent_age_min = age_details.get("talent_age_min")
    project_age_max = age_details.get("project_age_max")
    if (
        talent_age_min is not None
        and project_age_max is not None
        and 0 < project_age_max < 100
        and talent_age_min > project_age_max
    ):
        return _excluded_result({"rule": "age_out_of_range", "detail": age_details}, reason)

    # 各項目の満点の合計（RAW_SCORE_MAX）を 100 点に換算する。
    # 以前は合計 135 点を 100 点で頭打ちにしていたため、単価が不明・不一致でも 100 点になっていた。
    # 不明な項目は 0 点のため、すべての項目が一致した場合だけ 100 点になる。
    total_score = round(total_score * 100 / RAW_SCORE_MAX)
    # 必須スキルが全部揃っていない（スキル 50 点でない）場合は、100 点にしない。
    max_score = 100 if skill_score >= SKILL_SCORE_ALL else MAX_SCORE_WITHOUT_ALL_SKILLS
    total_score = min(max_score, total_score)
    # 除外されていないペアが 0 点にならないよう 1 点を下限にする（0 点は除外の意味で使っているため）
    total_score = max(1, total_score)

    return (total_score, reason)


def _is_same_sender(talent_record: dict, project_record: dict) -> bool:
    """folder または sender_addr が一致する場合に同一送信者とみなします。"""
    talent_folder = str(talent_record.get("folder") or "").strip().lower()
    project_folder = str(project_record.get("folder") or "").strip().lower()
    if talent_folder and project_folder and talent_folder == project_folder:
        return True

    talent_sender_addr = str(talent_record.get("sender_addr") or "").strip().lower()
    project_sender_addr = str(project_record.get("sender_addr") or "").strip().lower()
    if talent_sender_addr and project_sender_addr and talent_sender_addr == project_sender_addr:
        return True

    return False


def _chunk_values(values: list[int], chunk_size: int) -> list[list[int]]:
    """整数ID配列を固定長チャンクへ分割します。"""
    normalized_chunk_size = max(1, int(chunk_size or DEFAULT_MATCHING_CHUNK_SIZE))
    return [values[idx: idx + normalized_chunk_size] for idx in range(0, len(values), normalized_chunk_size)]


def _load_project_records(
    conn: sqlite3.Connection,
    project_ids: list[int],
) -> list[tuple[int, dict]]:
    """案件レコードを一度だけ読み込み、評価用に保持します（件名・本文に根拠の無い抽出値は除く）。"""
    project_records: list[tuple[int, dict]] = []
    for project_id in sorted(project_ids):
        project_record = get_project_record(conn, project_id)
        if project_record:
            project_records.append(
                (project_id, ground_record(project_record, PROJECT_TECH_FIELDS, _load_skill_aliases()))
            )
    return project_records


def _load_talent_record(conn: sqlite3.Connection, talent_id: int) -> dict:
    """人材レコードを読み込みます（件名・本文に根拠の無い抽出値は除く）。見つからなければ空の辞書。"""
    talent_record = get_talent_record(conn, talent_id)
    if not talent_record:
        return {}
    return ground_record(talent_record, TALENT_TECH_FIELDS, _load_skill_aliases())


def _get_talent_subject(talent_record: dict) -> str:
    """人材レコードから件名を安全に取り出します。"""
    return str(talent_record.get("subject") or "").strip()


def _collect_match_rows_for_talent(
    talent_id: int,
    talent_record: dict,
    project_records: list[tuple[int, dict]],
    save_reason_in_db: bool,
) -> dict:
    """1人材に対する案件評価結果を集約します。"""
    total_matches = 0
    same_sender_skipped = 0
    zero_score_skipped = 0
    excluded_by_rule: dict[str, int] = {}
    pending_match_rows: list[tuple[int, int, int, Optional[dict]]] = []

    for project_id, project_record in project_records:
        same_sender = _is_same_sender(talent_record, project_record)
        if same_sender:
            same_sender_skipped += 1
            continue

        total_matches += 1
        score, reason = calculate_match_score(talent_record, project_record)
        if score <= 0:
            zero_score_skipped += 1
            excluded = reason.get("excluded")
            if excluded:
                rule_name = str(excluded.get("rule"))
                excluded_by_rule[rule_name] = excluded_by_rule.get(rule_name, 0) + 1
            continue

        if not save_reason_in_db:
            reason = None

        pending_match_rows.append((talent_id, project_id, score, reason))

    return {
        "rows": pending_match_rows,
        "total_matches": total_matches,
        "same_sender_skipped": same_sender_skipped,
        "zero_score_skipped": zero_score_skipped,
        "excluded_by_rule": excluded_by_rule,
    }


def _merge_excluded_counts(total: dict[str, int], addition: dict) -> None:
    """除外ルール別の件数を加算します。"""
    for rule_name, count in (addition or {}).items():
        total[rule_name] = total.get(rule_name, 0) + int(count)


def _compute_matches_sequential(
    conn: sqlite3.Connection,
    talent_ids: list[int],
    project_ids: list[int],
    talent_log_interval: int,
    save_reason_in_db: bool,
) -> dict:
    """逐次でマッチング候補を計算します。"""
    project_records = _load_project_records(conn, project_ids)
    pending_match_rows: list[tuple[int, int, int, Optional[dict]]] = []
    total_matches = 0
    same_sender_skipped = 0
    zero_score_skipped = 0
    excluded_by_rule: dict[str, int] = {}
    processed_talent_count = 0

    normalized_log_interval = max(1, int(talent_log_interval or DEFAULT_TALENT_LOG_INTERVAL))

    for talent_id in sorted(talent_ids):
        talent_record = _load_talent_record(conn, talent_id)
        if not talent_record:
            continue

        talent_stats = _collect_match_rows_for_talent(
            talent_id,
            talent_record,
            project_records,
            save_reason_in_db=save_reason_in_db,
        )
        pending_match_rows.extend(talent_stats["rows"])
        total_matches += int(talent_stats["total_matches"])
        same_sender_skipped += int(talent_stats["same_sender_skipped"])
        zero_score_skipped += int(talent_stats["zero_score_skipped"])
        _merge_excluded_counts(excluded_by_rule, talent_stats["excluded_by_rule"])
        processed_talent_count += 1
        if processed_talent_count % normalized_log_interval == 0:
            logger.info(
                "人材マッチング処理完了: talent_id=%s, subject=%s",
                talent_id,
                _get_talent_subject(talent_record),
            )

    return {
        "rows": pending_match_rows,
        "total_matches": total_matches,
        "same_sender_skipped": same_sender_skipped,
        "zero_score_skipped": zero_score_skipped,
        "excluded_by_rule": excluded_by_rule,
    }


def _compute_matches_worker(
    db_path: str,
    talent_ids_chunk: list[int],
    project_ids: list[int],
    save_reason_in_db: bool,
) -> dict:
    """子プロセスで人材チャンクのマッチング候補を計算します。"""
    conn = sqlite3.connect(db_path)
    try:
        project_records = _load_project_records(conn, project_ids)
        pending_match_rows: list[tuple[int, int, int, Optional[dict]]] = []
        total_matches = 0
        same_sender_skipped = 0
        zero_score_skipped = 0
        excluded_by_rule: dict[str, int] = {}
        completed_talents: list[tuple[int, str]] = []

        for talent_id in talent_ids_chunk:
            talent_record = _load_talent_record(conn, talent_id)
            if not talent_record:
                continue

            talent_stats = _collect_match_rows_for_talent(
                talent_id,
                talent_record,
                project_records,
                save_reason_in_db=save_reason_in_db,
            )
            pending_match_rows.extend(talent_stats["rows"])
            total_matches += int(talent_stats["total_matches"])
            same_sender_skipped += int(talent_stats["same_sender_skipped"])
            zero_score_skipped += int(talent_stats["zero_score_skipped"])
            _merge_excluded_counts(excluded_by_rule, talent_stats["excluded_by_rule"])
            completed_talents.append((talent_id, _get_talent_subject(talent_record)))

        return {
            "rows": pending_match_rows,
            "total_matches": total_matches,
            "same_sender_skipped": same_sender_skipped,
            "zero_score_skipped": zero_score_skipped,
            "excluded_by_rule": excluded_by_rule,
            "completed_talents": completed_talents,
        }
    finally:
        conn.close()


def _resolve_db_path(conn: sqlite3.Connection, db_path: Optional[str]) -> Optional[str]:
    """並列処理で使うDBファイルパスを解決します。"""
    if db_path:
        return os.path.abspath(db_path)

    row = conn.execute("PRAGMA database_list").fetchone()
    if not row or not row[2]:
        return None
    return os.path.abspath(str(row[2]))


def _compute_matches_parallel(
    db_path: str,
    talent_ids: list[int],
    project_ids: list[int],
    num_workers: int,
    chunk_size: int,
    talent_log_interval: int,
    save_reason_in_db: bool,
) -> dict:
    """マルチプロセスでマッチング候補を計算します。"""
    talent_chunks = _chunk_values(sorted(talent_ids), chunk_size)
    if len(talent_chunks) <= 1:
        conn = sqlite3.connect(db_path)
        try:
            return _compute_matches_sequential(
                conn,
                talent_ids,
                project_ids,
                talent_log_interval=talent_log_interval,
                save_reason_in_db=save_reason_in_db,
            )
        finally:
            conn.close()

    pending_match_rows: list[tuple[int, int, int, Optional[dict]]] = []
    total_matches = 0
    same_sender_skipped = 0
    zero_score_skipped = 0
    excluded_by_rule: dict[str, int] = {}
    processed_talent_count = 0
    normalized_log_interval = max(1, int(talent_log_interval or DEFAULT_TALENT_LOG_INTERVAL))

    with ProcessPoolExecutor(max_workers=max(1, int(num_workers or 1))) as executor:
        futures = [
            executor.submit(
                _compute_matches_worker,
                db_path,
                talent_chunk,
                project_ids,
                save_reason_in_db,
            )
            for talent_chunk in talent_chunks
        ]
        for future in as_completed(futures):
            worker_stats = future.result()
            pending_match_rows.extend(worker_stats["rows"])
            total_matches += int(worker_stats["total_matches"])
            same_sender_skipped += int(worker_stats["same_sender_skipped"])
            zero_score_skipped += int(worker_stats["zero_score_skipped"])
            _merge_excluded_counts(excluded_by_rule, worker_stats["excluded_by_rule"])
            for talent_id, subject in worker_stats["completed_talents"]:
                processed_talent_count += 1
                if processed_talent_count % normalized_log_interval == 0:
                    logger.info(
                        "人材マッチング処理完了: talent_id=%s, subject=%s",
                        talent_id,
                        subject,
                    )

    return {
        "rows": pending_match_rows,
        "total_matches": total_matches,
        "same_sender_skipped": same_sender_skipped,
        "zero_score_skipped": zero_score_skipped,
        "excluded_by_rule": excluded_by_rule,
    }


def process_all_matches(
    conn: sqlite3.Connection,
    use_multiprocessing: bool = False,
    save_reason_in_db: bool = True,
    num_workers: int = 1,
    chunk_size: int = DEFAULT_MATCHING_CHUNK_SIZE,
    talent_log_interval: int = DEFAULT_TALENT_LOG_INTERVAL,
    delete_low_score_threshold: int = 50,
    db_path: Optional[str] = None,
) -> dict:
    """全人材×全案件をスキャンして、マッチング処理を実行します。

    処理完了した人材・案件レコード（status='1'）のみを対象とします。

    :param conn: SQLiteコネクション。
    :return: 処理統計 {"total_matches": int, "added": int, "low_score_deleted": int, "same_sender_skipped": int, "zero_score_skipped": int} の辞書。
    """
    # status='1'（LM処理済み）の人材・案件を取得
    talent_rows = conn.execute(
        "SELECT id FROM mails_talent WHERE status = '1' ORDER BY id"
    ).fetchall()
    project_rows = conn.execute(
        "SELECT id FROM mails_project WHERE status = '1' ORDER BY id"
    ).fetchall()
    
    talent_ids = [row[0] for row in talent_rows]
    project_ids = [row[0] for row in project_rows]
    delete_matches_for_non_active_records(conn)

    resolved_db_path = _resolve_db_path(conn, db_path)
    if use_multiprocessing and max(1, int(num_workers or 1)) > 1 and resolved_db_path:
        match_stats = _compute_matches_parallel(
            db_path=resolved_db_path,
            talent_ids=talent_ids,
            project_ids=project_ids,
            num_workers=num_workers,
            chunk_size=chunk_size,
            talent_log_interval=talent_log_interval,
            save_reason_in_db=save_reason_in_db,
        )
    else:
        match_stats = _compute_matches_sequential(
            conn,
            talent_ids,
            project_ids,
            talent_log_interval=talent_log_interval,
            save_reason_in_db=save_reason_in_db,
        )

    matches_added = add_matches_bulk(conn, match_stats["rows"])
    # 今回の計算結果に出てこない既存行（除外ルールで落ちたペア等）を削除する。
    stale_deleted = delete_matches_not_in_pairs(
        conn,
        [(row[0], row[1]) for row in match_stats["rows"] if int(row[2]) > 0],
    )
    low_score_deleted = delete_low_score_matches(conn, delete_low_score_threshold)

    # 一括保存では INSERT/UPDATE を区別せず、保存件数を added として返す。
    
    return {
        "total_matches": int(match_stats["total_matches"]),
        "added": matches_added,
        "low_score_deleted": low_score_deleted,
        "same_sender_skipped": int(match_stats["same_sender_skipped"]),
        "zero_score_skipped": int(match_stats["zero_score_skipped"]),
        "stale_deleted": stale_deleted,
        "excluded_by_rule": match_stats["excluded_by_rule"],
    }


# デバッグ用：単一のマッチペアを調査
def debug_match(conn: sqlite3.Connection, talent_id: int, project_id: int) -> None:
    """特定の人材-案件マッチングの詳細を出力します。

    :param conn: SQLiteコネクション。
    :param talent_id: 人材ID。
    :param project_id: 案件ID。
    :return: なし。
    """
    talent = _load_talent_record(conn, talent_id)
    project_record = get_project_record(conn, project_id)
    project = ground_record(project_record, PROJECT_TECH_FIELDS, _load_skill_aliases()) if project_record else {}

    if not talent or not project:
        print(f"Error: Talent ID {talent_id} or Project ID {project_id} not found")
        return
    
    score, reason = calculate_match_score(talent, project)
    
    print(f"\n=== Debug Match: Talent {talent_id} vs Project {project_id} ===")
    print(f"Total Score: {score}/100")
    print(f"\n人材: {talent.get('名前', 'N/A')} ({talent.get('所属', 'N/A')})")
    print(f"案件: {project.get('案件名', 'N/A')} ({project.get('業種', 'N/A')})")
    print(f"\nReason JSON:\n{json.dumps(reason, indent=2, ensure_ascii=False)}")
