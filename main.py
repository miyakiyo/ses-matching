from msal import ConfidentialClientApplication
from datetime import datetime, timezone, timedelta
import argparse
import os
import yaml
import logging

from database_utils import init_db, get_last_run_at, record_run_at, delete_old_records, export_tables_to_csv
from classifier_utils import classify_ses_subject, append_unclassified_log
from graph_mail import get_mail_subjects, list_mail_folders
from postprocess_lmstudio import process_pending_records_with_lmstudio

# config.yaml ファイルから設定を読み込む（YAML形式）
env_file = 'config.yaml'
# 設定ファイルが無い場合は起動不可なので即時終了する。
if not os.path.exists(env_file):
    raise FileNotFoundError(f'{env_file} ファイルが見つかりません。{env_file} を作成してください。')

with open(env_file, 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f) or {}

# ロギング初期化
log_path = config.get('ses', {}).get('log_path', 'app.log')
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
    return 'all'


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

    unclassified_log_path = config.get('ses', {}).get('unclassified_log_path', 'unclassified.log')
    logger.info(f'件数: {len(titles)}')
    for folder, subject in titles[:1000]:
        category = classify_ses_subject(subject, project_keywords, talent_keywords)
        if category == '未分類':
            append_unclassified_log(unclassified_log_path, folder, subject)
            logger.info(str((folder, subject, category)))

    return end


def _run_lm_postprocess(conn) -> None:
    """LM後処理を実行します。"""
    lmstudio_endpoint = config.get('lmstudio', {}).get('endpoint', 'http://localhost:1234/v1/chat/completions')
    lmstudio_model = config.get('lmstudio', {}).get('model', 'Qwen2.5-7B-Instruct-GGUF')
    lmstudio_timeout = int(config.get('lmstudio', {}).get('timeout', 60))
    lmstudio_limit = int(config.get('lmstudio', {}).get('limit_per_table', 500))
    lm_success, lm_error = process_pending_records_with_lmstudio(
        conn=conn,
        endpoint=lmstudio_endpoint,
        model=lmstudio_model,
        timeout=lmstudio_timeout,
        limit_per_table=lmstudio_limit,
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
    from matching_engine import process_all_matches
    
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

    db_path = config.get('mailbox', {}).get('db_path', 'mails.db')
    conn = init_db(db_path)

    try:
        logger.info(f'実行モード: {mode}')

        if mode in ('mail', 'all'):
            access_token = _acquire_access_token()
            end = _run_mail_fetch(conn, access_token)
            if mode == 'mail':
                record_run_at(conn, end)

        if mode in ('lm', 'all'):
            _run_lm_postprocess(conn)

        if mode == 'matching':
            _run_matching_only(conn)

        if mode == 'all':
            record_run_at(conn, end)

        if mode in ('delete', 'all'):
            _run_delete_old_records(conn)

        if mode in ('csv', 'all'):
            _run_csv_export(conn)

    except Exception as e:
        logger.error(f'エラー: {e}')
    finally:
        conn.close()

