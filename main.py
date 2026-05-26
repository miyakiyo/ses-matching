from msal import ConfidentialClientApplication
from datetime import datetime, timezone, timedelta
import argparse
import os
import shutil
import yaml
import logging
from pathlib import Path

from src.database_utils import (
    init_db,
    get_last_run_at,
    record_run_at,
    expire_matching_target_records,
    delete_old_records,
    export_tables_to_csv,
)
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

RESULT_COUNTS_LOG_PATH = 'logs/result_counts.log'


def _build_result_counts_logger() -> logging.Logger:
    """結果件数専用ロガーを構築して返します。"""
    result_logger = logging.getLogger('result_counts')
    result_logger.setLevel(logging.INFO)
    result_logger.propagate = False
    if result_logger.handlers:
        return result_logger

    result_log_dir = Path(RESULT_COUNTS_LOG_PATH).parent
    result_log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(RESULT_COUNTS_LOG_PATH, encoding='utf-8')
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
    result_logger.addHandler(file_handler)
    return result_logger


def _log_result_counts(result_logger: logging.Logger, message: str) -> None:
    """結果件数ログを1行出力します。"""
    result_logger.info(message)

NOT_FOLDER = config.get('mailbox', {}).get('not_folder', [])
NOT_FOLDER_KEYWORDS = config.get('mailbox', {}).get('not_folder_keywords', [])
LM_EXCLUDE_FOLDERS_TALENT = config.get('mailbox', {}).get('lm_exclude_folders_talent', [])
LM_EXCLUDE_FOLDERS_PROJECT = config.get('mailbox', {}).get('lm_exclude_folders_project', [])

