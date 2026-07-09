from datetime import datetime, timezone, timedelta
from typing import Callable, List, Optional, Tuple, Union
import logging
import json
import sqlite3
import requests

from .classifier_utils import classify_ses_subject, should_mark_status2, should_skip_folder, should_exclude_by_sender_addr


logger = logging.getLogger(__name__)


JST = timezone(timedelta(hours=9))


def _format_dt(dt: Union[datetime, str]) -> str:
    """日時をGraph API用のUTC ISO文字列に変換します。

    :param dt: 変換対象の日時（datetimeまたはISO文字列）。
    :return: UTC ISO形式の日時文字列（末尾Z）。
    """
    # 文字列の場合はそのまま返す。datetimeの場合はUTC ISO形式へ変換する。
    if isinstance(dt, str):
        return dt
    # naive datetime はJSTとして扱ってからUTCへ変換する。
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_mail_folders(
    user_email: str,
    access_token: str,
    token_refresher: Callable[[], str] = None,
) -> List[Tuple[str, str]]:
    """メールボックスのトップレベルフォルダ一覧を取得します。

    :param user_email: 対象メールボックスのアドレス。
    :param access_token: Microsoft Graph APIのアクセストークン。
    :return: (displayName, id) のタプル一覧。
    """
    rows: List[Tuple[str, str]] = []
    url = f"https://graph.microsoft.com/v1.0/users/{user_email}/mailFolders"
    current_token = access_token

    def _refresh_access_token() -> None:
        nonlocal current_token
        if token_refresher is None:
            raise RuntimeError("アクセストークンの有効期限が切れました。再取得関数が設定されていません。")
        new_token = token_refresher()
        if not new_token:
            raise RuntimeError("アクセストークン再取得に失敗しました。")
        current_token = new_token
        logger.info("Graph API access token refreshed while listing folders")

    def _is_expired_token_response(resp: requests.Response) -> bool:
        if resp.status_code != 401:
            return False
        body = resp.text or ""
        return "InvalidAuthenticationToken" in body and (
            "token is expired" in body.lower() or "lifetime validation failed" in body.lower()
        )

    # ページネーションに対応して全件取得する。
    while url:
        resp = requests.get(url, headers={"Authorization": f"Bearer {current_token}"})

        # トークン期限切れ時は一度だけ再取得してリトライする。
        if _is_expired_token_response(resp):
            try:
                _refresh_access_token()
                resp = requests.get(url, headers={"Authorization": f"Bearer {current_token}"})
            except Exception as refresh_error:
                logger.error(f"Failed to refresh access token while listing folders: {refresh_error}")

        # APIエラー時は空リストを返す。
        if resp.status_code != 200:
            logger.debug(f"Failed to list folders: {resp.status_code} {resp.text}")
            return rows
        data = resp.json()
        # フォルダ名とIDをタプルで保存する。
        for folder in data.get("value", []):
            rows.append((folder.get("displayName"), folder.get("id")))
        url = data.get("@odata.nextLink")
    return rows


