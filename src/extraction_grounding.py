"""LLM の抽出結果のうち、件名・本文に根拠の無い値を取り除きます。

小さいモデルは、フォーマットの説明文にある例（Java, MySQL など）を書き写したり、
選択肢（工程・担当）を全部選んだりすることがある。実データでは案件の工程の75%が6つ全部、
データベースの値の約半数が本文に無い値だった。これらがスキル・担当・工程の一致を水増しするため、
マッチングの前に、件名・本文に出てくる値だけを残す。
"""
import json
import re
import unicodedata
from typing import Any


# 技術名・スキル名の配列項目（値そのものが件名・本文に出てくるかで確かめる）
TALENT_TECH_FIELDS = ("開発言語", "OS/クラウド", "データベース", "スキル", "資格")
PROJECT_TECH_FIELDS = ("必須スキル", "尚可スキル", "開発言語", "データベース", "OS/クラウド")

# 工程・担当は選択肢から選ばせているため、本文での言い換えも根拠として認める
_PHASE_EVIDENCE = {
    "要件定義": r"要件|要求定義",
    "基本設計": r"基本設計|外部設計|方式設計|概要設計|設計",
    "詳細設計": r"詳細設計|内部設計|設計",
    "開発": r"開発|製造|実装|コーディング|プログラミング|構築",
    "テスト": r"テスト|試験|検証|打鍵",
    "運用保守": r"運用|保守|監視",
}
# 英字の略語は単語として探す（「SES」の SE、「PMO」の PM、「PL/SQL」の PL を拾わない）
_ROLE_EVIDENCE = {
    "PM": r"(?<![a-z])pm(?![a-z])|プロジェクトマネ|プロマネ",
    "PMO": r"(?<![a-z])pmo(?![a-z])",
    "リーダー": r"リーダー|(?<![a-z])pl(?![a-z/])|リード|leader",
    "SE": r"(?<![a-z])se(?![a-z])|システムエンジニア|設計",
    "PG": r"(?<![a-z])pg(?![a-z])|プログラマ|製造|実装|コーディング",
    "テスト": r"テスト|テスター|試験|検証",
    "メンバー": r"メンバー",
}
_PHASE_PATTERNS = {name: re.compile(pattern) for name, pattern in _PHASE_EVIDENCE.items()}
_ROLE_PATTERNS = {name: re.compile(pattern) for name, pattern in _ROLE_EVIDENCE.items()}

_ASCII_TERM_CHARS = r"a-z0-9+#"


def _normalize_text(value: Any) -> str:
    """全角半角を統一して小文字にします。"""
    return unicodedata.normalize("NFKC", str(value or "")).lower()


def _compact(text: str) -> str:
    """空白を除きます（「SQL Server」と「SQLServer」を同じとみなすため）。"""
    return re.sub(r"\s+", "", text)


def _parse_values(field: Any) -> list[str] | None:
    """配列項目（list / JSON配列文字列）を文字列のリストにします。配列でなければ None。"""
    if isinstance(field, list):
        return [str(value).strip() for value in field if str(value).strip()]
    if not isinstance(field, str) or not field.strip().startswith("["):
        return None
    try:
        parsed = json.loads(field)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list):
        return None
    return [str(value).strip() for value in parsed if str(value).strip()]


def _term_appears(term: str, compact_text: str, aliases: dict[str, str]) -> bool:
    """技術名が本文に出てくるか判定します。

    英数字だけの語は単語として探す（「C」が「C#」に、「Java」が「JavaScript」に一致しないように）。
    別名表（skill_aliases.csv）で同じ技術とされる別名が出てくる場合も一致とする。
    """
    normalized = _normalize_text(term).strip()
    if not normalized:
        return False
    canonical = aliases.get(normalized, normalized)
    candidates = {normalized, canonical}
    candidates.update(alias for alias, target in aliases.items() if target == canonical)
    for candidate in candidates:
        compact_candidate = _compact(candidate)
        if not compact_candidate:
            continue
        if re.fullmatch(rf"[{_ASCII_TERM_CHARS}.\-]+", compact_candidate):
            pattern = rf"(?<![{_ASCII_TERM_CHARS}]){re.escape(compact_candidate)}(?![{_ASCII_TERM_CHARS}])"
            if re.search(pattern, compact_text):
                return True
        elif compact_candidate in compact_text:
            return True
    return False


def _ground_choices(values: list[str], patterns: dict[str, re.Pattern[str]], text: str) -> list[str]:
    """工程・担当の値を、本文に根拠（言い換えを含む）があるものだけにします。"""
    grounded = []
    for value in values:
        pattern = patterns.get(value)
        # 選択肢に無い値は判断できないため残す
        if pattern is None or pattern.search(text):
            grounded.append(value)
    return grounded


def ground_record(record: dict[str, Any], tech_fields: tuple[str, ...], aliases: dict[str, str]) -> dict[str, Any]:
    """件名・本文に根拠の無い技術名・工程・担当を取り除いた新しいレコードを返します。

    件名・本文が無い場合は確かめられないため、そのまま返す。

    :param record: 人材または案件のレコード。
    :param tech_fields: 技術名として確かめる配列項目。
    :param aliases: 技術名の別名表（別名 → 正式名。小文字・全角半角統一済み）。
    :return: 根拠の無い値を除いたレコード。
    """
    text = _normalize_text(record.get("subject")) + "\n" + _normalize_text(record.get("body"))
    if not text.strip():
        return record

    compact_text = _compact(text)
    grounded = dict(record)
    for field in tech_fields:
        values = _parse_values(record.get(field))
        if values is None:
            continue
        grounded[field] = [value for value in values if _term_appears(value, compact_text, aliases)]
    for field, patterns in (("工程", _PHASE_PATTERNS), ("担当", _ROLE_PATTERNS)):
        values = _parse_values(record.get(field))
        if values is None:
            continue
        grounded[field] = _ground_choices(values, patterns, text)
    return grounded
