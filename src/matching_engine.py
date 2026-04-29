import json
import sqlite3
from typing import Optional, Tuple
from .database_utils import (
    get_talent_record,
    get_project_record,
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


def _match_salary(talent_price: Optional[str], project_price: Optional[str]) -> Tuple[bool, int, dict]:
    """給与マッチング判定を実施します。

    :param talent_price: 人材の希望単価。
    :param project_price: 案件の単価。
    :return: (マッチ判定, スコア加点, 詳細情報) の3要素タプル。
    """
    talent_min, talent_max = _parse_price_range(talent_price)
    project_min, project_max = _parse_price_range(project_price)
    
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
    talent_foreign = talent_record.get("外国籍", "").strip().lower()
    project_foreign_ok = project_record.get("外国籍可否", "").strip().lower()
    
    if project_foreign_ok and project_foreign_ok != "":
        # 案件が外国籍の可否を指定している場合
        if talent_foreign in ("true", "true", "1", "はい", "可"):
            # 人材が外国籍の場合、案件が「外国籍可否: true/可」である必要
            if project_foreign_ok not in ("true", "yes", "可", "1", "ok"):
                constraints_ok = False
            details["foreign"] = "talent_is_foreign_but_project_not_ok"
        else:
            details["foreign"] = "talent_domestic_ok"
    else:
        details["foreign"] = "project_no_restriction"
    
    # 個人事業主チェック
    talent_freelance = talent_record.get("個人事業主", "").strip().lower()
    project_freelance_ok = project_record.get("個人事業主可否", "").strip().lower()
    
    if project_freelance_ok and project_freelance_ok != "":
        if talent_freelance in ("true", "1", "はい", "可"):
            if project_freelance_ok not in ("true", "yes", "可", "1", "ok"):
                constraints_ok = False
            details["freelance"] = "talent_is_freelance_but_project_not_ok"
        else:
            details["freelance"] = "talent_employee_ok"
    else:
        details["freelance"] = "project_no_restriction"

    # 商流制限_貴社所属迄チェック
    # True の場合、人材の所属が弊社直接雇用（「弊社」「直」を含む）である必要がある
    project_direct_only = project_record.get("商流制限_貴社所属迄", False)
    if project_direct_only:
        talent_belonging = talent_record.get("所属", "").strip()
        is_direct = any(kw in talent_belonging for kw in ["弊社", "直属", "直雇", "直接"])
        if not is_direct:
            constraints_ok = False
        details["direct_only"] = "match" if is_direct else "talent_not_direct"
    else:
        details["direct_only"] = "no_restriction"

    # 派遣案件と1社下所属の不適合チェック
    # 派遣案件で人材が「1社下所属」の場合は NG
    contract_type = project_record.get("契約形態", "").strip()
    talent_belonging = talent_record.get("所属", "").strip()
    
    if contract_type == "派遣" and "1社下" in talent_belonging:
        constraints_ok = False
        details["dispatch_secondment"] = "ng_dispatch_with_secondment"
    else:
        details["dispatch_secondment"] = "ok" if contract_type == "派遣" else "not_dispatch"

    constraint_score = 25 if constraints_ok else 0
    
    return (constraints_ok, constraint_score, details)


def _match_location_remote(talent_record: dict, project_record: dict) -> Tuple[bool, int, dict]:
    """地域・リモートマッチング判定を実施します。

    :param talent_record: 人材レコード辞書。
    :param project_record: 案件レコード辞書。
    :return: (マッチ判定, スコア加点, 詳細情報) の3要素タプル。
    """
    location_ok = True
    details = {}
    
    # リモート頻度チェック
    talent_remote = talent_record.get("リモート頻度", "").strip()
    project_remote = project_record.get("リモート頻度", "").strip()
    
    remote_score = 0
    if talent_remote and project_remote:
        # 両者の値が同じか、人材が「フルリモート」なら OK
        if talent_remote == project_remote or talent_remote == "フルリモート":
            remote_score = 13  # リモート頻度が合致で 13 点（オプショナル）
            details["remote"] = "match"
        else:
            details["remote"] = "no_match"
    else:
        details["remote"] = "no_data"
    
    # 最寄駅 / 作業場所チェック（単純な存在確認）
    talent_station = talent_record.get("最寄駅", "").strip()
    project_location = project_record.get("作業場所", "").strip()
    
    location_score = 0
    if talent_station and project_location:
        # 近いかどうかの判定は正確な緯度経度マッピングが必要なので、
        # ここでは両方存在するだけで OK とする
        location_score = 12  # 位置情報が存在で 12 点
        details["location"] = "both_exist"
    elif not project_location:
        # リモート案件の可能性が高い
        location_score = 12
        details["location"] = "project_no_fixed_location"
    else:
        details["location"] = "uncertain"
    
    location_ok = True  # 地域・リモートは「条件をはずす」判定ではなく、スコアのみ影響
    
    return (location_ok, remote_score + location_score, details)


def _parse_talent_age(age_val) -> Optional[int]:
    """人材の年齢値を整数に変換します。

    :param age_val: 年齢（int または str）。
    :return: 年齢整数。変換不可なら None。
    """
    if age_val is None:
        return None
    if isinstance(age_val, int):
        return age_val
    import re
    s = str(age_val).strip()
    # 例: "35" / "35歳"
    m = re.match(r'^(\d+)', s)
    if m:
        return int(m.group(1))
    # 例: "30代前半" -> 32 / "30代後半" -> 37 / "30代" -> 35
    m = re.match(r'^(\d+)代(前半|後半)?', s)
    if m:
        base = int(m.group(1))
        suffix = m.group(2)
        if suffix == '前半':
            return base + 2
        if suffix == '後半':
            return base + 7
        return base + 5
    return None


def _parse_project_age_limit(age_str: Optional[str]) -> Optional[int]:
    """案件の年齢制限文字列を数値上限に変換します。

    :param age_str: 年齢文字列（例: 「〜45歳」「45歳まで」「制限なし」）。
    :return: 上限年齢（制限なしまたは未記載は None）。
    """
    if not age_str:
        return None
    s = str(age_str).strip()
    if s in ('制限なし', '不問', '不明', ''):
        return None
    import re
    # 例: 「〜45歳」「45歳まで」「45歳以下」「45迄」「~45」
    m = re.search(r'(\d+)\s*(?:歳|迄|まで|以下)?', s)
    if m:
        return int(m.group(1))
    return None


def _match_age(talent_record: dict, project_record: dict) -> Tuple[bool, int, dict]:
    """年齢マッチング判定を実施します（ソフト制約）。

    :param talent_record: 人材レコード辞書。
    :param project_record: 案件レコード辞書。
    :return: (True, スコア, 詳細) の3要素タプル。
    """
    talent_age = _parse_talent_age(talent_record.get('年齢'))
    project_limit = _parse_project_age_limit(project_record.get('年齢'))

    # 制限なしまたは人材の年齢不明の場合はデータなしとしてスキップ
    if project_limit is None or talent_age is None:
        return (True, 0, {'status': 'unknown_age'})

    within = talent_age <= project_limit
    score = 15 if within else 0
    return (
        True,  # ソフト制約なので常に True
        score,
        {
            'talent_age': talent_age,
            'project_limit': project_limit,
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
        project_record.get("単価"),
    )
    total_score += salary_score
    reason["salary"] = {
        "match": salary_match,
        "score": salary_score,
        "details": salary_details,
    }
    
    # 3. 立場制約チェック
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
    
    # 4. 地域・リモート
    location_match, location_score, location_details = _match_location_remote(
        talent_record,
        project_record,
    )
    total_score += location_score
    reason["location_remote"] = {
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
    talent_rows = conn.execute(
        "SELECT id FROM mails_talent WHERE status = '1' ORDER BY id"
    ).fetchall()
    project_rows = conn.execute(
        "SELECT id FROM mails_project WHERE status = '1' ORDER BY id"
    ).fetchall()
    
    talent_ids = [row[0] for row in talent_rows]
    project_ids = [row[0] for row in project_rows]
    
    total_matches = 0
    matches_added = 0
    matches_updated = 0
    
    # 全組み合わせをスキャン
    for talent_id in talent_ids:
        talent_record = get_talent_record(conn, talent_id)
        if not talent_record:
            continue
        
        for project_id in project_ids:
            project_record = get_project_record(conn, project_id)
            if not project_record:
                continue
            
            total_matches += 1
            
            # マッチングスコアを計算
            score, reason = calculate_match_score(talent_record, project_record)
            
            # マッチング結果を保存
            existing = check_existing_match(conn, talent_id, project_id)
            add_match(conn, talent_id, project_id, score, reason)
            
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
