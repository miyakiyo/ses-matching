from msal import ConfidentialClientApplication
from datetime import datetime, timezone, timedelta
import argparse
import os
import yaml
import logging
from pathlib import Path

from src.database_utils import init_db, get_last_run_at, record_run_at, delete_old_records, export_tables_to_csv
from src.classifier_utils import classify_ses_subject, append_unclassified_log
from src.graph_mail import get_mail_subjects, list_mail_folders
from src.postprocess_lmstudio import process_pending_records_with_lmstudio

# config.yaml ファイルから設定を読み込む（YAML形式）
env_file = 'config/config.yaml'
# 設定ファイルが無い場合は起動不可なので即時終了する。
if not os.path.exists(env_file):
    raise FileNotFoundError(f'{env_file} ファイルが見つかりません。{env_file} を作成してください。')

with open(env_file, 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f) or {}

# ロギング初期化
log_path = config.get('ses', {}).get('log_path', 'logs/app.log')
# ログディレクトリを自動作成
log_dir = Path(log_path).parent
log_dir.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_path, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

NOT_FOLDER = config.get('mailbox', {}).get('not_folder', [])
NOT_FOLDER_KEYWORDS = config.get('mailbox', {}).get('not_folder_keywords', [])
LM_EXCLUDE_FOLDERS_HUMAN = config.get('mailbox', {}).get('lm_exclude_folders_human', [])
LM_EXCLUDE_FOLDERS_CASE = config.get('mailbox', {}).get('lm_exclude_folders_case', [])

PROCESS_DEFAULTS = {
    'mail_fetch': True,
    'lm_postprocess_human': True,
    'lm_postprocess_case': True,
    'matching': True,
    'delete_old': True,
    'csv_export': True,
}