def get_mail_subjects(
    user_email: str,
    start_dt: Union[datetime, str],
    end_dt: Union[datetime, str],
    access_token: str,
    conn: sqlite3.Connection = None,
    include_subfolders: bool = True,
    project_keywords: List[str] = None,
    talent_keywords: List[str] = None,
    status2_keywords: List[str] = None,
    not_folder: List[str] = None,
    not_folder_keywords: List[str] = None,
    status3_window_hours: int = 48,
    exclude_sender_patterns_talent: List[str] = None,
    exclude_sender_patterns_project: List[str] = None,
    token_refresher: Callable[[], str] = None,
) -> tuple[List[Tuple[str, str]], dict[str, int]]:
    """指定期間のメール件名を取得し、分類に応じてDB保存します。

    :param user_email: 対象メールボックスのアドレス。
    :param start_dt: 取得開始日時。
    :param end_dt: 取得終了日時。
    :param access_token: Microsoft Graph APIのアクセストークン。
    :param conn: 保存先SQLiteコネクション。Noneの場合は保存しません。
    :param include_subfolders: 子フォルダを再帰的に探索するか。
    :param project_keywords: 案件分類キーワード一覧。
    :param talent_keywords: 人材分類キーワード一覧。
    :param status2_keywords: mails_talent の status=2 判定に使うキーワード一覧。
    :param not_folder: 除外フォルダ名一覧（完全一致）。
    :param not_folder_keywords: 除外キーワード一覧（完全一致）。
    :param status3_window_hours: status=3 判定に使う重複検知ウィンドウ時間。
    :param exclude_sender_patterns_talent: 人材テーブル保存時に除外するメールアドレスパターン一覧。
    :param exclude_sender_patterns_project: 案件テーブル保存時に除外するメールアドレスパターン一覧。
    :return: ((folder_name, subject) の一覧, 分類別件数辞書)。
    """

    # access_tokenがない場合は処理を続行できないため、明示的に例外を投げる。
    if not access_token:
        raise RuntimeError("access_token が利用できません。認証情報を確認してください。")

    # API呼び出しの共通ヘッダを定義する。
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Prefer": 'outlook.timezone="Tokyo Standard Time", outlook.body-content-type="text"',
    }

    def _is_expired_token_response(resp: requests.Response) -> bool:
        if resp.status_code != 401:
            return False
        body = resp.text or ""
        return "InvalidAuthenticationToken" in body and (
            "token is expired" in body.lower() or "lifetime validation failed" in body.lower()
        )

    def _refresh_access_token() -> None:
        nonlocal access_token, headers
        if token_refresher is None:
            raise RuntimeError("アクセストークンの有効期限が切れました。再取得関数が設定されていません。")
        new_token = token_refresher()
        if not new_token:
            raise RuntimeError("アクセストークン再取得に失敗しました。")
        access_token = new_token
        headers["Authorization"] = f"Bearer {new_token}"
        logger.info("Graph API access token refreshed after expiration")

    def _graph_get(url: str) -> requests.Response:
        resp = requests.get(url, headers=headers)
        if _is_expired_token_response(resp):
            _refresh_access_token()
            resp = requests.get(url, headers=headers)
        return resp
    # 取得期間の日時をGraph API用のUTC ISO文字列に変換する。
    start_iso = _format_dt(start_dt)
    # 取得終了日時も同様に変換する。
    end_iso = _format_dt(end_dt)
    # キーワードリストがNoneの場合は空リストに置き換える。これにより後続の判定処理でNoneチェックを省略できる。
    project_keywords = project_keywords or []
    # 同様に人材キーワードリストもNoneなら空リストにする。
    talent_keywords = talent_keywords or []
    # status=2 判定用キーワードもNoneなら空リストにする。
    status2_keywords = status2_keywords or []
    # status=3 判定時間を正規化する。
    status3_window_hours = max(0, int(status3_window_hours or 48))
    # 除外フォルダ名リストもNoneなら空リストにする。
    not_folder = not_folder or []
    # 除外キーワードリストもNoneなら空リストにする。
    not_folder_keywords = not_folder_keywords or []

    def _to_utc_datetime(value: object) -> Optional[datetime]:
        """ISO日時文字列をUTC aware datetimeへ変換します。"""
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _to_utc_iso_z(dt: datetime) -> str:
        """UTC aware datetime をISO文字列（末尾Z）へ変換します。"""
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _should_mark_status3(
        table_name: str,
        folder_value: str,
        subject_value: str,
        sender_addr_value: str,
        received_dt_utc: Optional[datetime],
    ) -> bool:
        """重複条件に一致する status=1 レコードが直近ウィンドウ内にあるか判定します。"""
        if conn is None:
            return False
        if table_name not in ("mails_talent", "mails_project"):
            return False
        if not subject_value or received_dt_utc is None or status3_window_hours <= 0:
            return False

        window_start = _to_utc_iso_z(received_dt_utc - timedelta(hours=status3_window_hours))
        received_iso = _to_utc_iso_z(received_dt_utc)

        row = conn.execute(
            f"""
            SELECT 1
            FROM {table_name}
            WHERE status = '1'
              AND received_at >= ?
              AND received_at <= ?
              AND (
                    (folder = ? AND subject = ?)
                 OR (? IS NOT NULL AND sender_addr = ? AND subject = ?)
              )
            LIMIT 1
            """,
            (
                window_start,
                received_iso,
                folder_value,
                subject_value,
                sender_addr_value,
                sender_addr_value,
                subject_value,
            ),
        ).fetchone()
        return row is not None

    subjects: List[Tuple[str, str]] = []
    category_counts = {
        "project": 0,
        "talent": 0,
        "status2": 0,
        "status3": 0,
    }

    def fetch_messages_in_folder(folder_id: str, folder_name: str = "") -> None:
        """指定フォルダのメッセージをページング取得して分類保存する。

        :param folder_id: 取得対象フォルダID。
        :param folder_name: 取得対象フォルダ名。
        :return: なし。
        """
        nonlocal subjects
        # 取得期間でメッセージを絞り込む。
        filter_q = f"receivedDateTime ge {start_iso} and receivedDateTime le {end_iso}"
        select = "subject,receivedDateTime,parentFolderId,from,body"
        top = 50
        logger.info(f"Fetching messages for folder_id={folder_id}, folder_name={folder_name or '(unknown)'}")
        # APIのページネーションに対応して全件取得する。
        url = (
            f"https://graph.microsoft.com/v1.0/users/{user_email}/mailFolders/{folder_id}/messages"
            f"?$filter={filter_q}&$select={select}&$top={top}"
        )
        # API呼び出しの共通ヘッダを定義する。
        while url:
            resp = _graph_get(url)
            logger.debug(f"GET {url}")
            logger.debug(f"status {resp.status_code}")
            # APIエラー時はループを抜ける。
            if resp.status_code != 200:
                raise RuntimeError(f"Graph API エラー: status={resp.status_code} detail={resp.text}")

            data = resp.json()
            logger.debug(f"page_count {len(data.get('value', []))} nextLink={bool(data.get('@odata.nextLink'))}")
            # メッセージごとに件名と受信日時、送信者情報を取得する。
            for item in data.get("value", []):
                subj = item.get("subject")
                received = item.get("receivedDateTime")
                from_obj = item.get("from", {}).get("emailAddress", {})
                sender_name = from_obj.get("name")
                sender_addr = from_obj.get("address")
                body = item.get("body", {}).get("content")
                parent_id = item.get("parentFolderId") or folder_id

                folder_name = None
                # parent_idからフォルダ名を取得する。APIエラーなどで取得できない場合はIDを代わりに使う。
                try:
                    fresp = _graph_get(
                        f"https://graph.microsoft.com/v1.0/users/{user_email}/mailFolders/{parent_id}",
                    )
                    if fresp.status_code == 200:
                        folder_name = fresp.json().get("displayName")
                except Exception:
                    folder_name = None

                # フォルダ名が取得できない場合はIDをそのまま使う。
                if subj is not None:
                    subjects.append((folder_name or parent_id, subj))
                    if conn is not None:
                        # 件名分類に応じて保存先テーブルを切り替える。
                        category = classify_ses_subject(subj, project_keywords, talent_keywords)
                        table_name = None
                        # 分類結果に応じて保存先テーブルを決定する。
                        if category == "人材":
                            table_name = "mails_talent"
                            category_counts["talent"] += 1
                        elif category == "案件":
                            table_name = "mails_project"
                            category_counts["project"] += 1
                        else:  # 未分類
                            table_name = "mails_unclassified"

                        # メールアドレスフィルター処理：除外パターンに一致する場合はスキップ
                        if table_name == "mails_talent" and should_exclude_by_sender_addr(sender_addr, exclude_sender_patterns_talent):
                            logger.debug(f"メール除外（人材）: {sender_addr} - {subj}")
                            continue

                        if table_name == "mails_project" and should_exclude_by_sender_addr(sender_addr, exclude_sender_patterns_project):
                            logger.debug(f"メール除外（案件）: {sender_addr} - {subj}")
                            continue

                        # 全分類（人材、案件、未分類）をテーブルに保存する。
                        json_payload = json.dumps(item, ensure_ascii=False)
                        received_dt_utc = _to_utc_datetime(received)
                        received_for_db = _to_utc_iso_z(received_dt_utc) if received_dt_utc else received

                        base_status = '2' if table_name == "mails_talent" and should_mark_status2(subj, body, status2_keywords) else '0'
                        status = base_status
                        if _should_mark_status3(
                            table_name=table_name,
                            folder_value=(folder_name or parent_id),
                            subject_value=subj,
                            sender_addr_value=sender_addr,
                            received_dt_utc=received_dt_utc,
                        ):
                            status = '3'

                        insert_cursor = conn.execute(
                            f"INSERT OR IGNORE INTO {table_name} "
                            "(folder, subject, sender_name, sender_addr, received_at, body, status, json_data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (folder_name or parent_id, subj, sender_name, sender_addr, received_for_db, body, status, json_payload),
                        )
                        # 新規保存された行のみ status 件数に加算する。
                        if insert_cursor.rowcount > 0:
                            if status == '2':
                                category_counts["status2"] += 1
                            elif status == '3':
                                category_counts["status3"] += 1

            if conn is not None:
                # ページ単位でコミットして処理途中の再実行をしやすくする。
                conn.commit()
            url = data.get("@odata.nextLink")

    def recurse_folder(folder_path: str) -> None:
        """指定フォルダを処理し、必要に応じて子フォルダを再帰探索する。

        :param folder_path: フォルダID、または "inbox"。
        :return: なし。
        """
        # Inbox指定とID指定の両方を受け付ける。
        if folder_path.lower() == "inbox":
            folder_url = f"https://graph.microsoft.com/v1.0/users/{user_email}/mailFolders/Inbox"
        else:
            folder_url = f"https://graph.microsoft.com/v1.0/users/{user_email}/mailFolders/{folder_path}"

        response = _graph_get(folder_url)
        # APIエラー時は処理を中断する。特に403は権限不足の可能性が高いため、わかりやすいエラーメッセージを出す。
        if response.status_code != 200:
            if response.status_code == 403:
                raise RuntimeError(
                    f"フォルダ情報の取得に失敗しました: status={response.status_code} detail={response.text} - "
                    "アクセスが拒否されました。Graph のアプリ権限 "
                    "(Mail.Read / Mail.Read.Shared) と管理者同意を確認してください。"
                )
            raise RuntimeError(f"フォルダ情報の取得に失敗しました: status={response.status_code} detail={response.text}")

        folder = response.json()
        folder_id = folder.get("id")
        folder_name = folder.get("displayName") or ""
        if not folder_id:
            return

        fetch_messages_in_folder(folder_id, folder_name)

        if include_subfolders:
            child_url = f"https://graph.microsoft.com/v1.0/users/{user_email}/mailFolders/{folder_id}/childFolders"
            child_resp = _graph_get(child_url)
            logger.debug(f"GET childFolders {child_url} status {child_resp.status_code}")
            if child_resp.status_code != 200:
                logger.debug(f"childFolders error: {child_resp.status_code} {child_resp.text}")
                return

            children = child_resp.json().get("value", [])
            logger.debug(f"child count for {folder_id} {len(children)}")
            # API応答が空でもInbox配下がある環境向けに名前指定で再取得する。
            if len(children) == 0:
                try:
                    finfo = _graph_get(
                        f"https://graph.microsoft.com/v1.0/users/{user_email}/mailFolders/{folder_id}",
                    )
                    if finfo.status_code == 200:
                        fname = finfo.json().get("displayName", "")
                        # Inbox配下のフォルダが取得できない環境では、トップレベルのInboxを名前指定で再取得してみる。
                        if fname and fname.lower() == "inbox":
                            fallback_url = (
                                f"https://graph.microsoft.com/v1.0/users/{user_email}/mailFolders/Inbox/childFolders"
                            )
                            fr = _graph_get(fallback_url)
                            logger.debug(f"Fallback GET childFolders by name {fallback_url} status {fr.status_code}")
                            if fr.status_code == 200:
                                children = fr.json().get("value", [])
                except Exception:
                    pass

            # 子フォルダごとに再帰的に処理する。除外ルールにマッチするフォルダはスキップする。
            for child in children:
                cid = child.get("id")
                cname = child.get("displayName")
                logger.debug(f" child folder: {cname} {cid}")
                # フォルダ名が除外ルールにマッチする場合はスキップする。
                if should_skip_folder(cname, not_folder, not_folder_keywords):
                    logger.info(f"Skipping child folder by rule: {cname} {cid}")
                    continue
                if cid:
                    recurse_folder(cid)

    # トップレベルのフォルダ一覧を取得してから、各フォルダを再帰的に処理する。
    for folder_name, folder_id in list_mail_folders(user_email, access_token, token_refresher=token_refresher):
        try:
            # トップレベルフォルダも除外ルールを適用する。
            if should_skip_folder(folder_name, not_folder, not_folder_keywords):
                logger.info(f"Skipping folder by rule: {folder_name} {folder_id}")
                continue
            recurse_folder(folder_id)
        except Exception as error:
            logger.error(f"Error while recursing folder {folder_name} {folder_id}: {error}")

    return subjects, category_counts


