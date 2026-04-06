import json
import logging
from pathlib import Path
from typing import Any

import requests
import sqlite3

from database_utils import get_pending_records, update_record_json_status_and_properties
from matching_engine import process_all_matches


logger = logging.getLogger(__name__)


def _load_case_schema_text() -> str:
    """案件JSONスキーマ文字列を読み込みます。"""
    schema_path = Path(__file__).with_name("案件フォーマット.json")
    if not schema_path.exists():
        raise FileNotFoundError(f"案件スキーマが見つかりません: {schema_path}")
    return schema_path.read_text(encoding="utf-8")


def _load_human_schema_text() -> str:
    """人材JSONスキーマ文字列を読み込みます。"""
    schema_path = Path(__file__).with_name("人材フォーマット.json")
    if not schema_path.exists():
        raise FileNotFoundError(f"人材スキーマが見つかりません: {schema_path}")
    return schema_path.read_text(encoding="utf-8")


def _call_lmstudio(
    endpoint: str,
    model: str,
    body_text: str,
    category: str,
    timeout: int = 60,
) -> str:
    """LM Studio に本文を送り、JSON文字列を返します。"""
    if category == "案件":
        case_schema_text = _load_case_schema_text()
        system_content = (
            "あなたはIT/SES営業メールの案件から情報を抽出するシステムです\n"
            "必ずJSONスキーマに従って出力してください。\n"
            "以下のJSONスキーマに従う\n"
            f"{case_schema_text}"
        )
    elif category == "人材":
        human_schema_text = _load_human_schema_text()
        system_content = (
            "あなたはIT/SES営業メールの人材から情報を抽出するシステムです\n"
            "必ずJSONスキーマに従って出力してください。\n"
            "以下のJSONスキーマに従う\n"
            f"{human_schema_text}"
        )
    else:
        system_content = "You are a JSON generator. Return JSON only."

    prompt = (
        f"以下は{category}メール本文です。解析して必ずJSONのみを返してください。"
        "未記載の項目は必ず null または [] にする。\n"
        "日本語で作成する。\n"
        "説明文やコードブロックは不要です。\n\n"
        f"本文:\n{body_text}"
    )

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
    }
    resp = requests.post(endpoint, json=payload, timeout=timeout)
    resp.raise_for_status()

    data = resp.json()
    content = (
        data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
        .strip()
    )
    if not content:
        raise RuntimeError("LM Studio response content is empty")

    # モデル応答がJSON文字列として妥当かを検証する。
    parsed = json.loads(content)
    return json.dumps(parsed, ensure_ascii=False)


def process_pending_records_with_lmstudio(
    conn: sqlite3.Connection,
    endpoint: str,
    model: str,
    timeout: int = 60,
    limit_per_table: int = 500,
    exclude_folders: list[str] | None = None,
) -> tuple[int, int]:
    """status='0' のレコードを LM Studio でJSON化して保存します。"""
    total_success = 0
    total_error = 0
    normalized_excludes = [
        str(folder_name).strip()
        for folder_name in (exclude_folders or [])
        if str(folder_name).strip()
    ]

    for table_name in ("mails_human", "mails_case"):
        records = get_pending_records(
            conn,
            table_name,
            limit=limit_per_table,
            exclude_folders=normalized_excludes,
        )
        category = "人材" if table_name == "mails_human" else "案件"
        logger.info(
            f"LM後処理開始: table={table_name}, pending={len(records)}, excluded_folders={normalized_excludes}"
        )

        for record_id, body_text in records:
            try:
                json_text = _call_lmstudio(
                    endpoint=endpoint,
                    model=model,
                    body_text=body_text,
                    category=category,
                    timeout=timeout,
                )
                parsed = json.loads(json_text)
                if isinstance(parsed, list):
                    first_obj = parsed[0] if parsed else {}
                elif isinstance(parsed, dict):
                    first_obj = parsed
                else:
                    first_obj = {}

                update_record_json_status_and_properties(
                    conn=conn,
                    table_name=table_name,
                    record_id=record_id,
                    json_data=json_text,
                    properties=first_obj,
                    status="1",
                )
                total_success += 1
            except Exception as e:
                total_error += 1
                logger.error(
                    f"LM後処理失敗: table={table_name}, id={record_id}, error={e}"
                )

        conn.commit()

    logger.info(f"LM後処理完了: success={total_success}, error={total_error}")
    
    # LM処理完了後、マッチング処理を実行
    logger.info("マッチング処理開始...")
    try:
        match_stats = process_all_matches(conn)
        logger.info(
            f"マッチング処理完了: total={match_stats['total_matches']}, "
            f"added={match_stats['added']}, updated={match_stats['updated']}"
        )
    except Exception as e:
        logger.error(f"マッチング処理失敗: {e}")
    
    return total_success, total_error
