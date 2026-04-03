import json
import sqlite3
from typing import Optional, Tuple
from database_utils import (
    get_human_record,
    get_case_record,
    add_match,
    check_existing_match,
)


def _parse_json_array(field: Optional[str]) -> list:
    """JSON配列文字列をPython listに変換します。

    :param field: JSON配列文字列（例: "[\"Java\", \"Python\"]"）。
    :return: 変換後のリスト。パースに失敗した場合は空リスト。
    """
    if not field:
        return []
    
    try:
        result = json.loads(field)
        if isinstance(result, list):
            return result
    except (json.JSONDecodeError, TypeError):
        pass
    
    return []


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
    human_skills_dict: dict,
    case_required_skills: list,
    case_preferred_skills: list,
) -> Tuple[bool, int, dict]:
    """スキルマッチング判定を実施します。

    :param human_skills_dict: 人材スキル情報（"開発言語", "OS/クラウド", "データベース", "スキル", "資格"キーを持つ辞書）。
    :param case_required_skills: 案件の必須スキル（リスト）。
    :param case_preferred_skills: 案件の尚可スキル（リスト）。
    :return: (マッチ判定, スコア加点, 詳細情報) の3要素タプル。
    """
    # 人材の全スキル情報を集約
    human_all_skills = set()
    
    for key in ["開発言語", "OS/クラウド", "データベース", "スキル", "資格"]:
        skills_array = _parse_json_array(human_skills_dict.get(key))
        for skill in skills_array:
            human_all_skills.add(_normalize_skill(skill))
    
    # 必須スキルを正規化
    normalized_required = [_normalize_skill(s) for s in case_required_skills]
    normalized_preferred = [_normalize_skill(s) for s in case_preferred_skills]
    
    # 必須スキルの部分一致判定
    required_matches = 0
    for req_skill in normalized_required:
        if req_skill in human_all_skills:
            required_matches += 1
    
    # 必須スキルが1個以上マッチで OK
    required_ok = required_matches > 0 if normalized_required else True
    
    # 尚可スキルのマッチ数をカウント
    preferred_matches = 0
    for pref_skill in normalized_preferred:
        if pref_skill in human_all_skills:
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


def _match_salary(human_price: Optional[str], case_price: Optional[str]) -> Tuple[bool, int, dict]:
    """給与マッチング判定を実施します。

    :param human_price: 人材の希望単価。
    :param case_price: 案件の単価。
    :return: (マッチ判定, スコア加点, 詳細情報) の3要素タプル。
    """
    human_min, human_max = _parse_price_range(human_price)
    case_min, case_max = _parse_price_range(case_price)
    
    # どちらかが parse に失敗したら、スキップ（条件なし）
    if human_min is None or case_min is None:
        return (True, 0, {"status": "unknown_price"})
    
    # 案件予算が人材要望内かチェック
    # 人材の要望 min≦ 案件 max かつ 人材の要望 max≧ 案件 min なら重複範囲あり
    match = human_min <= case_max and human_max >= case_min
    
    salary_score = 25 if match else 0
    
    details = {
        "match": match,
        "human_range": (human_min, human_max),
        "case_range": (case_min, case_max),
    }
    
    return (match, salary_score, details)


def _match_constraints(human_record: dict, case_record: dict) -> Tuple[bool, int, dict]:
    """立場制約マッチング判定を実施します（外国籍、個人事業主等）。

    :param human_record: 人材レコード辞書。
    :param case_record: 案件レコード辞書。
    :return: (マッチ判定, スコア加点, 詳細情報) の3要素タプル。
    """
    constraints_ok = True
    details = {}
    
    # 外国籍チェック
    human_foreign = human_record.get("外国籍", "").strip().lower()
    case_foreign_ok = case_record.get("外国籍可否", "").strip().lower()
    
    if case_foreign_ok and case_foreign_ok != "":
        # 案件が外国籍の可否を指定している場合
        if human_foreign in ("true", "true", "1", "はい", "可"):
            # 人材が外国籍の場合、案件が「外国籍可否: true/可」である必要
            if case_foreign_ok not in ("true", "yes", "可", "1", "ok"):
                constraints_ok = False
            details["foreign"] = "human_is_foreign_but_case_not_ok"
        else:
            details["foreign"] = "human_domestic_ok"
    else:
        details["foreign"] = "case_no_restriction"
    
    # 個人事業主チェック
    human_freelance = human_record.get("個人事業主", "").strip().lower()
    case_freelance_ok = case_record.get("個人事業主可否", "").strip().lower()
    
    if case_freelance_ok and case_freelance_ok != "":
        if human_freelance in ("true", "1", "はい", "可"):
            if case_freelance_ok not in ("true", "yes", "可", "1", "ok"):
                constraints_ok = False
            details["freelance"] = "human_is_freelance_but_case_not_ok"
        else:
            details["freelance"] = "human_employee_ok"
    else:
        details["freelance"] = "case_no_restriction"
    
    constraint_score = 25 if constraints_ok else 0
    
    return (constraints_ok, constraint_score, details)


