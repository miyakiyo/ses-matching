"""
指定日数以前のメールをメールサーバーから削除するスクリプト。
データベースは変更しません。メールボックスのメールのみ削除します。

実行: pipenv run python delete_mails_from_server.py
オプション: pipenv run python delete_mails_from_server.py --days 7
"""
import os
import sys
import yaml
import logging
import argparse
from datetime import datetime, timezone, timedelta
from msal import ConfidentialClientApplication

from graph_mail import delete_mails_before_date

# config.yaml ファイルから設定を読み込む
config_file = 'config.yaml'
if not os.path.exists(config_file):
    raise FileNotFoundError(f'{config_file} ファイルが見つかりません。')

with open(config_file, 'r', encoding='utf-8') as f:
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


def main():
    """メインエントリーポイント。"""
    parser = argparse.ArgumentParser(
        description='メールサーバーから指定日数以前のメールを削除します。'
    )
    parser.add_argument(
        '--days',
        type=int,
        default=14,
        help='保持日数（デフォルト: 14日。これより前のメールを削除）'
    )
    args = parser.parse_args()
    
    try:
        mailbox = config.get('mailbox', {}).get('shared_mailbox', 'sales@offgrid.co.jp')
        logger.info(f'メールサーバー削除処理を開始します。Mailbox: {mailbox}')
        
        # 削除対象日時を計算
        days_to_keep = args.days
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days_to_keep)
        logger.info(f'{days_to_keep}日以前を削除対象とします: {cutoff_dt.isoformat()}')
        
        # 認証情報の取得
        CLIENT_ID = config.get('azure', {}).get('client_id')
        CLIENT_SECRET = config.get('azure', {}).get('client_secret')
        TENANT_ID = config.get('azure', {}).get('tenant_id')
        
        if not all([CLIENT_ID, CLIENT_SECRET, TENANT_ID]):
            raise RuntimeError('認証情報が不足しています。config.yaml を確認してください。')
        
        AUTHORITY = f'https://login.microsoftonline.com/{TENANT_ID}'
        SCOPE = ['https://graph.microsoft.com/.default']
        
        # Graph API トークン取得
        logger.info('Graph API トークンを取得しています...')
        app = ConfidentialClientApplication(CLIENT_ID, authority=AUTHORITY, client_credential=CLIENT_SECRET)
        token_response = app.acquire_token_for_client(scopes=SCOPE)
        
        if not token_response or 'access_token' not in token_response:
            raise RuntimeError(f'トークン取得失敗: {token_response}')
        
        access_token = token_response['access_token']
        logger.info('Graph API トークンを取得しました')
        
        # 除外フォルダ設定を取得
        not_folder = config.get('mailbox', {}).get('not_folder', [])
        not_folder_keywords = config.get('mailbox', {}).get('not_folder_keywords', [])
        
        # メールサーバーからメール削除を実行
        logger.info(f'{cutoff_dt.isoformat()} 以前のメールをメールサーバーから削除します')
        deleted_count, error_count = delete_mails_before_date(
            mailbox,
            cutoff_dt,
            access_token,
            not_folder=not_folder,
            not_folder_keywords=not_folder_keywords
        )
        
        logger.info(f'メールサーバー削除完了: 削除={deleted_count}件, エラー={error_count}件')
        logger.info('メールサーバー削除処理が正常に完了しました。')
        return 0
        
    except Exception as e:
        logger.error(f'エラーが発生しました: {e}', exc_info=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