def _coerce_process_flag(process_name: str, value, default: bool) -> bool:
    """processes の値を bool に正規化します。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    logger.warning(
        f'config.processes.{process_name} は bool を指定してください。default={default} を使用します。'
    )
    return default


def _resolve_process_config() -> dict[str, bool]:
    """config.yaml の processes 設定を解決します。"""
    raw_processes = config.get('processes', {})
    resolved: dict[str, bool] = {}
    for process_name, default in PROCESS_DEFAULTS.items():
        raw_value = raw_processes.get(process_name) if isinstance(raw_processes, dict) else None
        resolved[process_name] = _coerce_process_flag(process_name, raw_value, default)
    return resolved

def _parse_args() -> argparse.Namespace:
    """起動引数を解析します。"""
    parser = argparse.ArgumentParser(
        description='SESメール処理バッチ',
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument('-m', action='store_true', help='メール取得のみ実行')
    group.add_argument('-l', action='store_true', help='LM後処理のみ実行')
    group.add_argument('-n', action='store_true', help='マッチング処理のみ実行')
    group.add_argument('-d', action='store_true', help='古いレコード削除のみ実行')
    group.add_argument('-c', action='store_true', help='CSV出力のみ実行')
    group.add_argument('-a', action='store_true', help='全処理を実行')
    return parser.parse_args()


def _resolve_mode(args: argparse.Namespace) -> str:
    """引数から実行モードを決定します。"""
    if args.m:
        return 'mail'
    if args.l:
        return 'lm'
    if args.n:
        return 'matching'
    if args.d:
        return 'delete'
    if args.c:
        return 'csv'
    if args.a:
        return 'all'
    # 引数なしは従来どおり全処理。
    return 'config'


def _resolve_process_plan(args: argparse.Namespace, process_config: dict[str, bool]) -> dict[str, bool]:
    """CLI優先で実行プランを解決します。"""
    if args.m:
        return {
            'mail_fetch': True,
            'lm_postprocess_human': False,
            'lm_postprocess_case': False,
            'matching': False,
            'delete_old': False,
            'csv_export': False,
        }
    if args.l:
        return {
            'mail_fetch': False,
            'lm_postprocess_human': True,
            'lm_postprocess_case': True,
            'matching': False,
            'delete_old': False,
            'csv_export': False,
        }
    if args.n:
        return {
            'mail_fetch': False,
            'lm_postprocess_human': False,
            'lm_postprocess_case': False,
            'matching': True,
            'delete_old': False,
            'csv_export': False,
        }
    if args.d:
        return {
            'mail_fetch': False,
            'lm_postprocess_human': False,
            'lm_postprocess_case': False,
            'matching': False,
            'delete_old': True,
            'csv_export': False,
        }
    if args.c:
        return {
            'mail_fetch': False,
            'lm_postprocess_human': False,
            'lm_postprocess_case': False,
            'matching': False,
            'delete_old': False,
            'csv_export': True,
        }
    if args.a:
        return {
            'mail_fetch': True,
            'lm_postprocess_human': True,
            'lm_postprocess_case': True,
            'matching': True,
            'delete_old': True,
            'csv_export': True,
        }
    # CLI未指定時のみconfigを採用する。
    return process_config


def _acquire_access_token() -> str:
    """Graph API用アクセストークンを取得します。"""
    client_id = config.get('azure', {}).get('client_id')
    client_secret = config.get('azure', {}).get('client_secret')
    tenant_id = config.get('azure', {}).get('tenant_id')
    authority = f'https://login.microsoftonline.com/{tenant_id}'
    scope = ['https://graph.microsoft.com/.default']

    # シークレットが未設定だと認証できないため起動時点で検知する。
    if not client_secret:
        raise RuntimeError('AZURE_CLIENT_SECRET environment variable is not set. Create a client secret in Azure and set it in the environment.')

    app = ConfidentialClientApplication(client_id, authority=authority, client_credential=client_secret)
    token_response = app.acquire_token_for_client(scopes=scope)

    # トークン取得失敗時は理由を含めて例外化する。
    if not token_response or 'access_token' not in token_response:
        err = token_response.get('error') if isinstance(token_response, dict) else 'no_response'
        desc = token_response.get('error_description', '') if isinstance(token_response, dict) else ''
        raise RuntimeError(f'Failed to acquire token: {err} {desc}')

    access_token = token_response['access_token']

    # access_token のペイロードをデコードしてデバッグ出力
    try:
        import base64, json

        def _b64url_decode(s: str) -> bytes:
            s = s + '=' * (-len(s) % 4)
            return base64.urlsafe_b64decode(s.encode())

        parts = access_token.split('.')
        if len(parts) >= 2:
            payload = json.loads(_b64url_decode(parts[1]))
            logger.info('access_token payload: ' + json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception:
        pass

    return access_token


def _run_mail_fetch(conn, access_token: str) -> datetime:
    """メール取得と分類保存を実行します。"""
    mailbox = config.get('mailbox', {}).get('shared_mailbox', '*****@offgrid.co.jp')
    last_run_at = get_last_run_at(conn)

    folders = list_mail_folders(mailbox, access_token)
    logger.info('Folders visible via Graph:')
    for name, fid in folders:
        logger.info(f'- {name} {fid}')

    end = datetime.now(timezone.utc)
    window_start = end - timedelta(hours=48)
    if last_run_at is None or last_run_at < window_start or last_run_at > end:
        start = window_start
    else:
        start = last_run_at

    project_keywords = config.get('ses', {}).get('project_keywords', ['案件', '募集'])
    talent_keywords = config.get('ses', {}).get('talent_keywords', ['人材', '要員'])
    titles = get_mail_subjects(
        mailbox,
        start,
        end,
        access_token=access_token,
        conn=conn,
        project_keywords=project_keywords,
        talent_keywords=talent_keywords,
        not_folder=NOT_FOLDER,
        not_folder_keywords=NOT_FOLDER_KEYWORDS,
    )

    unclassified_log_path = config.get('ses', {}).get('unclassified_log_path', 'logs/unclassified.log')
    logger.info(f'件数: {len(titles)}')
    for folder, subject in titles[:1000]:
        category = classify_ses_subject(subject, project_keywords, talent_keywords)
        if category == '未分類':
            append_unclassified_log(unclassified_log_path, folder, subject)
            logger.info(str((folder, subject, category)))

    return end


def _run_lm_postprocess(conn, run_human: bool = True, run_case: bool = True) -> None:
    """LM後処理を実行します。"""
    if not run_human and not run_case:
        logger.info('LM後処理は設定によりスキップされました。')
        return

    lmstudio_endpoint = config.get('lmstudio', {}).get('endpoint', 'http://localhost:1234/v1/chat/completions')
    lmstudio_model = config.get('lmstudio', {}).get('model', 'Qwen2.5-7B-Instruct-GGUF')
    lmstudio_timeout = int(config.get('lmstudio', {}).get('timeout', 60))
    lmstudio_max_tokens = max(1, int(config.get('lmstudio', {}).get('max_tokens', 512)))
    lmstudio_limit = int(config.get('lmstudio', {}).get('limit_per_table', 500))
    lmstudio_num_workers = max(1, int(config.get('lmstudio', {}).get('num_workers', 4)))
    lm_exclude_folders_human = [str(name).strip() for name in LM_EXCLUDE_FOLDERS_HUMAN if str(name).strip()]
    lm_exclude_folders_case = [str(name).strip() for name in LM_EXCLUDE_FOLDERS_CASE if str(name).strip()]
    enabled_tables = []
    if run_human:
        enabled_tables.append('mails_human')
    if run_case:
        enabled_tables.append('mails_case')

    lm_success, lm_error = process_pending_records_with_lmstudio(
        conn=conn,
        endpoint=lmstudio_endpoint,
        model=lmstudio_model,
        timeout=lmstudio_timeout,
        max_tokens=lmstudio_max_tokens,
        limit_per_table=lmstudio_limit,
        exclude_folders_human=lm_exclude_folders_human,
        exclude_folders_case=lm_exclude_folders_case,
        enabled_tables=enabled_tables,
        run_matching=False,
        max_workers=lmstudio_num_workers,
    )
    logger.info(f'LM後処理結果: success={lm_success}, error={lm_error}')


def _run_delete_old_records(conn) -> None:
    """古いレコード削除を実行します。"""
    deleted_human, deleted_case, deleted_history = delete_old_records(conn, days=7)
    logger.info(f'削除完了: mails_human={deleted_human}件, mails_case={deleted_case}件, run_history={deleted_history}件')


def _run_csv_export(conn) -> None:
    """CSV出力を実行します。"""
    csv_output_dir = config.get('mailbox', {}).get('csv_output_dir', 'csv_exports')
    exported_files = export_tables_to_csv(conn, csv_output_dir)
    for exported_file in exported_files:
        logger.info(f'CSV出力完了: {exported_file}')


def _run_matching_only(conn) -> None:
    """マッチング処理のみを実行します。"""
    from src.matching_engine import process_all_matches
    
    logger.info('マッチング処理開始...')
    try:
        match_stats = process_all_matches(conn)
        logger.info(
            f'マッチング処理完了: total={match_stats["total_matches"]}, '
            f'added={match_stats["added"]}, updated={match_stats["updated"]}'
        )
    except Exception as e:
        logger.error(f'マッチング処理失敗: {e}')
        raise


if __name__ == '__main__':
    args = _parse_args()
    mode = _resolve_mode(args)
    process_config = _resolve_process_config()
    process_plan = _resolve_process_plan(args, process_config)

    db_path = config.get('mailbox', {}).get('db_path', 'DB/mails.db')
    conn = init_db(db_path)

    try:
        logger.info(f'実行モード: {mode}')
        logger.info(f'実行プラン: {process_plan}')
        end = None

        if process_plan['mail_fetch']:
            access_token = _acquire_access_token()
            end = _run_mail_fetch(conn, access_token)
        else:
            logger.info('メール取得は設定によりスキップされました。')

        if process_plan['lm_postprocess_human'] or process_plan['lm_postprocess_case']:
            _run_lm_postprocess(
                conn,
                run_human=process_plan['lm_postprocess_human'],
                run_case=process_plan['lm_postprocess_case'],
            )
        else:
            logger.info('LM後処理は設定によりスキップされました。')

        if process_plan['matching']:
            _run_matching_only(conn)
        else:
            logger.info('マッチング処理は設定によりスキップされました。')

        if end is not None:
            record_run_at(conn, end)

        if process_plan['delete_old']:
            _run_delete_old_records(conn)
        else:
            logger.info('古いレコード削除は設定によりスキップされました。')

        if process_plan['csv_export']:
            _run_csv_export(conn)
        else:
            logger.info('CSV出力は設定によりスキップされました。')

    except Exception as e:
        logger.error(f'エラー: {e}')
    finally:
        conn.close()

