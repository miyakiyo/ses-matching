import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests
import sqlite3

from .database_utils import get_pending_records, update_record_json_status_and_properties
from .matching_engine import process_all_matches


logger = logging.getLogger(__name__)


def _load_case_schema_text() -> str:
    """案件JSONスキーマ文字列を読み込みます。"""
    schema_path = Path(__file__).parent.parent / "config" / "案件フォーマット.json"
    if not schema_path.exists():
        raise FileNotFoundError(f"案件スキーマが見つかりません: {schema_path}")
    return schema_path.read_text(encoding="utf-8")


def _load_human_schema_text() -> str:
    """人材JSONスキーマ文字列を読み込みます。"""
    schema_path = Path(__file__).parent.parent / "config" / "人材フォーマット.json"
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
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse failed: content={content}, error={e}")
        return json.dumps({}, ensure_ascii=False)
    return json.dumps(parsed, ensure_ascii=False)


def _process_single_record_for_lm(
    endpoint: str,
    model: str,
    body_text: str,
    category: str,
    timeout: int,
) -> tuple[str, dict[str, Any]]:
    """単一レコードのLM問い合わせ結果を返す（DB更新は行わない）。"""
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

    if not isinstance(first_obj, dict):
        first_obj = {}

    return json_text, first_obj


def process_pending_records_with_lmstudio(
    conn: sqlite3.Connection,
    endpoint: str,
    model: str,
    timeout: int = 60,
    limit_per_table: int = 500,
    exclude_folders_human: list[str] | None = None,
    exclude_folders_case: list[str] | None = None,
    enabled_tables: list[str] | None = None,
    run_matching: bool = True,
    max_workers: int = 4,
) -> tuple[int, int]:
    """status='0' のレコードを LM Studio でJSON化して保存します。"""
    total_success = 0
    total_error = 0
    normalized_workers = max(1, int(max_workers))
    enabled_tables_set = set(enabled_tables) if enabled_tables else {"mails_human", "mails_case"}
    normalized_excludes_human = [
        str(folder_name).strip()
        for folder_name in (exclude_folders_human or [])
        if str(folder_name).strip()
    ]
    normalized_excludes_case = [
        str(folder_name).strip()
        for folder_name in (exclude_folders_case or [])
        if str(folder_name).strip()
    ]

    for table_name in ("mails_human", "mails_case"):
        if table_name not in enabled_tables_set:
            logger.info(f"LM後処理スキップ: table={table_name}")
            continue

        current_excludes = (
            normalized_excludes_human
            if table_name == "mails_human"
            else normalized_excludes_case
        )
        records = get_pending_records(
            conn,
            table_name,
            limit=limit_per_table,
            exclude_folders=current_excludes,
        )
        category = "人材" if table_name == "mails_human" else "案件"
        logger.info(
            f"LM後処理開始: table={table_name}, pending={len(records)}, excluded_folders={current_excludes}, workers={normalized_workers}"
        )

        futures: dict[Any, int] = {}
        with ThreadPoolExecutor(max_workers=normalized_workers) as executor:
            for record_id, body_text in records:
                future = executor.submit(
                    _process_single_record_for_lm,
                    endpoint,
                    model,
                    body_text,
                    category,
                    timeout,
                )
                futures[future] = record_id

            for future in as_completed(futures):
                record_id = futures[future]
                try:
                    json_text, first_obj = future.result()
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

    if run_matching:
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
