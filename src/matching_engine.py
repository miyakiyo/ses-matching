from concurrent.futures import ProcessPoolExecutor, as_completed
import json
import os
import sqlite3
import re
import logging
from typing import Optional, Tuple
from .database_utils import (
    get_talent_record,
    get_project_record,
    add_matches_bulk,
    delete_matches_for_non_active_records,
    delete_low_score_matches,
)


logger = logging.getLogger(__name__)

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


def _normalize_skill(skill: str) -> str:
    """スキル名を正規化します（小文字化、前後の空白削除）。

    :param skill: スキル名。
    :return: 正規化されたスキル名。
    """
    return skill.strip().lower() if skill else ""


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


def _match_skills(
    talent_skills_dict: dict,
    project_required_skills: list,
    project_preferred_skills: list,
) -> Tuple[bool, int, dict]:
    """スキルマッチング判定を実施します。

    :param talent_skills_dict: 人材スキル情報（"開発言語", "OS/クラウド", "データベース", "スキル", "資格"キーを持つ辞書）。
    :param project_required_skills: 案件の必須スキル（リスト）。
    :param project_preferred_skills: 案件の尚可スキル（リスト）。
    :return: (マッチ判定, スコア加点, 詳細情報) の3要素タプル。
    """
    # 人材の全スキル情報を集約
    talent_all_skills = set()
    
    for key in ["開発言語", "OS/クラウド", "データベース", "スキル", "資格"]:
        skills_array = _parse_json_array(talent_skills_dict.get(key))
        for skill in skills_array:
            talent_all_skills.add(_normalize_skill(skill))
    
    # 必須スキルを正規化
    normalized_required = [_normalize_skill(s) for s in project_required_skills]
    normalized_preferred = [_normalize_skill(s) for s in project_preferred_skills]
    
    # 必須スキルの部分一致判定
    required_matches = 0
    for req_skill in normalized_required:
        if req_skill in talent_all_skills:
            required_matches += 1
    
    # 必須スキルが1個以上マッチで OK
    required_ok = required_matches > 0 if normalized_required else True
    
    # 尚可スキルのマッチ数をカウント
    preferred_matches = 0
    for pref_skill in normalized_preferred:
        if pref_skill in talent_all_skills:
            preferred_matches += 1
    
    # スコア計算：必須スキル一致などで加点
    skill_score = 0
    if required_ok:
        skill_score += 50  # 必須スキル条件クリアで 50 点
    if preferred_matches > 0:
        skill_score += min(25, preferred_matches * 5)  # 尚可スキル マッチで最大 25 点
    
    details = {
        "required_match": required_ok,
        "required_matches": required_matches,
        "required_count": len(normalized_required),
        "preferred_matches": preferred_matches,
        "preferred_count": len(normalized_preferred),
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


def _parse_project_price_range(project_record: dict) -> Tuple[Optional[float], Optional[float]]:
    """案件レコードから単価下限/単価上限を取得します。"""
    project_min = _parse_optional_number(project_record.get("単価下限"))
    project_max = _parse_optional_number(project_record.get("単価上限"))

    if project_min is None and project_max is not None:
        project_min = project_max
    if project_max is None and project_min is not None:
        project_max = project_min

    return (project_min, project_max)


def _parse_talent_price_range(talent_price) -> Tuple[Optional[float], Optional[float]]:
    """人材の希望単価（単一数値）を min/max レンジへ正規化します。"""
    single_value = _parse_optional_number(talent_price)
    if single_value is not None:
        return (single_value, single_value)

    # 互換性のため、旧データの範囲文字列はフォールバックで解釈する。
    return _parse_price_range(talent_price)


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

    details = {
        "role": {
            "status": role_status,
            "talent_values": talent_roles,
            "project_values": project_roles,
            "overlap": role_overlap,
        },
        "phase": {
            "status": phase_status,
            "talent_values": talent_phases,
            "project_values": project_phases,
            "overlap": phase_overlap,
        },
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

    # 制限なしまたは人材の年齢不明の場合はデータなしとしてスキップ
    if (
        (talent_age_min is None and talent_age_max is None)
        or (project_age_min is None and project_age_max is None)
    ):
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


def calculate_match_score(talent_record: dict, project_record: dict) -> Tuple[int, dict]:
    """人材と案件のマッチングスコアを計算します。

    :param talent_record: 人材レコード辞書。
    :param project_record: 案件レコード辞書。
    :return: (総合スコア 0-100, 詳細理由 JSON) のタプル。
    """
    total_score = 0
    reason = {}
    
    # 1. スキルマッチング
    project_required = _parse_json_array(project_record.get("必須スキル"))
    # 案件の開発言語・データベース・OS/クラウドも必須スキルに加える
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
    
    # スコアを 0-100 に正規化（最大 100 を超えないように）
    total_score = min(100, total_score)
    
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
    """案件レコードを一度だけ読み込み、評価用に保持します。"""
    project_records: list[tuple[int, dict]] = []
    for project_id in sorted(project_ids):
        project_record = get_project_record(conn, project_id)
        if project_record:
            project_records.append((project_id, project_record))
    return project_records


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
            continue

        if not save_reason_in_db:
            reason = None

        pending_match_rows.append((talent_id, project_id, score, reason))

    return {
        "rows": pending_match_rows,
        "total_matches": total_matches,
        "same_sender_skipped": same_sender_skipped,
        "zero_score_skipped": zero_score_skipped,
    }


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
    processed_talent_count = 0

    normalized_log_interval = max(1, int(talent_log_interval or DEFAULT_TALENT_LOG_INTERVAL))

    for talent_id in sorted(talent_ids):
        talent_record = get_talent_record(conn, talent_id)
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
        completed_talents: list[tuple[int, str]] = []

        for talent_id in talent_ids_chunk:
            talent_record = get_talent_record(conn, talent_id)
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
            completed_talents.append((talent_id, _get_talent_subject(talent_record)))

        return {
            "rows": pending_match_rows,
            "total_matches": total_matches,
            "same_sender_skipped": same_sender_skipped,
            "zero_score_skipped": zero_score_skipped,
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
    low_score_deleted = delete_low_score_matches(conn, delete_low_score_threshold)

    # 一括保存では INSERT/UPDATE を区別せず、保存件数を added として返す。
    
    return {
        "total_matches": int(match_stats["total_matches"]),
        "added": matches_added,
        "low_score_deleted": low_score_deleted,
        "same_sender_skipped": int(match_stats["same_sender_skipped"]),
        "zero_score_skipped": int(match_stats["zero_score_skipped"]),
    }


# デバッグ用：単一のマッチペアを調査
def debug_match(conn: sqlite3.Connection, talent_id: int, project_id: int) -> None:
    """特定の人材-案件マッチングの詳細を出力します。

    :param conn: SQLiteコネクション。
    :param talent_id: 人材ID。
    :param project_id: 案件ID。
    :return: なし。
    """
    talent = get_talent_record(conn, talent_id)
    project = get_project_record(conn, project_id)
    
    if not talent or not project:
        print(f"Error: Talent ID {talent_id} or Project ID {project_id} not found")
        return
    
    score, reason = calculate_match_score(talent, project)
    
    print(f"\n=== Debug Match: Talent {talent_id} vs Project {project_id} ===")
    print(f"Total Score: {score}/100")
    print(f"\n人材: {talent.get('名前', 'N/A')} ({talent.get('所属', 'N/A')})")
    print(f"案件: {project.get('案件名', 'N/A')} ({project.get('業種', 'N/A')})")
    print(f"\nReason JSON:\n{json.dumps(reason, indent=2, ensure_ascii=False)}")
