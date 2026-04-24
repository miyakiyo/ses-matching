import logging
from typing import List
from pathlib import Path


def classify_ses_subject(subject: str, project_keywords: List[str], talent_keywords: List[str]) -> str:
    """件名キーワードでSESメールを分類します。

    :param subject: メール件名。
    :param project_keywords: 案件判定に使うキーワード一覧。
    :param talent_keywords: 人材判定に使うキーワード一覧。
    :return: "案件" / "人材" / "未分類" のいずれか。
    """
    # 大文字小文字の揺れを吸収して判定する。
    normalized = (subject or "").lower()
    # 各キーワードが件名に含まれるかをそれぞれ判定する。
    project_hit = any(k and k.lower() in normalized for k in project_keywords)
    talent_hit = any(k and k.lower() in normalized for k in talent_keywords)

    if talent_hit:
        return "人材"
    if project_hit:
        return "案件"

    return "未分類"


def append_unclassified_log(log_path: str, folder: str, subject: str) -> None:
    """未分類メール情報をログファイルへ追記します。

    :param log_path: 出力先ログファイルパス。
    :param folder: メールが属するフォルダ名。
    :param subject: メール件名。
    :return: なし。
    """
    # ログディレクトリを自動作成
    log_dir = Path(log_path).parent
    log_dir.mkdir(parents=True, exist_ok=True)
    
    logger_name = f"unclassified.{log_path}"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    # ルートロガーへの伝播を止めて重複出力を防ぐ。
    logger.propagate = False

    # 同じロガーにハンドラを重複登録しない。
    if not logger.handlers:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s\tfolder=%(folder)s\tsubject=%(subject)s", "%Y-%m-%d %H:%M:%S")
        )
        logger.addHandler(handler)

    # extra でフォルダ名と件名をログフォーマットへ渡す。
    logger.info("", extra={"folder": folder, "subject": subject})


def should_skip_folder(folder_name: str, not_folder: List[str], not_folder_keywords: List[str]) -> bool:
    """フォルダを除外対象とするか判定します。

    :param folder_name: 判定対象のフォルダ名。
    :param not_folder: 除外フォルダ名リスト（完全一致）。
    :param not_folder_keywords: 除外キーワードリスト（完全一致）。
    :return: 除外対象なら True、それ以外は False。
    """
    if not folder_name:
        return False

    # 比較は完全一致だが、大文字小文字は区別しない。
    norm_name = str(folder_name).lower()
    for exact_name in not_folder:
        if exact_name and str(exact_name).lower() == norm_name:
            return True

    # フォルダ名が除外キーワードを含むかを完全判定する。
    for keyword in not_folder_keywords:
        if keyword and str(keyword).lower() == norm_name:
            return True

    return False
