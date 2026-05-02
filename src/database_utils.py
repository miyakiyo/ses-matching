from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
import csv
import json
import logging
import sqlite3


logger = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))

# CSV出力時にJST変換する日時カラム。
CSV_DATETIME_COLUMNS = {
    "run_at",
    "created_at",
    "updated_at",
    "received_at",
    "talent_received_at",
    "project_received_at",
}


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


def init_db(db_path: str = "DB/mails.db") -> sqlite3.Connection:
    """アプリで利用するSQLiteテーブルを初期化します。

    :param db_path: SQLiteデータベースファイルのパス。
    :return: 初期化済みのDBコネクション。
    """
    conn = sqlite3.connect(db_path)

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
            "稼働率"    TEXT,
            "精算"      TEXT,
            "面談"      TEXT,
            "募集人数"  TEXT,
            "年齢"      TEXT,
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
        CREATE TABLE IF NOT EXISTS matches (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            talent_id   INTEGER NOT NULL,
            project_id    INTEGER NOT NULL,
            score      INTEGER NOT NULL DEFAULT 0,
            reason     TEXT,
            status     VARCHAR(1) NOT NULL DEFAULT '0',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (talent_id) REFERENCES mails_talent(id),
            FOREIGN KEY (project_id) REFERENCES mails_project(id),
            UNIQUE(talent_id, project_id)
        )
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


def delete_old_records(conn: sqlite3.Connection, days: int = 7) -> tuple:
    """指定日数以前のレコードを削除します。

    :param conn: SQLiteコネクション。
    :param days: 保持日数（デフォルト7日）。
    :return: (削除した mails_talent 件数, 削除した mails_project 件数, 削除した run_history 件数) のタプル。
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

    # run_history テーブルから古いレコードを削除する。
    history_cursor = conn.execute("DELETE FROM run_history WHERE run_at < ?", (cutoff_str,))
    deleted_history = history_cursor.rowcount
    
    conn.commit()

    return (deleted_talent, deleted_project, deleted_history)


def export_tables_to_csv(conn: sqlite3.Connection, output_dir: str = "csv_exports") -> list[str]:
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
) -> list[tuple[int, str]]:
    """未処理レコード（status='0'）を取得します。

    :param conn: SQLiteコネクション。
    :param table_name: 対象テーブル名（mails_talent または mails_project）。
    :param limit: 取得上限件数。
    :param exclude_folders: 除外するフォルダ名一覧（完全一致・大文字小文字無視）。
    :return: (id, body) のタプル一覧。
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
        SELECT id, body
        FROM {table_name}
        WHERE status = '0'
          AND body IS NOT NULL
          AND body <> ''{folder_exclude_sql}
        ORDER BY id ASC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [(int(row[0]), str(row[1])) for row in rows]


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

    allowed_cols = {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }

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


def check_existing_match(conn: sqlite3.Connection, talent_id: int, project_id: int) -> bool:
    """特定の人材-案件マッチングが既に存在するか確認します。

    :param conn: SQLiteコネクション。
    :param talent_id: 人材ID。
    :param project_id: 案件ID。
    :return: True（既存）、False（新規）。
    """
    result = conn.execute(
        "SELECT id FROM matches WHERE talent_id = ? AND project_id = ? LIMIT 1",
        (talent_id, project_id),
    ).fetchone()
    return result is not None


def add_match(
    conn: sqlite3.Connection,
    talent_id: int,
    project_id: int,
    score: int,
    reason: dict,
) -> int:
    """マッチング結果を matches テーブルに追加または更新します。

    :param conn: SQLiteコネクション。
    :param talent_id: 人材ID。
    :param project_id: 案件ID。
    :param score: マッチングスコア（0-100）。
    :param reason: マッチング理由（JSON辞書）。
    :return: 挿入または更新されたレコードID。
    """
    reason_json = json.dumps(reason, ensure_ascii=False)
    
    # UNIQUE 制約により、既存レコードは UPDATE、新規は INSERT される
    conn.execute(
        """
        INSERT INTO matches (talent_id, project_id, score, reason, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, '0', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(talent_id, project_id) DO UPDATE SET
            score = excluded.score,
            reason = excluded.reason,
            updated_at = CURRENT_TIMESTAMP
        """,
        (talent_id, project_id, score, reason_json),
    )
    conn.commit()
    
    result = conn.execute(
        "SELECT id FROM matches WHERE talent_id = ? AND project_id = ?",
        (talent_id, project_id),
    ).fetchone()
    return result[0] if result else -1


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