PROCESS_DEFAULTS = {
    'mail_fetch': True,
    'lm_postprocess_talent': True,
    'lm_postprocess_project': True,
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
    group.add_argument('-lt', action='store_true', help='LM後処理（人材）のみ実行')
    group.add_argument('-lp', action='store_true', help='LM後処理（案件）のみ実行')
    group.add_argument('-n', action='store_true', help='マッチング処理のみ実行')
    group.add_argument('-d', action='store_true', help='古いレコード削除のみ実行')
    group.add_argument('-c', action='store_true', help='CSV出力のみ実行')
    group.add_argument('-a', action='store_true', help='全処理を実行')
    return parser.parse_args()


def _resolve_mode(args: argparse.Namespace) -> str:
    """引数から実行モードを決定します。"""
    if args.m:
        return 'mail'
    if args.lt:
        return 'lm_talent'
    if args.lp:
        return 'lm_project'
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
            'lm_postprocess_talent': False,
            'lm_postprocess_project': False,
            'matching': False,
            'delete_old': False,
            'csv_export': False,
        }
    if args.lt:
        return {
            'mail_fetch': False,
            'lm_postprocess_talent': True,
            'lm_postprocess_project': False,
            'matching': False,
            'delete_old': False,
            'csv_export': False,
        }
    if args.lp:
        return {
            'mail_fetch': False,
            'lm_postprocess_talent': False,
            'lm_postprocess_project': True,
            'matching': False,
            'delete_old': False,
            'csv_export': False,
        }
    if args.n:
        return {
            'mail_fetch': False,
            'lm_postprocess_talent': False,
            'lm_postprocess_project': False,
            'matching': True,
            'delete_old': False,
            'csv_export': False,
        }
    if args.d:
        return {
            'mail_fetch': False,
            'lm_postprocess_talent': False,
            'lm_postprocess_project': False,
            'matching': False,
            'delete_old': True,
            'csv_export': False,
        }
    if args.c:
        return {
            'mail_fetch': False,
            'lm_postprocess_talent': False,
            'lm_postprocess_project': False,
            'matching': False,
            'delete_old': False,
            'csv_export': True,
        }
    if args.a:
        return {
            'mail_fetch': True,
            'lm_postprocess_talent': True,
            'lm_postprocess_project': True,
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


def _run_mail_fetch(conn, access_token: str) -> tuple[datetime, int, int, int, int]:
    """メール取得と分類保存を実行します。"""
    mailbox = config.get('mailbox', {}).get('shared_mailbox', '*****@offgrid.co.jp')
    last_run_at = get_last_run_at(conn)

    folders = list_mail_folders(mailbox, access_token, token_refresher=_acquire_access_token)
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
    status2_keywords = config.get('ses', {}).get('status2_keywords', [])
    status3_window_hours = int(config.get('ses', {}).get('status3_window_hours', 48))
    titles, mail_counts = get_mail_subjects(
        mailbox,
        start,
        end,
        access_token=access_token,
        conn=conn,
        project_keywords=project_keywords,
        talent_keywords=talent_keywords,
        status2_keywords=status2_keywords,
        status3_window_hours=status3_window_hours,
        not_folder=NOT_FOLDER,
        not_folder_keywords=NOT_FOLDER_KEYWORDS,
        token_refresher=_acquire_access_token,
    )

    unclassified_log_path = config.get('ses', {}).get('unclassified_log_path', 'logs/unclassified.log')
    logger.info(f'件数: {len(titles)}')
    for folder, subject in titles[:1000]:
        category = classify_ses_subject(subject, project_keywords, talent_keywords)
        if category == '未分類':
            append_unclassified_log(unclassified_log_path, folder, subject)
            logger.info(str((folder, subject, category)))

    return (
        end,
        mail_counts.get('project', 0),
        mail_counts.get('talent', 0),
        mail_counts.get('status2', 0),
        mail_counts.get('status3', 0),
    )


def _run_lm_postprocess(conn, run_talent: bool = True, run_project: bool = True) -> tuple[int, int, int, int]:
    """LM後処理を実行します。"""
    if not run_talent and not run_project:
        logger.info('LM後処理は設定によりスキップされました。')
        return 0, 0, 0, 0

    lmstudio_endpoint = config.get('lmstudio', {}).get('endpoint', 'http://localhost:1234/v1/chat/completions')
    lmstudio_model = config.get('lmstudio', {}).get('model', 'Qwen2.5-7B-Instruct-GGUF')
    lmstudio_timeout = int(config.get('lmstudio', {}).get('timeout', 60))
    lmstudio_max_tokens = max(1, int(config.get('lmstudio', {}).get('max_tokens', 512)))
    lmstudio_limit_talent = int(config.get('lmstudio', {}).get('limit_per_table_talent', 500))
    lmstudio_limit_project = int(config.get('lmstudio', {}).get('limit_per_table_project', 500))
    lmstudio_num_workers = max(1, int(config.get('lmstudio', {}).get('num_workers', 4)))
    lmstudio_signature_trim_chars = max(0, int(config.get('lmstudio', {}).get('signature_trim_chars', 300)))
    lmstudio_greeting_trim_chars = max(0, int(config.get('lmstudio', {}).get('greeting_trim_chars', 100)))
    lmstudio_interval_work_seconds = max(0, int(config.get('lmstudio', {}).get('interval_work_seconds', 0)))
    lmstudio_interval_rest_seconds = max(0, int(config.get('lmstudio', {}).get('interval_rest_seconds', 30)))
    lmstudio_reload_interval_seconds = max(0, int(config.get('lmstudio', {}).get('reload_interval_seconds', 900)))
    status5_keywords_config = config.get('ses', {}).get('status5_keywords', [])
    if isinstance(status5_keywords_config, list):
        status5_keywords = [str(keyword).strip() for keyword in status5_keywords_config if str(keyword).strip()]
    else:
        logger.warning('config.ses.status5_keywords は list を指定してください。default=[] を使用します。')
        status5_keywords = []
    lm_exclude_folders_talent = [str(name).strip() for name in LM_EXCLUDE_FOLDERS_TALENT if str(name).strip()]
    lm_exclude_folders_project = [str(name).strip() for name in LM_EXCLUDE_FOLDERS_PROJECT if str(name).strip()]
    enabled_tables = []
    if run_talent:
        enabled_tables.append('mails_talent')
    if run_project:
        enabled_tables.append('mails_project')

    lm_success, lm_error, lm_success_talent, lm_success_project = process_pending_records_with_lmstudio(
        conn=conn,
        endpoint=lmstudio_endpoint,
        model=lmstudio_model,
        timeout=lmstudio_timeout,
        max_tokens=lmstudio_max_tokens,
        limit_per_table_talent=lmstudio_limit_talent,
        limit_per_table_project=lmstudio_limit_project,
        exclude_folders_talent=lm_exclude_folders_talent,
        exclude_folders_project=lm_exclude_folders_project,
        enabled_tables=enabled_tables,
        run_matching=False,
        max_workers=lmstudio_num_workers,
        signature_trim_chars=lmstudio_signature_trim_chars,
        greeting_trim_chars=lmstudio_greeting_trim_chars,
        interval_work_seconds=lmstudio_interval_work_seconds,
        interval_rest_seconds=lmstudio_interval_rest_seconds,
        reload_interval_seconds=lmstudio_reload_interval_seconds,
        status5_keywords=status5_keywords,
    )
    logger.info(f'LM後処理結果: success={lm_success}, error={lm_error}')
    return lm_success, lm_error, lm_success_talent, lm_success_project


def _run_delete_old_records(conn) -> tuple[int, int, int, int]:
    """古いレコード削除を実行します。"""
    delete_days = max(1, int(config.get('delete_old', {}).get('days', 7)))
    deleted_talent, deleted_project, deleted_matches, deleted_history = delete_old_records(conn, days=delete_days)
    logger.info(
        f'削除完了: days={delete_days}, '
        f'mails_talent={deleted_talent}件, mails_project={deleted_project}件, '
        f'matches={deleted_matches}件, run_history={deleted_history}件'
    )
    return deleted_talent, deleted_project, deleted_matches, deleted_history


def _run_csv_export(conn) -> None:
    """CSV出力を実行します。"""
    mailbox_config = config.get('mailbox', {})
    csv_output_dir = mailbox_config.get('csv_output_dir', 'csv_exports')
    db_path = mailbox_config.get('db_path', 'DB/mails.db')
    include_matches_joined = mailbox_config.get('export_matches_joined_csv', True)
    if not isinstance(include_matches_joined, bool):
        logger.warning('config.mailbox.export_matches_joined_csv は bool を指定してください。default=true を使用します。')
        include_matches_joined = True

    raw_copy_destinations = mailbox_config.get('csv_copy_destinations', [])
    if not isinstance(raw_copy_destinations, list):
        logger.warning('config.mailbox.csv_copy_destinations は list を指定してください。default=[] を使用します。')
        raw_copy_destinations = []
    copy_destinations = [str(path).strip() for path in raw_copy_destinations if str(path).strip()]

    raw_db_copy_destinations = mailbox_config.get('db_copy_destinations', [])
    if not isinstance(raw_db_copy_destinations, list):
        logger.warning('config.mailbox.db_copy_destinations は list を指定してください。default=[] を使用します。')
        raw_db_copy_destinations = []
    db_copy_destinations = [str(path).strip() for path in raw_db_copy_destinations if str(path).strip()]

    exported_files = export_tables_to_csv(
        conn,
        csv_output_dir,
        include_matches_joined=include_matches_joined,
    )
    for exported_file in exported_files:
        logger.info(f'CSV出力完了: {exported_file}')

    for destination in copy_destinations:
        destination_path = Path(destination)
        if not destination_path.is_dir():
            logger.warning(f'CSVコピー先ディレクトリが存在しないためスキップします: {destination_path}')
            continue

        for exported_file in exported_files:
            source_path = Path(exported_file)
            if not source_path.exists():
                logger.warning(f'コピー元CSVが存在しないためスキップします: {source_path}')
                continue

            target_path = destination_path / source_path.name
            try:
                shutil.copy2(source_path, target_path)
                logger.info(f'CSVコピー完了: {source_path} -> {target_path}')
            except Exception as ex:
                logger.warning(f'CSVコピーに失敗しました: {source_path} -> {target_path}, error={ex}')

    db_source_path = Path(db_path)
    if not db_source_path.exists():
        logger.warning(f'コピー元DBが存在しないためDBコピーをスキップします: {db_source_path}')
        return

    for destination in db_copy_destinations:
        destination_path = Path(destination)
        if not destination_path.is_dir():
            logger.warning(f'DBコピー先ディレクトリが存在しないためスキップします: {destination_path}')
            continue

        target_path = destination_path / db_source_path.name
        try:
            shutil.copy2(db_source_path, target_path)
            logger.info(f'DBコピー完了: {db_source_path} -> {target_path}')
        except Exception as ex:
            logger.warning(f'DBコピーに失敗しました: {db_source_path} -> {target_path}, error={ex}')


def _run_matching_only(conn) -> dict[str, int]:
    """マッチング処理のみを実行します。"""
    from src.matching_engine import process_all_matches
    matching_config = config.get('matching', {})
    ses_config = config.get('ses', {})
    matching_expire_hours = max(0, int(ses_config.get('matching_expire_hours', 120)))
    matching_multiprocess_enabled = matching_config.get('multiprocess_enabled', False)
    if not isinstance(matching_multiprocess_enabled, bool):
        logger.warning('config.matching.multiprocess_enabled は bool を指定してください。default=false を使用します。')
        matching_multiprocess_enabled = False
    matching_num_workers = max(1, int(matching_config.get('num_workers', 1)))
    matching_chunk_size = max(1, int(matching_config.get('chunk_size', 50)))
    matching_talent_log_interval = max(1, int(matching_config.get('talent_log_interval', 100)))

    expired_talent, expired_project = expire_matching_target_records(
        conn,
        expire_hours=matching_expire_hours,
    )
    logger.info(
        f'マッチング期限更新: hours={matching_expire_hours}, '
        f'expired_talent={expired_talent}, expired_project={expired_project}'
    )
    
    logger.info('マッチング処理開始...')
    try:
        match_stats = process_all_matches(
            conn,
            use_multiprocessing=matching_multiprocess_enabled,
            num_workers=matching_num_workers,
            chunk_size=matching_chunk_size,
            talent_log_interval=matching_talent_log_interval,
        )
        logger.info(
            f'マッチング処理完了: total={match_stats["total_matches"]}, '
            f'added={match_stats["added"]}, '
            f'expired_talent={expired_talent}, expired_project={expired_project}'
        )
        return match_stats
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
    result_counts_logger = _build_result_counts_logger()

    try:
        logger.info(f'実行モード: {mode}')
        logger.info(f'実行プラン: {process_plan}')
        end = None

        if process_plan['mail_fetch']:
            try:
                access_token = _acquire_access_token()
                end, mail_project_count, mail_talent_count, mail_status2_count, mail_status3_count = _run_mail_fetch(conn, access_token)
                _log_result_counts(
                    result_counts_logger,
                    f'process=mail status=success mail_project={mail_project_count} mail_talent={mail_talent_count} status2={mail_status2_count} status3={mail_status3_count}',
                )
            except Exception as e:
                _log_result_counts(
                    result_counts_logger,
                    f'process=mail status=failed error={str(e).replace(" ", "_")}',
                )
                raise
        else:
            logger.info('メール取得は設定によりスキップされました。')
            _log_result_counts(result_counts_logger, 'process=mail status=skipped')

        if process_plan['lm_postprocess_talent'] or process_plan['lm_postprocess_project']:
            try:
                lm_success, lm_error, lm_talent_count, lm_project_count = _run_lm_postprocess(
                    conn,
                    run_talent=process_plan['lm_postprocess_talent'],
                    run_project=process_plan['lm_postprocess_project'],
                )
                _log_result_counts(
                    result_counts_logger,
                    f'process=lm status=success lm_project={lm_project_count} lm_talent={lm_talent_count} lm_success={lm_success} lm_error={lm_error}',
                )
            except Exception as e:
                _log_result_counts(
                    result_counts_logger,
                    f'process=lm status=failed error={str(e).replace(" ", "_")}',
                )
                raise
        else:
            logger.info('LM後処理は設定によりスキップされました。')
            _log_result_counts(result_counts_logger, 'process=lm status=skipped')

        if process_plan['matching']:
            try:
                match_stats = _run_matching_only(conn)
                _log_result_counts(
                    result_counts_logger,
                    f'process=matching status=success matching_total={match_stats["total_matches"]} matching_added={match_stats["added"]}',
                )
            except Exception as e:
                _log_result_counts(
                    result_counts_logger,
                    f'process=matching status=failed error={str(e).replace(" ", "_")}',
                )
                raise
        else:
            logger.info('マッチング処理は設定によりスキップされました。')
            _log_result_counts(result_counts_logger, 'process=matching status=skipped')

        if end is not None:
            record_run_at(conn, end)

        if process_plan['delete_old']:
            try:
                deleted_talent, deleted_project, deleted_matches, deleted_history = _run_delete_old_records(conn)
                delete_total = deleted_talent + deleted_project + deleted_matches
                _log_result_counts(
                    result_counts_logger,
                    f'process=delete status=success delete_total={delete_total} delete_talent={deleted_talent} delete_project={deleted_project} delete_matches={deleted_matches} delete_history={deleted_history}',
                )
            except Exception as e:
                _log_result_counts(
                    result_counts_logger,
                    f'process=delete status=failed error={str(e).replace(" ", "_")}',
                )
                raise
        else:
            logger.info('古いレコード削除は設定によりスキップされました。')
            _log_result_counts(result_counts_logger, 'process=delete status=skipped')

        if process_plan['csv_export']:
            _run_csv_export(conn)
        else:
            logger.info('CSV出力は設定によりスキップされました。')

    except Exception as e:
        logger.error(f'エラー: {e}')
    finally:
        conn.close()