def delete_mails_before_date(
    user_email: str,
    cutoff_dt: Union[datetime, str],
    access_token: str,
    include_subfolders: bool = True,
    not_folder: List[str] = None,
    not_folder_keywords: List[str] = None,
    token_refresher: Callable[[], str] = None,
    hard_delete: bool = False,
) -> tuple:
    """指定日時より前のメールをメールボックスから削除します。

    :param user_email: 対象メールボックスのアドレス。
    :param cutoff_dt: 削除対象日時（これより前のメールを削除）。
    :param access_token: Microsoft Graph APIのアクセストークン。
    :param include_subfolders: 子フォルダも再帰的に検索するか。
    :param not_folder: 除外フォルダ名一覧（完全一致）。
    :param not_folder_keywords: 除外キーワード一覧（完全一致）。
    :param token_refresher: 期限切れ時のアクセストークン再取得関数。
    :param hard_delete: True の場合は permanentDelete を使って完全削除する。
    :return: (削除したメール数, エラー数) のタプル。
    """
    # access_tokenがない場合は処理を続行できないため、明示的に例外を投げる。
    if not access_token:
        raise RuntimeError("access_token が利用できません。認証情報を確認してください。")

    cutoff_iso = _format_dt(cutoff_dt)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Prefer": 'outlook.timezone="Tokyo Standard Time"',
    }

    def _is_expired_token_response(resp: requests.Response) -> bool:
        if resp.status_code != 401:
            return False
        body = resp.text or ""
        return "InvalidAuthenticationToken" in body and (
            "token is expired" in body.lower() or "lifetime validation failed" in body.lower()
        )

    def _refresh_access_token() -> None:
        nonlocal access_token, headers
        if token_refresher is None:
            raise RuntimeError("アクセストークンの有効期限が切れました。再取得関数が設定されていません。")
        new_token = token_refresher()
        if not new_token:
            raise RuntimeError("アクセストークン再取得に失敗しました。")
        access_token = new_token
        headers["Authorization"] = f"Bearer {new_token}"
        logger.info("Graph API access token refreshed during deletion")

    def _graph_get(url: str) -> requests.Response:
        resp = requests.get(url, headers=headers)
        if _is_expired_token_response(resp):
            _refresh_access_token()
            resp = requests.get(url, headers=headers)
        return resp

    def _graph_delete(url: str) -> requests.Response:
        resp = requests.delete(url, headers=headers)
        if _is_expired_token_response(resp):
            _refresh_access_token()
            resp = requests.delete(url, headers=headers)
        return resp

    def _graph_post(url: str) -> requests.Response:
        resp = requests.post(url, headers=headers)
        if _is_expired_token_response(resp):
            _refresh_access_token()
            resp = requests.post(url, headers=headers)
        return resp

    not_folder = not_folder or []
    not_folder_keywords = not_folder_keywords or []

    deleted_count = 0
    error_count = 0

    def delete_messages_in_folder(folder_id: str, folder_name: str = "") -> None:
        """指定フォルダのメールを削除日時条件で削除する。

        :param folder_id: 対象フォルダID。
        :param folder_name: フォルダ名（ログ出力用）。
        :return: なし。
        """
        nonlocal deleted_count, error_count
        # 削除対象日時より前のメールを絞り込む。
        filter_q = f"receivedDateTime lt {cutoff_iso}"
        select = "id,subject,receivedDateTime"
        top = 50
        logger.info(f"Deleting messages before {cutoff_iso} in folder_id={folder_id}, folder_name={folder_name or '(unknown)'}")
        # APIのページネーションに対応して削除する。
        url = (
            f"https://graph.microsoft.com/v1.0/users/{user_email}/mailFolders/{folder_id}/messages"
            f"?$filter={filter_q}&$select={select}&$top={top}"
        )
        page_num = 0
        while url:
            page_num += 1
            resp = _graph_get(url)
            logger.debug(f"Fetch page {page_num}: {resp.status_code}")
            # APIエラー時はループを抜ける。
            if resp.status_code != 200:
                logger.error(f"Failed to list messages for deletion: status={resp.status_code} detail={resp.text}")
                error_count += 1
                break

            data = resp.json()
            messages = data.get("value", [])
            logger.debug(f"Page {page_num}: {len(messages)} messages to delete")
            # メッセージごとに削除を実行する。
            for item in messages:
                msg_id = item.get("id")
                msg_subject = item.get("subject", "(no subject)")
                try:
                    if hard_delete:
                        delete_url = f"https://graph.microsoft.com/v1.0/users/{user_email}/messages/{msg_id}/permanentDelete"
                        del_resp = _graph_post(delete_url)
                    else:
                        delete_url = f"https://graph.microsoft.com/v1.0/users/{user_email}/messages/{msg_id}"
                        del_resp = _graph_delete(delete_url)
                    if del_resp.status_code in [200, 204]:
                        deleted_count += 1
                        logger.debug(f"Deleted: {msg_subject}")
                    else:
                        logger.warning(
                            f"Failed to delete: {msg_subject} "
                            f"(status={del_resp.status_code}, detail={del_resp.text})"
                        )
                        error_count += 1
                except Exception as e:
                    logger.error(f"Error deleting message {msg_subject}: {e}")
                    error_count += 1

            url = data.get("@odata.nextLink")

    def recurse_delete_folder(folder_path: str) -> None:
        """指定フォルダとその子フォルダのメールを削除する。

        :param folder_path: フォルダID、または "inbox"。
        :return: なし。
        """
        # Inbox指定とID指定の両方を受け付ける。
        if folder_path.lower() == "inbox":
            folder_url = f"https://graph.microsoft.com/v1.0/users/{user_email}/mailFolders/Inbox"
        else:
            folder_url = f"https://graph.microsoft.com/v1.0/users/{user_email}/mailFolders/{folder_path}"

        response = _graph_get(folder_url)
        # APIエラー時は処理を中断する。
        if response.status_code != 200:
            logger.error(f"Failed to get folder info: status={response.status_code} detail={response.text}")
            return

        folder = response.json()
        folder_id = folder.get("id")
        folder_name = folder.get("displayName", "")
        if not folder_id:
            return

        delete_messages_in_folder(folder_id, folder_name)

        if include_subfolders:
            child_url = f"https://graph.microsoft.com/v1.0/users/{user_email}/mailFolders/{folder_id}/childFolders"
            child_resp = _graph_get(child_url)
            if child_resp.status_code != 200:
                logger.debug(f"Failed to list child folders: {child_resp.status_code}")
                return

            children = child_resp.json().get("value", [])
            logger.debug(f"Found {len(children)} child folders")
            # 子フォルダごとに再帰的に処理する。除外ルールにマッチするフォルダはスキップする。
            for child in children:
                cid = child.get("id")
                cname = child.get("displayName")
                # フォルダ名が除外ルールにマッチする場合はスキップする。
                if should_skip_folder(cname, not_folder, not_folder_keywords):
                    logger.info(f"Skipping child folder by rule: {cname}")
                    continue
                if cid:
                    recurse_delete_folder(cid)

    # トップレベルのフォルダ一覧を取得してから、各フォルダから削除を実行する。
    for folder_name, folder_id in list_mail_folders(user_email, access_token, token_refresher=token_refresher):
        try:
            # トップレベルフォルダも除外ルールを適用する。
            if should_skip_folder(folder_name, not_folder, not_folder_keywords):
                logger.info(f"Skipping folder by rule: {folder_name}")
                continue
            recurse_delete_folder(folder_id)
        except Exception as error:
            logger.error(f"Error while deleting from folder {folder_name} {folder_id}: {error}")
            error_count += 1

    logger.info(f"メール削除完了: 削除={deleted_count}件, エラー={error_count}件")
    return (deleted_count, error_count)
