from datetime import datetime, timezone, timedelta
from pathlib import Path
from contextlib import contextmanager
from typing import Optional
import csv
import json
import logging
import os
import sqlite3
import time

try:
    import msvcrt
except ImportError:
    msvcrt = None

try:
    import fcntl
except ImportError:
    fcntl = None


logger = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))

# LMフォールバック時に json_data 内へ格納する内部メタキー。
# これらはテーブル列へ展開しないため、列不一致警告の対象外とする。
LM_JSON_METADATA_KEYS = {
    "api_error",
    "raw_content",
    "sanitize_error",
    "sanitized_content",
}

# CSV出力時にJST変換する日時カラム。
CSV_DATETIME_COLUMNS = {
    "run_at",
    "created_at",
    "updated_at",
    "received_at",
    "talent_received_at",
    "project_received_at",
}

SQLITE_BUSY_TIMEOUT_MS = 300000
DB_LOCK_POLL_INTERVAL_SECONDS = 1.0
_TABLE_COLUMNS_CACHE: dict[int, dict[str, set[str]]] = {}


def _lock_file_handle(lock_handle) -> None:
    """ロックファイルの先頭1バイトを排他ロックします。"""
    lock_handle.seek(0)
    if lock_handle.tell() == 0 and lock_handle.read(1) == b"":
        lock_handle.seek(0)
        lock_handle.write(b"0")
        lock_handle.flush()
    lock_handle.seek(0)

    if msvcrt is not None:
        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    if fcntl is not None:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    raise RuntimeError("Unsupported platform: no file locking backend available")


def _unlock_file_handle(lock_handle) -> None:
    """取得済みのロックファイルを解放します。"""
    lock_handle.seek(0)
    lock_handle.flush()  # Ensure all writes are flushed before unlocking
    if msvcrt is not None:
        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    if fcntl is not None:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        return
    raise RuntimeError("Unsupported platform: no file locking backend available")