def _match_location_remote(human_record: dict, case_record: dict) -> Tuple[bool, int, dict]:
    """地域・リモートマッチング判定を実施します。

    :param human_record: 人材レコード辞書。
    :param case_record: 案件レコード辞書。
    :return: (マッチ判定, スコア加点, 詳細情報) の3要素タプル。
    """
    location_ok = True
    details = {}
    
    # リモート頻度チェック
    human_remote = human_record.get("リモート頻度", "").strip()
    case_remote = case_record.get("リモート頻度", "").strip()
    
    remote_score = 0
    if human_remote and case_remote:
        # 両者の値が同じか、人材が「フルリモート」なら OK
        if human_remote == case_remote or human_remote == "フルリモート":
            remote_score = 13  # リモート頻度が合致で 13 点（オプショナル）
            details["remote"] = "match"
        else:
            details["remote"] = "no_match"
    else:
        details["remote"] = "no_data"
    
    # 最寄駅 / 作業場所チェック（単純な存在確認）
    human_station = human_record.get("最寄駅", "").strip()
    case_location = case_record.get("作業場所", "").strip()
    
    location_score = 0
    if human_station and case_location:
        # 近いかどうかの判定は正確な緯度経度マッピングが必要なので、
        # ここでは両方存在するだけで OK とする
        location_score = 12  # 位置情報が存在で 12 点
        details["location"] = "both_exist"
    elif not case_location:
        # リモート案件の可能性が高い
        location_score = 12
        details["location"] = "case_no_fixed_location"
    else:
        details["location"] = "uncertain"
    
    location_ok = True  # 地域・リモートは「条件をはずす」判定ではなく、スコアのみ影響
    
    return (location_ok, remote_score + location_score, details)


def calculate_match_score(human_record: dict, case_record: dict) -> Tuple[int, dict]:
    """人材と案件のマッチングスコアを計算します。

    :param human_record: 人材レコード辞書。
    :param case_record: 案件レコード辞書。
    :return: (総合スコア 0-100, 詳細理由 JSON) のタプル。
    """
    total_score = 0
    reason = {}
    
    # 1. スキルマッチング
    case_required = _parse_json_array(case_record.get("必須スキル"))
    case_preferred = _parse_json_array(case_record.get("尚可スキル"))
    
    skill_match, skill_score, skill_details = _match_skills(
        {
            "開発言語": human_record.get("開発言語"),
            "OS/クラウド": human_record.get("OS/クラウド"),
            "データベース": human_record.get("データベース"),
            "スキル": human_record.get("スキル"),
            "資格": human_record.get("資格"),
        },
        case_required,
        case_preferred,
    )
    total_score += skill_score
    reason["skills"] = {
        "match": skill_match,
        "score": skill_score,
        "details": skill_details,
    }
    
    # 2. 給与マッチング
    salary_match, salary_score, salary_details = _match_salary(
        human_record.get("希望単価"),
        case_record.get("単価"),
    )
    total_score += salary_score
    reason["salary"] = {
        "match": salary_match,
        "score": salary_score,
        "details": salary_details,
    }
    
    # 3. 立場制約チェック
    constraint_match, constraint_score, constraint_details = _match_constraints(
        human_record,
        case_record,
    )
    total_score += constraint_score
    reason["constraints"] = {
        "match": constraint_match,
        "score": constraint_score,
        "details": constraint_details,
    }
    
    # 4. 地域・リモート
    location_match, location_score, location_details = _match_location_remote(
        human_record,
        case_record,
    )
    total_score += location_score
    reason["location_remote"] = {
        "match": location_match,
        "score": location_score,
        "details": location_details,
    }
    
    # スコアを 0-100 に正規化（最大 100 を超えないように）
    total_score = min(100, total_score)
    
    return (total_score, reason)


def process_all_matches(conn: sqlite3.Connection) -> dict:
    """全人材×全案件をスキャンして、マッチング処理を実行します。

    処理完了した人材・案件レコード（status='1'）のみを対象とします。

    :param conn: SQLiteコネクション。
    :return: 処理統計 {"total_matches": int, "added": int, "updated": int} の辞書。
    """
    # status='1'（LM処理済み）の人材・案件を取得
    human_rows = conn.execute(
        "SELECT id FROM mails_human WHERE status = '1' ORDER BY id"
    ).fetchall()
    case_rows = conn.execute(
        "SELECT id FROM mails_case WHERE status = '1' ORDER BY id"
    ).fetchall()
    
    human_ids = [row[0] for row in human_rows]
    case_ids = [row[0] for row in case_rows]
    
    total_matches = 0
    matches_added = 0
    matches_updated = 0
    
    # 全組み合わせをスキャン
    for human_id in human_ids:
        human_record = get_human_record(conn, human_id)
        if not human_record:
            continue
        
        for case_id in case_ids:
            case_record = get_case_record(conn, case_id)
            if not case_record:
                continue
            
            total_matches += 1
            
            # マッチングスコアを計算
            score, reason = calculate_match_score(human_record, case_record)
            
            # マッチング結果を保存
            existing = check_existing_match(conn, human_id, case_id)
            add_match(conn, human_id, case_id, score, reason)
            
            if existing:
                matches_updated += 1
            else:
                matches_added += 1
    
    return {
        "total_matches": total_matches,
        "added": matches_added,
        "updated": matches_updated,
    }


# デバッグ用：単一のマッチペアを調査
def debug_match(conn: sqlite3.Connection, human_id: int, case_id: int) -> None:
    """特定の人材-案件マッチングの詳細を出力します。

    :param conn: SQLiteコネクション。
    :param human_id: 人材ID。
    :param case_id: 案件ID。
    :return: なし。
    """
    human = get_human_record(conn, human_id)
    case = get_case_record(conn, case_id)
    
    if not human or not case:
        print(f"Error: Human ID {human_id} or Case ID {case_id} not found")
        return
    
    score, reason = calculate_match_score(human, case)
    
    print(f"\n=== Debug Match: Human {human_id} vs Case {case_id} ===")
    print(f"Total Score: {score}/100")
    print(f"\n人材: {human.get('名前', 'N/A')} ({human.get('所属', 'N/A')})")
    print(f"案件: {case.get('案件名', 'N/A')} ({case.get('業種', 'N/A')})")
    print(f"\nReason JSON:\n{json.dumps(reason, indent=2, ensure_ascii=False)}")
