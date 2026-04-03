"""
2週間以前のメールをメールボックスから削除します。
データベースは変更しません。

実行: pipenv run python delete_old_mails.py
"""
import os
import yaml
import logging
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

if __name__ == '__main__':
    try:
        mailbox = config.get('mailbox', {}).get('shared_mailbox', '******@offgrid.co.jp')
        logger.info(f'メール削除処理を開始します。Mailbox: {mailbox}')
        
        # 2週間削除対象日時を計算
        days_to_keep = 14
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days_to_keep)
        logger.info(f'{days_to_keep}日以前を削除対象とします: {cutoff_dt.isoformat()}')
        
        # Graph APIでメールボックスから削除を実行
        try:
            # 認証情報の取得
            CLIENT_ID = config.get('azure', {}).get('client_id')
            CLIENT_SECRET = config.get('azure', {}).get('client_secret')
            TENANT_ID = config.get('azure', {}).get('tenant_id')
            
            if not all([CLIENT_ID, CLIENT_SECRET, TENANT_ID]):
                raise RuntimeError('認証情報が不足しています。config.yamlを確認してください。')
            
            AUTHORITY = f'https://login.microsoftonline.com/{TENANT_ID}'
            SCOPE = ['https://graph.microsoft.com/.default']
            
            app = ConfidentialClientApplication(CLIENT_ID, authority=AUTHORITY, client_credential=CLIENT_SECRET)
            token_response = app.acquire_token_for_client(scopes=SCOPE)
            
            if not token_response or 'access_token' not in token_response:
                raise RuntimeError(f'トークン取得失敗: {token_response}')
            
            access_token = token_response['access_token']
            logger.info('Graph APIトークンを取得しました')
            
            # 残しておきたいフォルダを取得
            not_folder = config.get('mailbox', {}).get('not_folder', [])
            not_folder_keywords = config.get('mailbox', {}).get('not_folder_keywords', [])
            
            # メールボックスから削除
            logger.info(f'{cutoff_dt.isoformat()}以前のメールをメールボックスから削除します')
            deleted_graph, error_graph = delete_mails_before_date(
                mailbox,
                cutoff_dt,
                access_token,
                not_folder=not_folder,
                not_folder_keywords=not_folder_keywords
            )
            logger.info(f'メールボックス削除完了: 削除={deleted_graph}件, エラー={error_graph}件')
        except Exception as e:
            logger.error(f'Graph API削除処理でエラー: {e}', exc_info=True)
            exit(1)
        
        logger.info('メール削除処理が正常に完了しました。')
        
    except Exception as e:
        logger.error(f'エラーが発生しました: {e}', exc_info=True)
        exit(1)