@contextmanager
def advisory_db_lock(
    db_path: str,
    timeout_seconds: int = 900,
    poll_interval_seconds: float = DB_LOCK_POLL_INTERVAL_SECONDS,
):
    """同一DBを使う別プロセス同士を待機させるための排他ロックです。"""
    lock_path = Path(f"{db_path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = open(lock_path, "a+b")
    start_time = time.monotonic()
    last_wait_log_seconds = -1

    # PHASE 2 FIX: Prepare message BEFORE acquiring lock
    # This prevents truncate() from invalidating the lock byte range
    message = f"pid={os.getpid()} acquired_at={datetime.now(timezone.utc).isoformat()}\n".encode("utf-8")

    try:
        while True:
            try:
                _lock_file_handle(lock_handle)
                break
            except OSError:
                elapsed_seconds = int(time.monotonic() - start_time)
                if elapsed_seconds >= timeout_seconds:
                    raise TimeoutError(
                        f"DB lock wait timed out after {timeout_seconds} seconds: {lock_path}"
                    )
                if elapsed_seconds != last_wait_log_seconds:
                    logger.info(
                        "DBロック待機中: db=%s, lock=%s, elapsed=%ss, timeout=%ss",
                        db_path,
                        lock_path,
                        elapsed_seconds,
                        timeout_seconds,
                    )
                    last_wait_log_seconds = elapsed_seconds
                time.sleep(poll_interval_seconds)

        # Write pre-prepared message while lock is held
        lock_handle.seek(0)
        lock_handle.truncate()
        lock_handle.write(message)
        lock_handle.flush()
        yield
    finally:
        # PHASE 1 FIX: Defensive error handling for unlock failure
        # If unlock fails, log warning but continue closing
        try:
            _unlock_file_handle(lock_handle)
        except OSError as e:
            logger.warning(
                "ファイルロック解除に失敗しましたが、処理を継続します: lock_path=%s, error=%s",
                lock_path,
                e
            )
        finally:
            lock_handle.close()


def _configure_sqlite_connection(conn: sqlite3.Connection) -> None:
    """SQLite の待機・ジャーナル設定を適用します。"""
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    journal_mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
    if journal_mode and str(journal_mode[0]).lower() != "wal":
        logger.warning("SQLite journal_mode could not be set to WAL: actual=%s", journal_mode[0])
    conn.execute("PRAGMA synchronous=NORMAL")


def _get_table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    """接続ごとにテーブル列一覧をキャッシュして返します。"""
    connection_cache = _TABLE_COLUMNS_CACHE.setdefault(id(conn), {})
    cached_columns = connection_cache.get(table_name)
    if cached_columns is not None:
        return cached_columns

    resolved_columns = {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    connection_cache[table_name] = resolved_columns
    return resolved_columns


def _to_jst_for_csv(value: object) -> object:
    """UTC系の日時文字列をJST文字列へ変換して返します。

    変換不能な値はそのまま返し、CSV出力を継続します。
    """
    if not isinstance(value, str):
        return value

    text = value.strip()
    if not text:
        return value

    # 末尾Zをfromisoformatで扱える+00:00へ正規化する。
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return value

    # tz情報がない値はUTCとして扱う（SQLite CURRENT_TIMESTAMP互換）。
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    jst_dt = parsed.astimezone(JST)

    # ISO系はオフセット付き、スペース区切りは従来互換の見た目で出力する。
    if "T" in text or text.endswith("Z") or "+" in text:
        return jst_dt.isoformat(timespec="seconds")
    return jst_dt.strftime("%Y-%m-%d %H:%M:%S")


def _convert_row_datetimes_for_csv(column_names: list[str], row: tuple) -> list[object]:
    """CSV行のうち日時カラムのみJSTへ変換して返します。"""
    converted = list(row)
    for idx, column_name in enumerate(column_names):
        if column_name in CSV_DATETIME_COLUMNS:
            converted[idx] = _to_jst_for_csv(converted[idx])
    return converted


def _ensure_columns_exist(conn: sqlite3.Connection, table_name: str, columns: list[tuple[str, str]]) -> None:
    """テーブルに不足している列を追加します。"""
    existing_columns = {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    for column_name, column_definition in columns:
        if column_name in existing_columns:
            continue
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_definition}")


def init_db(db_path: str = "DB/mails.db") -> sqlite3.Connection:
    """アプリで利用するSQLiteテーブルを初期化します。

    :param db_path: SQLiteデータベースファイルのパス。
    :return: 初期化済みのDBコネクション。
    """
    conn = sqlite3.connect(db_path, timeout=max(30.0, SQLITE_BUSY_TIMEOUT_MS / 1000))
    _TABLE_COLUMNS_CACHE[id(conn)] = {}
    _configure_sqlite_connection(conn)

    # 既存DBの命名を project/talent へ移行する（旧名があり新名がない場合のみ）。
    existing_tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }

    if "mails_human" in existing_tables and "mails_talent" not in existing_tables:
        conn.execute("ALTER TABLE mails_human RENAME TO mails_talent")
    if "mails_case" in existing_tables and "mails_project" not in existing_tables:
        conn.execute("ALTER TABLE mails_case RENAME TO mails_project")
    if "matches_human_case" in existing_tables and "matches" not in existing_tables:
        conn.execute("ALTER TABLE matches_human_case RENAME TO matches")
    if "matches_talent_project" in existing_tables and "matches" not in existing_tables:
        conn.execute("ALTER TABLE matches_talent_project RENAME TO matches")

    _ensure_columns_exist(
        conn,
        "mails_project",
        [
            ("単価下限", '"単価下限" TEXT'),
            ("単価上限", '"単価上限" TEXT'),
            ("年齢下限", '"年齢下限" TEXT'),
            ("年齢上限", '"年齢上限" TEXT'),
        ],
    )

    # 旧カラム名が残っている場合に project/talent へ移行する。
    match_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(matches)").fetchall()
    }
    if "human_id" in match_columns and "talent_id" not in match_columns:
        conn.execute("ALTER TABLE matches RENAME COLUMN human_id TO talent_id")
    if "case_id" in match_columns and "project_id" not in match_columns:
        conn.execute("ALTER TABLE matches RENAME COLUMN case_id TO project_id")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mails_talent (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            folder      TEXT,
            subject     TEXT,
            sender_name TEXT,
            sender_addr TEXT,
            received_at TEXT,
            body        TEXT,
            status      VARCHAR(1) NOT NULL DEFAULT '0',
            json_data   TEXT,
            created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "分類"      TEXT,
            "名前"      TEXT,
            "性別"      TEXT,
            "年齢"      TEXT,
            "所属"      TEXT,
            "外国籍"    TEXT,
            "個人事業主" TEXT,
            "最寄駅"    TEXT,
            "希望単価"  TEXT,
            "稼働可能日" TEXT,
            "稼働率"    TEXT,
            "リモート頻度" TEXT,
            "担当"      TEXT,
            "工程"      TEXT,
            "開発言語"  TEXT,
            "OS/クラウド" TEXT,
            "データベース" TEXT,
            "スキル"    TEXT,
            "資格"      TEXT,
            "備考"      TEXT,
            UNIQUE(sender_addr, subject, received_at)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mails_project (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            folder      TEXT,
            subject     TEXT,
            sender_name TEXT,
            sender_addr TEXT,
            received_at TEXT,
            body        TEXT,
            status      VARCHAR(1) NOT NULL DEFAULT '0',
            json_data   TEXT,
            created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "分類"      TEXT,
            "案件名"    TEXT,
            "概要"      TEXT,
            "業種"      TEXT,
            "担当"      TEXT,
            "工程"      TEXT,
            "業務内容"  TEXT,
            "作業場所"  TEXT,
            "リモート頻度" TEXT,
            "必須スキル" TEXT,
            "尚可スキル" TEXT,
            "開発言語"  TEXT,
            "データベース" TEXT,
            "OS/クラウド" TEXT,
            "求める人物像" TEXT,
            "時期"      TEXT,
            "契約期間"  TEXT,
            "単価"      TEXT,
            "単価下限"  TEXT,
            "単価上限"  TEXT,
            "稼働率"    TEXT,
            "精算"      TEXT,
            "面談"      TEXT,
            "募集人数"  TEXT,
            "年齢"      TEXT,
            "年齢下限"  TEXT,
            "年齢上限"  TEXT,
            "外国籍可否" TEXT,
            "個人事業主可否" TEXT,
            "契約形態"  TEXT,
            "商流制限_貴社所属迄" TEXT,
            "商流制限_営業支援費可" TEXT,
            "商流制限_浅い方優先" TEXT,
            "服装"      TEXT,
            "備考"      TEXT,
            UNIQUE(sender_addr, subject, received_at)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS run_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at     TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mails_unclassified (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            folder      TEXT,
            subject     TEXT,
            sender_name TEXT,
            sender_addr TEXT,
            received_at TEXT,
            body        TEXT,
            status      VARCHAR(1) NOT NULL DEFAULT '0',
            json_data   TEXT,
            created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(sender_addr, subject, received_at)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS matches( 
            id INTEGER PRIMARY KEY AUTOINCREMENT
            , talent_id INTEGER NOT NULL
            , project_id INTEGER NOT NULL
            , score INTEGER NOT NULL DEFAULT 0
            , reason TEXT
            , status VARCHAR (1) NOT NULL DEFAULT '0'
            , created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            , updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            , UNIQUE (talent_id, project_id)
        )
        """
    )

    # 差分マッチングの検索速度を安定化するインデックス。
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_mails_talent_status_updated_id
        ON mails_talent(status, updated_at, id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_mails_project_status_updated_id
        ON mails_project(status, updated_at, id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_matches_talent_project_updated
        ON matches(talent_id, project_id, updated_at)
        """
    )
    conn.commit()
    return conn


def get_last_run_at(conn: sqlite3.Connection) -> Optional[datetime]:
    """前回実行時刻を取得します。

    :param conn: SQLiteコネクション。
    :return: 前回実行時刻。履歴がなければ None。
    """
    # id の降順で1件取得し、直近の実行履歴を参照する。
    row = conn.execute("SELECT run_at FROM run_history ORDER BY id DESC LIMIT 1").fetchone()
    if not row or not row[0]:
        return None
    try:
        # 保存値はUTCのISO文字列（末尾Z）なので、fromisoformat用に+00:00へ置換する。
        return datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
    except Exception:
        return None


def record_run_at(conn: sqlite3.Connection, run_at: datetime) -> None:
    """実行時刻を履歴テーブルに保存します。

    :param conn: SQLiteコネクション。
    :param run_at: 保存する実行時刻。
    :return: なし。
    """
    # 取得条件で再利用しやすいようUTC ISO形式で保存する。
    run_at_utc = run_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("INSERT INTO run_history (run_at) VALUES (?)", (run_at_utc,))
    conn.commit()


def _to_sqlite_utc_timestamp(dt: datetime) -> str:
    """UTC datetime を SQLite CURRENT_TIMESTAMP 互換の文字列へ変換します。"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def delete_matches_for_non_active_records(conn: sqlite3.Connection) -> int:
    """status='1' ではない人材/案件に紐づく matches を削除します。"""
    cursor = conn.execute(
        """
        DELETE FROM matches
        WHERE talent_id IN (
            SELECT id FROM mails_talent WHERE status != '1'
        )
        OR project_id IN (
            SELECT id FROM mails_project WHERE status != '1'
        )
        """
    )
    deleted = cursor.rowcount if cursor.rowcount is not None else 0
    conn.commit()
    return deleted


def delete_low_score_matches(conn: sqlite3.Connection, threshold: int) -> int:
    """score が閾値以下の matches を削除します。"""
    normalized_threshold = max(0, min(100, int(threshold)))
    cursor = conn.execute(
        """
        DELETE FROM matches
        WHERE score <= ?
        """,
        (normalized_threshold,),
    )
    deleted = cursor.rowcount if cursor.rowcount is not None else 0
    conn.commit()
    return deleted


def expire_matching_target_records(
    conn: sqlite3.Connection,
    expire_hours: int,
) -> tuple[int, int]:
    """status='1' の期限超過レコードを status='4' へ更新します。

    :param conn: SQLiteコネクション。
    :param expire_hours: 期限時間（時間）。0以下は無効。
    :return: (更新した mails_talent 件数, 更新した mails_project 件数) のタプル。
    """
    normalized_hours = max(0, int(expire_hours or 0))
    if normalized_hours <= 0:
        return (0, 0)

    cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=normalized_hours)
    cutoff_str = _to_sqlite_utc_timestamp(cutoff_dt)

    talent_cursor = conn.execute(
        """
        UPDATE mails_talent
        SET status = '4',
            updated_at = CURRENT_TIMESTAMP
        WHERE status = '1'
          AND received_at IS NOT NULL
          AND datetime(received_at) <= datetime(?)
        """,
        (cutoff_str,),
    )
    expired_talent = talent_cursor.rowcount if talent_cursor.rowcount is not None else 0

    project_cursor = conn.execute(
        """
        UPDATE mails_project
        SET status = '4',
            updated_at = CURRENT_TIMESTAMP
        WHERE status = '1'
          AND received_at IS NOT NULL
          AND datetime(received_at) <= datetime(?)
        """,
        (cutoff_str,),
    )
    expired_project = project_cursor.rowcount if project_cursor.rowcount is not None else 0

    conn.commit()
    return (expired_talent, expired_project)


def delete_old_records(conn: sqlite3.Connection, days: int = 7) -> tuple:
    """指定日数以前のレコードを削除します。

    :param conn: SQLiteコネクション。
    :param days: 保持日数（デフォルト7日）。
    :return: (削除した mails_talent 件数, 削除した mails_project 件数, 削除した matches 件数, 削除した run_history 件数) のタプル。
    """
    # 指定日数前の日時をUTC ISO形式で計算する。
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_str = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    # matches に参照がある親レコードは削除対象から除外する。
    talent_cursor = conn.execute(
        """
        DELETE FROM mails_talent
        WHERE received_at < ?
          AND NOT EXISTS (
              SELECT 1
              FROM matches
              WHERE matches.talent_id = mails_talent.id
          )
        """,
        (cutoff_str,),
    )
    deleted_talent = talent_cursor.rowcount

    project_cursor = conn.execute(
        """
        DELETE FROM mails_project
        WHERE received_at < ?
          AND NOT EXISTS (
              SELECT 1
              FROM matches
              WHERE matches.project_id = mails_project.id
          )
        """,
        (cutoff_str,),
    )
    deleted_project = project_cursor.rowcount

    # matches テーブルから古いレコードを削除する。
    matches_cursor = conn.execute("DELETE FROM matches WHERE created_at < ?", (cutoff_str,))
    deleted_matches = matches_cursor.rowcount

    # run_history テーブルから古いレコードを削除する。
    history_cursor = conn.execute("DELETE FROM run_history WHERE run_at < ?", (cutoff_str,))
    deleted_history = history_cursor.rowcount
    
    conn.commit()
    conn.execute("VACUUM")

    return (deleted_talent, deleted_project, deleted_matches, deleted_history)


def export_tables_to_csv(
    conn: sqlite3.Connection,
    output_dir: str = "csv_exports",
    include_matches_joined: bool = True,
) -> list[str]:
    """各テーブルの内容をCSVファイルへ出力します。

    DB内部値はUTCのまま保持し、CSVでは日時カラムのみJSTに変換して出力します。

    :param conn: SQLiteコネクション。
    :param output_dir: CSV出力先ディレクトリ。
    :return: 出力したCSVファイルパス一覧。
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    exported_files: list[str] = []
    table_names = ["mails_talent", "mails_project", "run_history", "matches"]

    for table_name in table_names:
        cursor = conn.execute(f"SELECT * FROM {table_name}")
        rows = cursor.fetchall()
        column_names = [description[0] for description in cursor.description or []]

        csv_path = output_path / f"{table_name}.csv"
        with csv_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
            writer = csv.writer(csv_file)
            if column_names:
                writer.writerow(column_names)
            for row in rows:
                writer.writerow(_convert_row_datetimes_for_csv(column_names, row))

        exported_files.append(str(csv_path))

    if include_matches_joined:
        # matches_joined.csv: matchesテーブルをmails_talentとmails_projectと連結したCSV
        joined_cursor = conn.execute(
            """
            SELECT
                t.folder AS talent_folder,
                t.subject AS talent_subject,
                t.body AS talent_body,
                t.received_at AS talent_received_at,
                p.folder AS project_folder,
                p.subject AS project_subject,
                p.body AS project_body,
                p.received_at AS project_received_at,
                m.score AS score
            FROM matches AS m
            LEFT JOIN mails_talent AS t ON t.id = m.talent_id
            LEFT JOIN mails_project AS p ON p.id = m.project_id
            ORDER BY m.score DESC, m.id DESC
            """
        )
        joined_rows = joined_cursor.fetchall()

        joined_csv_path = output_path / "matches_joined.csv"
        with joined_csv_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
            writer = csv.writer(csv_file)
            joined_columns = [
                    "talent_folder",
                    "talent_subject",
                    "talent_body",
                    "talent_received_at",
                    "project_folder",
                    "project_subject",
                    "project_body",
                    "project_received_at",
                    "score",
            ]
            writer.writerow(joined_columns)
            for row in joined_rows:
                writer.writerow(_convert_row_datetimes_for_csv(joined_columns, row))

        exported_files.append(str(joined_csv_path))

    return exported_files


def get_pending_records(
    conn: sqlite3.Connection,
    table_name: str,
    limit: int = 500,
    exclude_folders: list[str] | None = None,
) -> list[tuple[int, str, str]]:
    """未処理レコード（status='0'）を取得します。

    :param conn: SQLiteコネクション。
    :param table_name: 対象テーブル名（mails_talent または mails_project）。
    :param limit: 取得上限件数。
    :param exclude_folders: 除外するフォルダ名一覧（完全一致・大文字小文字無視）。
    :return: (id, subject, body) のタプル一覧。
    """
    if table_name not in ("mails_talent", "mails_project"):
        raise ValueError(f"Unsupported table_name: {table_name}")

    normalized_folders = [
        str(folder_name).strip().casefold()
        for folder_name in (exclude_folders or [])
        if str(folder_name).strip()
    ]

    folder_exclude_sql = ""
    params: list[object] = []

    if normalized_folders:
        placeholders = ", ".join(["?"] * len(normalized_folders))
        folder_exclude_sql = (
            f"\n          AND LOWER(TRIM(COALESCE(folder, ''))) NOT IN ({placeholders})"
        )
        params.extend(normalized_folders)

    params.append(limit)

    rows = conn.execute(
        f"""
        SELECT id, subject, body
        FROM {table_name}
        WHERE status = '0'
          AND body IS NOT NULL
          AND body <> ''{folder_exclude_sql}
        ORDER BY id ASC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [
        (
            int(row[0]),
            str(row[1] or ""),
            str(row[2] or ""),
        )
        for row in rows
    ]


def update_record_json_status(
    conn: sqlite3.Connection,
    table_name: str,
    record_id: int,
    json_data: str,
    status: str = "1",
) -> None:
    """レコードの json_data/status/updated_at を更新します。

    :param conn: SQLiteコネクション。
    :param table_name: 対象テーブル名（mails_talent または mails_project）。
    :param record_id: 更新対象のレコードID。
    :param json_data: 保存するJSON文字列。
    :param status: 更新後ステータス。
    :return: なし。
    """
    if table_name not in ("mails_talent", "mails_project"):
        raise ValueError(f"Unsupported table_name: {table_name}")

    conn.execute(
        f"""
        UPDATE {table_name}
        SET json_data = ?,
            status = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (json_data, status, record_id),
    )


def update_record_json_status_and_properties(
    conn: sqlite3.Connection,
    table_name: str,
    record_id: int,
    json_data: str,
    properties: dict,
    status: str = "1",
) -> None:
    """json_data/status と schema由来properties列を同時更新します。"""
    if table_name not in ("mails_talent", "mails_project"):
        raise ValueError(f"Unsupported table_name: {table_name}")

    allowed_cols = _get_table_columns(conn, table_name)

    def _to_text(value):
        if value is None:
            return None
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    prop_items = []
    unknown_cols = []
    for key, value in properties.items():
        col_name = str(key)
        if col_name not in allowed_cols:
            if col_name in LM_JSON_METADATA_KEYS:
                continue
            unknown_cols.append(col_name)
            continue
        prop_items.append((col_name, _to_text(value)))

    if unknown_cols:
        logger.warning(
            "Table column mismatch detected: table=%s, id=%s, unknown_columns=%s",
            table_name,
            record_id,
            sorted(set(unknown_cols)),
        )

    set_parts = [
        "json_data = ?",
        "status = ?",
        "updated_at = CURRENT_TIMESTAMP",
    ]
    params = [json_data, status]

    for col_name, col_value in prop_items:
        set_parts.append(f'"{col_name}" = ?')
        params.append(col_value)

    params.append(record_id)
    conn.execute(
        f"""
        UPDATE {table_name}
        SET {', '.join(set_parts)}
        WHERE id = ?
        """,
        tuple(params),
    )


def add_matches_bulk(
    conn: sqlite3.Connection,
    match_rows: list[tuple[int, int, int, Optional[dict]]],
) -> int:
    """マッチング結果を matches テーブルへ一括で追加または更新します。

    :param conn: SQLiteコネクション。
    :param match_rows: (talent_id, project_id, score, reason_dict_or_none) の配列。
    :return: 追加または更新した件数。
    """
    if not match_rows:
        return 0

    params: list[tuple[int, int, int, Optional[str]]] = []
    for talent_id, project_id, score, reason in match_rows:
        normalized_score = int(score)
        if normalized_score <= 0:
            continue

        serialized_reason = None
        if reason is not None:
            serialized_reason = json.dumps(reason, ensure_ascii=False)

        params.append(
            (
                int(talent_id),
                int(project_id),
                normalized_score,
                serialized_reason,
            )
        )

    if not params:
        return 0

    sql = """
        INSERT INTO matches (talent_id, project_id, score, reason, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, '0', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(talent_id, project_id) DO UPDATE SET
            score = excluded.score,
            reason = excluded.reason,
            updated_at = CURRENT_TIMESTAMP
    """

    try:
        conn.executemany(sql, params)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return len(params)


def get_all_matches(conn: sqlite3.Connection, limit: int = None) -> list[dict]:
    """全マッチング結果を取得します。

    :param conn: SQLiteコネクション。
    :param limit: 取得上限件数。
    :return: マッチング情報の辞書リスト。
    """
    sql = "SELECT id, talent_id, project_id, score, reason, status, created_at FROM matches ORDER BY score DESC, id DESC"
    if limit:
        sql += f" LIMIT {limit}"
    
    cursor = conn.execute(sql)
    rows = cursor.fetchall()
    
    results = []
    for row in rows:
        results.append({
            "id": row[0],
            "talent_id": row[1],
            "project_id": row[2],
            "score": row[3],
            "reason": json.loads(row[4]) if row[4] else {},
            "status": row[5],
            "created_at": row[6],
        })
    return results


def get_matches_for_talent(conn: sqlite3.Connection, talent_id: int) -> list[dict]:
    """特定の人材のマッチング結果を取得します。

    :param conn: SQLiteコネクション。
    :param talent_id: 人材ID。
    :return: マッチング情報の辞書リスト。
    """
    cursor = conn.execute(
        "SELECT id, project_id, score, reason, status, created_at FROM matches WHERE talent_id = ? ORDER BY score DESC",
        (talent_id,),
    )
    rows = cursor.fetchall()
    
    results = []
    for row in rows:
        results.append({
            "id": row[0],
            "project_id": row[1],
            "score": row[2],
            "reason": json.loads(row[3]) if row[3] else {},
            "status": row[4],
            "created_at": row[5],
        })
    return results


def get_matches_for_project(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    """特定の案件のマッチング結果を取得します。

    :param conn: SQLiteコネクション。
    :param project_id: 案件ID。
    :return: マッチング情報の辞書リスト。
    """
    cursor = conn.execute(
        "SELECT id, talent_id, score, reason, status, created_at FROM matches WHERE project_id = ? ORDER BY score DESC",
        (project_id,),
    )
    rows = cursor.fetchall()
    
    results = []
    for row in rows:
        results.append({
            "id": row[0],
            "talent_id": row[1],
            "score": row[2],
            "reason": json.loads(row[3]) if row[3] else {},
            "status": row[4],
            "created_at": row[5],
        })
    return results


def get_talent_record(conn: sqlite3.Connection, talent_id: int) -> dict:
    """特定の人材レコードを辞書形式で取得します。

    :param conn: SQLiteコネクション。
    :param talent_id: 人材ID。
    :return: 人材レコードの辞書。
    """
    cursor = conn.execute("SELECT * FROM mails_talent WHERE id = ?", (talent_id,))
    row = cursor.fetchone()
    if not row:
        return {}
    
    col_names = [desc[0] for desc in cursor.description]
    return dict(zip(col_names, row))


def get_project_record(conn: sqlite3.Connection, project_id: int) -> dict:
    """特定の案件レコードを辞書形式で取得します。

    :param conn: SQLiteコネクション。
    :param project_id: 案件ID。
    :return: 案件レコードの辞書。
    """
    cursor = conn.execute("SELECT * FROM mails_project WHERE id = ?", (project_id,))
    row = cursor.fetchone()
    if not row:
        return {}
    
    col_names = [desc[0] for desc in cursor.description]
    return dict(zip(col_names, row))
