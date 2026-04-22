import json
import logging
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests
import sqlite3

from .database_utils import get_pending_records, update_record_json_status_and_properties
from .matching_engine import process_all_matches


logger = logging.getLogger(__name__)


def _sanitize_text_for_lm_request(text: str) -> str:
    """LM Studio 送信用に、JSONペイロード化で問題になり得る文字を除去する。"""
    normalized = text if isinstance(text, str) else str(text)

    # メーラー由来の自動リンク <https://...> は、そのままだと入力パーサと相性が悪いことがある
    normalized = re.sub(r"<((?:https?|mailto):[^>]+)>", r"\1", normalized)

    # JSON文字列内で壊れやすい制御文字を除去（改行・タブ・CRは保持）
    normalized = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", normalized)

    # 孤立サロゲートを除去
    normalized = re.sub(r"[\uD800-\uDFFF]", "", normalized)

    # 文字化け代替文字はモデル側パーサの失敗要因になることがあるため除去
    normalized = normalized.replace("\uFFFD", "")

    # 改行コードを正規化
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    return normalized


def _strip_problematic_unicode(text: str) -> str:
    """LM Studio 側で不具合を起こしやすい文字を除去する。"""
    # まず UTF-8 encode/decode で壊れた文字を一括除去
    text = text.encode("utf-8", errors="ignore").decode("utf-8", errors="ignore")

    out: list[str] = []
    for ch in text:
        # U+FFFD replacement character
        if ch == "\uFFFD":
            continue

        cat = unicodedata.category(ch)
        # 制御・書式・サロゲート・私用領域・未割当は除去
        if cat in {"Cc", "Cf", "Cs", "Co", "Cn"}:
            if ch in {"\n", "\t", "\r"}:
                out.append(ch)
            continue

        out.append(ch)
    return "".join(out)


def _escape_control_chars_in_json_strings(text: str) -> str:
    """JSON文字列内部の生制御文字をエスケープしてパース可能性を上げる。"""
    out: list[str] = []
    in_string = False
    escaped = False

    for ch in text:
        if in_string:
            if escaped:
                out.append(ch)
                escaped = False
                continue

            if ch == "\\":
                out.append(ch)
                escaped = True
                continue

            if ch == '"':
                out.append(ch)
                in_string = False
                continue

            # 文字列中の生制御文字は JSON として不正なのでエスケープする
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\r":
                out.append("\\r")
                continue
            if ch == "\t":
                out.append("\\t")
                continue
            if ord(ch) < 0x20:
                out.append(" ")
                continue

            out.append(ch)
            continue

        # JSON構造上の区切りで使われた全角カンマを半角へ寄せる
        if ch == "，":
            out.append(",")
            continue

        out.append(ch)
        if ch == '"':
            in_string = True

    return "".join(out)


def _regex_sub_outside_json_strings(
    text: str,
    pattern: str | re.Pattern[str],
    repl: str | re.Match[str] | Any,
) -> str:
    """JSON文字列の外側にだけ正規表現置換を適用する。"""
    compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
    out: list[str] = []
    buffer: list[str] = []
    in_string = False
    escaped = False

    for ch in text:
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            if buffer:
                out.append(compiled.sub(repl, "".join(buffer)))
                buffer.clear()
            out.append(ch)
            in_string = True
            continue

        buffer.append(ch)

    if buffer:
        out.append(compiled.sub(repl, "".join(buffer)))

    return "".join(out)


def _convert_object_like_string_lists_to_arrays(text: str) -> str:
    """{"a", "b"} のようなオブジェクト風文字列列挙を配列へ補正する。"""
    # key:value を持たない「文字列のみの波括弧列挙」を配列へ変換する。
    # 例: {"A", "B"} -> ["A", "B"]
    object_like_list_pattern = re.compile(
        r'\{\s*"(?:[^"\\]|\\.)*"\s*(?:,\s*"(?:[^"\\]|\\.)*"\s*)+\}'
    )

    def _to_array(m: re.Match[str]) -> str:
        block = m.group(0)
        return f"[{block[1:-1]}]"

    prev = text
    while True:
        replaced = object_like_list_pattern.sub(_to_array, prev)
        if replaced == prev:
            return replaced
        prev = replaced


def _convert_array_with_keyvalue_to_object(text: str) -> str:
    """配列内にキーバリューペアがある [ "key": value, ... ] -> { "key": value, ... }を補正する。
    
    文字列とキーバリューペアが混在する配列にも対応する。
    ただし [ {"key": "val"}, ... ] のような正常な配列はそのまま保持する。
    """

    def _has_direct_kv(inner: str) -> bool:
        """配列の中身 inner でネスト深さ0に "key": パターンがあるか確認する。"""
        depth = 0
        in_str = False
        esc = False
        i = 0
        while i < len(inner):
            c = inner[i]
            if esc:
                esc = False
            elif in_str:
                if c == '\\':
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                if depth == 0:
                    # "key": パターンに一致するか
                    m = re.match(r'"(?:[^"\\]|\\.)*"\s*:', inner[i:])
                    if m:
                        return True
                in_str = True
            elif c in '[{':
                depth += 1
            elif c in ']}':
                depth -= 1
            i += 1
        return False

    result: list[str] = []
    i = 0
    changed = False
    while i < len(text):
        if text[i] == '[':
            # 対応する ] を探す（ネスト・文字列を考慮）
            depth = 0
            in_str = False
            esc = False
            j = i
            while j < len(text):
                c = text[j]
                if esc:
                    esc = False
                elif in_str:
                    if c == '\\':
                        esc = True
                    elif c == '"':
                        in_str = False
                elif c == '"':
                    in_str = True
                elif c in '[{':
                    depth += 1
                elif c in ']}':
                    depth -= 1
                    if depth == 0:
                        break
                j += 1

            if j < len(text) and text[j] in {']', '}'}:
                inner = text[i + 1:j]
                if _has_direct_kv(inner):
                    # 正常系: [ ... ] -> { ... }
                    # 異常系: [ ... } -> { ... }（閉じ記号の取り違えを救済）
                    result.append('{')
                    result.append(inner)
                    result.append('}')
                    i = j + 1
                    changed = True
                    continue

        result.append(text[i])
        i += 1

    rebuilt = ''.join(result)
    # ネスト変換が起きた場合は再帰的に繰り返す（最大10回）
    if changed:
        for _ in range(9):
            again = _convert_array_with_keyvalue_to_object(rebuilt)
            if again == rebuilt:
                break
            rebuilt = again
    return rebuilt


def _repair_broken_json_lines(text: str) -> str:
    """行単位の軽微補正で、欠落クォート由来のJSON崩れを修復する。"""
    # 改行結合済みの場合は複数行がスペースで結合されているため
    # ループで複数のパターンマッチに対応する
    
    fixed_lines: list[str] = []
    for line in text.splitlines():
        # キーのコロン修復：{ "key: value" -> { "key": value
        # { または , の直後の "xxx: を処理
        # ただし [ "xxx: は配列要素なので処理対象外
        # ループで複数マッチに対応
        for _ in range(100):
            before = line
            # { または , の直後の "key: パターンのみ処理
            line = re.sub(
                r'([\{\,]\s*)"(?!\d{1,2}:)([^"\n：~～〜（()）\[\],]{1,80}):\s*(?=(?:"|\{|\[|true\b|false\b|null\b|-?\d+(?:\.\d+)?\s*(?=[,}\]])))',
                r'\1"\2": ',
                line,
            )
            if line == before:
                break
        
        # 末尾クォート欠落の修復
        # 例: "項目": "値, -> "項目": "値",
        line = re.sub(r'(:\s*"[^"\n]*)(\s*,\s*)$', r'\1"\2', line)

        # 配列要素の末尾クォート欠落
        # 例: [ "要素, -> [ "要素",
        line = re.sub(r'^(\s*"[^"\n]*)(\s*,\s*)$', r'\1"\2', line)

        fixed_lines.append(line)

    # 例: "項目": "値" の末尾カンマ欠落を、次行が別キーなら補完する。
    for i in range(len(fixed_lines) - 1):
        cur = fixed_lines[i]
        cur_stripped = cur.rstrip()
        if not cur_stripped:
            continue

        j = i + 1
        next_line = ""
        while j < len(fixed_lines):
            if fixed_lines[j].strip():
                next_line = fixed_lines[j].lstrip()
                break
            j += 1

        if not next_line:
            continue

        # 次行が別プロパティ開始っぽい時のみ補完
        if not next_line.startswith('"'):
            continue

        # 現行がプロパティ行で、末尾カンマが無い場合
        if ":" not in cur_stripped:
            continue
        if cur_stripped.endswith((",", "{", "[", "}", "]")):
            continue

        fixed_lines[i] = f"{cur_stripped},"

    return "\n".join(fixed_lines)


def _fix_orphan_strings_in_objects(text: str) -> str:
    """オブジェクト内でキーなし文字列が現れた場合のみ補正する。"""
    out: list[str] = []
    stack: list[dict[str, Any]] = []
    i = 0
    n = len(text)
    # 既存の _extra_item_N と番号衝突しないよう開始番号を決める
    existing_nums = [int(m.group(1)) for m in re.finditer(r'"_extra_item_(\d+)"\s*:', text)]
    extra_idx = max(existing_nums, default=0)

    while i < n:
        ch = text[i]

        if ch == '"':
            # 文字列トークンを丸ごと抽出（エスケープ考慮）
            j = i + 1
            escaped = False
            while j < n:
                cj = text[j]
                if escaped:
                    escaped = False
                elif cj == "\\":
                    escaped = True
                elif cj == '"':
                    break
                j += 1
            token = text[i:j + 1] if j < n else text[i:]

            in_object_expect_value = (
                bool(stack)
                and stack[-1]["type"] == "object"
                and not stack[-1]["expect_key"]
            )
            if in_object_expect_value:
                # この文字列トークンの直前の非空白文字（入力側）を確認する。
                # ':' 直後なら正規の値開始なので何もしない。
                p = i - 1
                while p >= 0 and text[p].isspace():
                    p -= 1
                prev_non_space = text[p] if p >= 0 else ""

                if prev_non_space != ":":
                    k = j + 1
                    while k < n and text[k].isspace():
                        k += 1

                    # 値の途中で次キーが来た（カンマ欠落）ケース
                    if k < n and text[k] in {":", "："}:
                        out.append(", ")
                        out.append(token)
                        stack[-1]["expect_key"] = True
                        i = j + 1
                        continue

                    # キーでない文字列が続く場合も、欠落カンマを補って orphan として収容
                    extra_idx += 1
                    out.append(f', "_extra_item_{extra_idx}": {token}')
                    stack[-1]["expect_key"] = True
                    i = j + 1
                    continue

            in_object_expect_key = (
                bool(stack)
                and stack[-1]["type"] == "object"
                and stack[-1]["expect_key"]
            )
            if in_object_expect_key:
                k = j + 1
                while k < n and text[k].isspace():
                    k += 1

                # "key": の形でなければ、キーなし文字列要素とみなして補正
                if k >= n or text[k] not in {":", "："}:
                    extra_idx += 1
                    out.append(f'"_extra_item_{extra_idx}": {token}')
                    # 1つの key:value を補った後なので、次はキー（または ,）を待つ状態へ戻す
                    stack[-1]["expect_key"] = True
                    i = j + 1
                    continue

            out.append(token)
            i = j + 1
            continue

        if ch == "{":
            # オブジェクトがキー期待位置に現れた場合は、キーなし値として補正
            in_object_expect_key = (
                bool(stack)
                and stack[-1]["type"] == "object"
                and stack[-1]["expect_key"]
            )
            if in_object_expect_key:
                extra_idx += 1
                out.append(f'"_extra_item_{extra_idx}": ')
                stack[-1]["expect_key"] = False
            stack.append({"type": "object", "expect_key": True})
            out.append(ch)
            i += 1
            continue

        if ch == "[":
            # 配列がキー期待位置に現れた場合も、キーなし値として補正
            in_object_expect_key = (
                bool(stack)
                and stack[-1]["type"] == "object"
                and stack[-1]["expect_key"]
            )
            if in_object_expect_key:
                extra_idx += 1
                out.append(f'"_extra_item_{extra_idx}": ')
                stack[-1]["expect_key"] = False
            stack.append({"type": "array"})
            out.append(ch)
            i += 1
            continue

        if ch == "}":
            if stack and stack[-1]["type"] == "object":
                stack.pop()
            out.append(ch)
            i += 1
            continue

        if ch == "]":
            if stack and stack[-1]["type"] == "array":
                stack.pop()
            out.append(ch)
            i += 1
            continue

        if ch in {":", "："}:
            if stack and stack[-1]["type"] == "object":
                stack[-1]["expect_key"] = False
            # 全角コロンは JSON として不正なので半角へ寄せる
            out.append(":")
            i += 1
            continue

        if ch == ",":
            if stack and stack[-1]["type"] == "object":
                stack[-1]["expect_key"] = True
            out.append(ch)
            i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def _close_unbalanced_json_structures(text: str) -> str:
    """末尾切れで未閉じになった JSON 構造を最小限で補完する。"""
    stack: list[str] = []
    in_string = False
    escaped = False

    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            stack.append("}")
            continue
        if ch == "[":
            stack.append("]")
            continue
        if ch in {"}", "]"} and stack and stack[-1] == ch:
            stack.pop()

    out = text.rstrip()
    if out.endswith(":"):
        out = out[:-1].rstrip() + " null"
    if out.endswith(","):
        out = out[:-1].rstrip()
    if in_string:
        out += '"'
    if stack:
        out += "".join(reversed(stack))
    return out


def _trim_after_first_complete_json(text: str) -> str:
    """先頭のJSON値が閉じた位置までを残し、後続ノイズを切り捨てる。"""
    s = text.lstrip()
    if not s:
        return text
    if s[0] not in "[{":
        return text

    stack: list[str] = []
    in_string = False
    escaped = False

    for i, ch in enumerate(s):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            stack.append("}")
            continue
        if ch == "[":
            stack.append("]")
            continue
        if ch in {"}", "]"}:
            if not stack or stack[-1] != ch:
                continue
            stack.pop()
            if not stack:
                return s[: i + 1]

    return s


def _sanitize_json_like_content(content: str) -> str:
    """LLMが返したJSON風文字列を、よくある崩れに対して軽微補正する。"""
    normalized = content.strip()

    # ```json ... ``` のコードフェンスを除去
    normalized = normalized.replace("```json", "").replace("```", "").strip()

    # 先頭/末尾の余計な説明文を切り落とし、JSON本体らしき区間を抽出
    first_obj = normalized.find("{")
    first_arr = normalized.find("[")
    candidates = [idx for idx in (first_obj, first_arr) if idx >= 0]
    if candidates:
        start_idx = min(candidates)
        last_obj = normalized.rfind("}")
        last_arr = normalized.rfind("]")
        end_idx = max(last_obj, last_arr)
        if end_idx >= start_idx:
            normalized = normalized[start_idx:end_idx + 1]

    # 生の改行を正規化
    # 改行周辺のスペースを除去して、改行を1スペースに置換するだけ
    # これにより "key": value\n"key2" が "key": value "key2" へ修復される
    # 同時に ■■等の強調記号も除去する
    
    # 例: "内容": "...＝＝＝\n■氏名": の形式（改行内で文字列が未閉じ）を修復する
    # "...＝＝＝\n■...": → "...＝＝＝", "...":に修復することで、改行前に文字列を閉じる
    normalized = re.sub(
        r'"([^"]*?)(\S)[\s■●▪▫※【】＝=ー―─-]*(?:\n|\\n)+[\s■●▪▫※【】＝=ー―─-]*([^\s":■●▪▫※【】＝=ー―─-]+)":\s*',
        r'"\1\2", "\3": ',
        normalized,
    )
    
    # 複数の改行と記号に対応：[\s■●▪▫※【】\n]* → スペース1個
    normalized = re.sub(r'[\s■●▪▫※【】]*(?:\n|\\n)+[\s■●▪▫※【】]*', ' ', normalized)
    # 複数スペースを1スペースに
    normalized = re.sub(r' +', ' ', normalized).strip()

    # 例: "9:00~18": 00": true -> "9:00~18:00": true（分断された時刻値を修復、最優先）
    # 改行結合直後の最初の修復で対応する、元の形式を早期に復元する
    normalized = re.sub(
        r'"(\d+:\d+[~～]\d+)":\s*(\d+)":\s*',
        r'"\1:\2": ',
        normalized,
    )

    # 例: "9": 00~18": 00": true -> "9:00~18:00": true
    normalized = re.sub(
        r'"(\d{1,2})"\s*:\s*(\d{1,2})\s*[~～〜]\s*(\d{1,2})"\s*:\s*(\d{1,2})"\s*:\s*',
        lambda m: f'"{m.group(1)}:{m.group(2).zfill(2)}~{m.group(3)}:{m.group(4).zfill(2)}": ',
        normalized,
    )

    # 例: "就業時間8": 00～17:00" -> "就業時間8:00～17:00"
    normalized = re.sub(
        r'"([^"\n]*?)(\d{1,2})"\s*:\s*(\d{2})([~～〜])(\d{1,2}):(\d{2})"',
        lambda m: f'"{m.group(1)}{m.group(2)}:{m.group(3)}{m.group(4)}{m.group(5)}:{m.group(6)}"',
        normalized,
    )

    # 例: "就業時間：8": 00～17": 00（...）" -> "就業時間：8:00～17:00（...）"
    # 文中の時刻が誤って "時": 分 の形に分断されたケースを復元する。
    normalized = re.sub(
        r'([：~～〜（(]\s*)(\d{1,2})"\s*:\s*(\d{2})',
        lambda m: f'{m.group(1)}{m.group(2)}:{m.group(3)}',
        normalized,
    )

    # 欠落クォート由来の行崩れを先に補正
    normalized = _repair_broken_json_lines(normalized)

    # 配列内のキーバリューペアをオブジェクトに補正
    # 例: [ "key": value, "key": value ] -> { "key": value, "key": value }
    normalized = _convert_array_with_keyvalue_to_object(normalized)

    # 例: {...} {...} / [...] {...} のような構造連結時のカンマ欠落を補完
    normalized = _regex_sub_outside_json_strings(
        normalized,
        r'\}\s*\{',
        '}, {',
    )
    normalized = _regex_sub_outside_json_strings(
        normalized,
        r'\]\s*\{',
        '], {',
    )

    # 例: [ "text ] -> [ "text" ]（配列末尾要素の閉じクォート欠落）
    normalized = re.sub(
        r'(?<=[\[,])(\s*"[^"\]]*?)(\s*\])',
        r'\1"\2',
        normalized,
    )

    # 例: "https": //example.com/path -> "https://example.com/path"
    normalized = re.sub(
        r'"(https?)"\s*:\s*(//[^"\],}]+)',
        r'"\1:\2"',
        normalized,
    )

    # 例: こちら<https": //example.com/path> -> こちら<https://example.com/path>
    normalized = re.sub(
        r'<(https?)"\s*:\s*(//[^">\]\},]+)>',
        r'<\1:\2>',
        normalized,
    )

    # 例: "年齢": "["50代迄"]" -> "年齢": "[50代迄]"
    normalized = re.sub(
        r'(:\s*)"\["([^"\]]+)"\]"',
        r'\1"[\2]"',
        normalized,
    )

    # 例: "経験度":"...SQL など）}, { -> "経験度":"...SQL など）"}, {
    normalized = re.sub(
        r'(:\s*"[^"\{\}\[\]]*?)\}([\s,]*\{)',
        r'\1"}\2',
        normalized,
    )

    # 例: "text""] -> "text"]（補正ルール競合で二重クォートになった末尾を畳む）
    normalized = re.sub(r'""(?=\s*[,}\]])', r'"', normalized)

    # 例: }"]} のような閉じ構造直後の余剰クォートを除去
    normalized = re.sub(r'([}\]])"(?=\s*[}\],])', r'\1', normalized)

    # 文字列内の生改行などを JSON エスケープへ変換
    # 注：この処理は改行正規化の後に実行すること
    normalized = _escape_control_chars_in_json_strings(normalized)

    # 例: "案件詳細": {"文1", "文2"} -> "案件詳細": ["文1", "文2"]
    normalized = _convert_object_like_string_lists_to_arrays(normalized)

    # PythonリテラルをJSONリテラルへ寄せる
    normalized = re.sub(r"\bNone\b", "null", normalized)
    normalized = re.sub(r"\bTrue\b", "true", normalized)
    normalized = re.sub(r"\bFalse\b", "false", normalized)

    # 例: "単価": 90万円 / "年齢制限": 40代まで などを文字列化
    normalized = _regex_sub_outside_json_strings(
        normalized,
        r'(:\s*)(-?\d+(?:\.\d+)?)(\s*(?:万円|千円|円|万|年|歳|岁|代|日|回|h|%|％)(?:\s*(?:程度|前後|前半|後半|以上|以下|以内|未満|くらい|ほど|迄|まで))?(?:\s*[（(][^)）]*[)）])?(?:\s*(?:程度|前後|前半|後半|以上|以下|以内|未満|くらい|ほど|迄|まで))?)(?=\s*[,}\]])',
        r'\1"\2\3"',
    )

    # 例: "単価": 3000-5500 / 140-180h / 100-120万 -> 文字列化（ハイフン区切り数値）
    normalized = _regex_sub_outside_json_strings(
        normalized,
        r'(:\s*)(-?\d+(?:\.\d+)?)(\s*-\s*)(\d+(?:\.\d+)?)(\s*(?:h|H|時間|日|ヶ月|か月|月|万円|千円|円|万|%|％))?(?=\s*[,}\]])',
        r'\1"\2\3\4\5"',
    )

    # 例: "単価": 45~55万（スキル見合い） -> "単価": "45~55万（スキル見合い）"（チルダ区切り）
    normalized = _regex_sub_outside_json_strings(
        normalized,
        r'(:\s*)(\d+[~～〜]\d+万[（(][^)）]*[)）]?)(?=\s*[,}\]])',
        r'\1"\2"',
    )

    # 例: "単価": 65万～75万まで -> "単価": "65万～75万まで"
    normalized = _regex_sub_outside_json_strings(
        normalized,
        r'(:\s*)(\d+万[~～〜]\d+万(?:まで)?)(?=\s*[,}\]])',
        r'\1"\2"',
    )

    # 例: "単価": 57万～62万 -> "単価": "57万～62万"（単純チルダ区切り）
    normalized = _regex_sub_outside_json_strings(
        normalized,
        r'(:\s*)(\d+万[~～〜]\d+万)(?=\s*[,}\]])',
        r'\1"\2"',
    )

    # 例: "単価": 650000～700000 / 550000〜600000 -> 文字列化
    normalized = _regex_sub_outside_json_strings(
        normalized,
        r'(:\s*)(\d+[~～〜]\d+)(?=\s*[,}\]])',
        r'\1"\2"',
    )

    # 例: "単価": [70万, 90万] -> ["70万", "90万"]（配列内の単位付き数値）
    normalized = _regex_sub_outside_json_strings(
        normalized,
        r'(?<=[\[,])\s*(-?\d+(?:\.\d+)?\s*(?:万円|千円|円|万|年|歳|岁|代|日|回|h|%|％)(?:\s*[（(][^)）]*[)）])?(?:程度|前後|前半|後半|以上|以下|未満|くらい|ほど|迄|まで)?)\s*(?=[,\]])',
        lambda m: f'"{m.group(1).strip()}"',
    )

    # 例: "単価": [53, 58] + "万円" -> "単価": "53~58万円"
    # 補足: 単位側が引用符を含むため、outside-json-strings の補助関数ではなく全体置換で扱う。
    normalized = re.sub(
        r'(:\s*)\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]\s*\+\s*"([^"\\]+)"(?=\s*[,}\]])',
        r'\1"\2~\3\4"',
        normalized,
    )

    # 例: "単価": ["85", "95"] + , "_extra_item_1": "万円" -> "単価": "85~95万円"
    normalized = re.sub(
        r'(:\s*)\[\s*"(-?\d+(?:\.\d+)?)"\s*,\s*"(-?\d+(?:\.\d+)?)"\s*\]\s*\+\s*,\s*"_extra_item_\d+"\s*:\s*"([^"\\]+)"(?=\s*[,}\]])',
        r'\1"\2~\3\4"',
        normalized,
    )

    # 例: "単価": [80, 90]万 -> "単価": "80~90万"
    normalized = re.sub(
        r'(:\s*)\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]\s*(万円|千円|円|万|%|％|h|H|時間|日|ヶ月|か月|月)(?=\s*[,}\]])',
        r'\1"\2~\3\4"',
        normalized,
    )

    # 例: "稼働率": 140 ~ 180 -> "稼働率": "140 ~ 180"（スペース入りチルダ区切り）
    normalized = _regex_sub_outside_json_strings(
        normalized,
        r'(:\s*)(-?\d+(?:\.\d+)?)(\s*[~～〜]\s*)(\d+(?:\.\d+)?)(?=\s*[,}\]])',
        r'\1"\2\3\4"',
    )

    # 例: "打ち合わせ": {"オンライン": 1 ~ 2回} -> "オンライン": "1 ~ 2回"
    normalized = _regex_sub_outside_json_strings(
        normalized,
        r'(:\s*)(-?\d+(?:\.\d+)?)(\s*[~～〜]\s*)(\d+(?:\.\d+)?)(\s*(?:回|h|H|時間|日|ヶ月|か月|月|万円|千円|円|万|%|％))(?=\s*[,}\]])',
        r'\1"\2\3\4\5"',
    )

    # 例: [GA4, "GTM"] -> ["GA4", "GTM"]（配列内の裸識別子を文字列化）
    def _quote_bare_identifier_in_array(m: re.Match[str]) -> str:
        token = m.group(1).strip()
        lower = token.lower()
        if lower in {"true", "false", "null"}:
            return token
        if re.fullmatch(r"-?\d+(?:\.\d+)?", token):
            return token
        return f'"{token}"'

    normalized = _regex_sub_outside_json_strings(
        normalized,
        r'(?<=[\[,])\s*([A-Za-z_][A-Za-z0-9_./+-]{0,80})\s*(?=[,\]])',
        _quote_bare_identifier_in_array,
    )

    # 例: "A": ①B": true -> "A": true, "①B": true
    # 丸数字で始まる値が次キーに崩れているケースを、JSONとして成立する形に補正する。
    normalized = _regex_sub_outside_json_strings(
        normalized,
        r':\s*([①-⑳][^":\{\}\[\],]{1,120})"\s*:\s*(true|false|null|-?\d+(?:\.\d+)?)',
        r': true, "\1": \2',
    )

    # 上記ルールで拾えない裸値を最終フォールバックで文字列化する。
    # 例: "支払いサイト": 50譌･（文字化け単位）や "精算幅": 160割にて時給精算
    def _quote_fallback_bare_value(m: re.Match[str]) -> str:
        prefix = m.group(1)
        raw_value = m.group(2).strip()

        # すでに構造化/文字列化されている値は対象外
        if any(token in raw_value for token in ['"', '[', ']', '{', '}']):
            return f"{prefix}{raw_value}"

        # JSONリテラル/数値はそのまま維持
        if raw_value in {"true", "false", "null"}:
            return f"{prefix}{raw_value}"
        if re.fullmatch(r"-?\d+(?:\.\d+)?", raw_value):
            return f"{prefix}{raw_value}"

        escaped = raw_value.replace('\\', '\\\\').replace('"', '\\"')
        return f'{prefix}"{escaped}"'

    normalized = _regex_sub_outside_json_strings(
        normalized,
        r'(:\s*)([^"\[{\],}\n][^,}\]\n]*)(?=\s*[,}\]])',
        _quote_fallback_bare_value,
    )

    # 例: "_extra_item_4": ", "本文..." -> "_extra_item_4": "本文..."
    # _extra_item 系の壊れに限定して、値先頭の不要な `", "` を除去する。
    normalized = re.sub(
        r'("_extra_item_\d+"\s*:\s*)",\s*"',
        r'\1"',
        normalized,
    )

    # 例: ..., "本文...", ", "_extra_item_5": ... -> ..., "本文...", "_extra_item_5": ...
    # 区切り用に紛れ込んだ孤立文字列 `", "` を削除する（_extra_item キー直前のみ）。
    normalized = re.sub(
        r',\s*",\s*"(_extra_item_\d+"\s*:)',
        r', "\1',
        normalized,
    )

    # 汎用: `"key": ", "text"` -> `"key": "text"`
    normalized = re.sub(
        r'("(?:[^"\\]|\\.)+"\s*:\s*)",\s*"',
        r'\1"',
        normalized,
    )

    # 汎用: `, ", "key":` -> `, "key":`
    normalized = re.sub(
        r',\s*",\s*"((?:[^"\\]|\\.)+"\s*:)',
        r', "\1',
        normalized,
    )

    # 例: "_extra_item_2": ", "■氏名": ... -> "_extra_item_2": ", ", "■氏名": ...
    # 区切り文字列値の直後に次キーが連結された壊れを補正する。
    normalized = re.sub(
        r'("_extra_item_\d+"\s*:\s*)",\s*"([^"\n]+)"\s*:',
        r'\1", ", "\2":',
        normalized,
    )

    # _extra_item 系の強い崩れを補正する。
    # 1) 裸キー化: _extra_item_5: / _extra_item_5, -> "_extra_item_5":
    normalized = re.sub(
        r'(?<=[\s,{])_extra_item_(\d+)(?=\s*[:,])',
        r'"_extra_item_\1"',
        normalized,
    )
    normalized = re.sub(
        r'"(_extra_item_\d+)\s*,',
        r'"\1":',
        normalized,
    )

    # 2) 値文字列の閉じ直後に次キーが連結したケース: "...""_extra_item_6": -> "...", "_extra_item_6":
    normalized = re.sub(
        r'("(?:[^"\\]|\\.)*")\s*("_extra_item_\d+"\s*:)',
        r'\1, \2',
        normalized,
    )

    # 3) 例: "会社, "_extra_item_1": ": "大手..." -> "会社": "大手..."
    normalized = re.sub(
        r'"([^"\\,:]{1,120})\s*,\s*"_extra_item_\d+"\s*:\s*":\s*"([^"\\]*)"',
        r'"\1": "\2"',
        normalized,
    )

    # 3.1) 例: "期間": "[直近], "会社, "_extra_item_1": ": "大手..." -> "期間": "[直近]", "会社": "大手..."
    normalized = re.sub(
        r'("(?:[^"\\]|\\.)+"\s*:\s*)"([^"\\]*?)\s*,\s*"([^"\\,:]{1,120})\s*,\s*"_extra_item_\d+"\s*:\s*":\s*"([^"\\]*)"',
        r'\1"\2", "\3": "\4"',
        normalized,
    )

    # 4) 例: "役職"_extra_item_3": ": "PL" -> "役職": "PL"
    normalized = re.sub(
        r'"([^"\\,:]{1,120})"_extra_item_\d+"\s*:\s*":\s*"([^"\\]*)"',
        r'"\1": "\2"',
        normalized,
    )

    # 4.1) 例: "役職"_extra_item_14": ": ["情報企画部..."_extra_item_15": "] -> "役職": ["情報企画部..."]
    normalized = re.sub(
        r'"([^"\\,:]{1,120})"_extra_item_\d+"\s*:\s*":\s*\[\s*"([^"\\]*)"_extra_item_\d+"\s*:\s*"\]\s*(?=[,}\]])',
        r'"\1": ["\2"]',
        normalized,
    )

    # 5) 例: "要件..."_extra_item_8": ", "SAP..." -> "要件...", "SAP..."
    normalized = re.sub(
        r'"([^"\\]+)"_extra_item_\d+"\s*:\s*",\s*"([^"\\]+)"',
        r'"\1", "\2"',
        normalized,
    )

    # 6) 例: "LINE": "https://... , "_extra_item_52": " } -> "LINE": "https://..." }
    normalized = re.sub(
        r'(:\s*"https?://[^"\\]+),\s*"_extra_item_\d+"\s*:\s*"\s*([}\]])',
        r'\1"\2',
        normalized,
    )

    # 例: {"Max": 800000, "140 ~ 180時間"} のようなキーなし文字列要素を補正
    normalized = _fix_orphan_strings_in_objects(normalized)

    # 例: "_extra_item_2": ", "■氏名": ... のような欠落カンマを補う。
    normalized = re.sub(
        r'("_extra_item_\d+"\s*:\s*"\s*,\s*")\s*("(?:[^"\\]|\\.)+"\s*:)',
        r'\1, \2',
        normalized,
    )

    # 末尾カンマを除去
    normalized = re.sub(r",\s*([}\]])", r"\1", normalized)

    # 末尾切れで未閉じになった構造を補完
    normalized = _close_unbalanced_json_structures(normalized)

    # 先頭の完全なJSON値より後ろに残ったノイズを除去
    normalized = _trim_after_first_complete_json(normalized)

    # 最終フォールバック: ここまででJSONとして成立しない場合は、
    # 原文をラップした最小JSONを返して処理停止を回避する。
    try:
        json.loads(normalized)
    except Exception:
        fallback_obj = {
            "raw_content": content if isinstance(content, str) else str(content),
            "sanitize_error": "fallback_applied",
        }
        return json.dumps(fallback_obj, ensure_ascii=False)

    return normalized


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
    max_tokens: int = 512,
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

    body_text = _sanitize_text_for_lm_request(body_text)
    body_text = _strip_problematic_unicode(body_text)
    system_content = _sanitize_text_for_lm_request(system_content)
    system_content = _strip_problematic_unicode(system_content)

    prompt = (
        f"以下は{category}メール本文です。解析して必ずJSONのみを返してください。"
        "未記載の項目は必ず null または [] にする。\n"
        "日本語で作成する。\n"
        "説明文やコードブロックは不要です。\n\n"
        f"本文:\n{body_text}"
    )

    compact_system_content = (
        "あなたはJSON抽出器です。"
        "必ずJSONのみ返してください。"
        "説明文・コードブロックは禁止。"
    )
    compact_prompt_2500 = (
        f"{category}メール本文から項目を抽出し、JSONのみ返してください。\n"
        "未記載は null または []。\n\n"
        f"本文:\n{body_text[:1500]}"
    )
    compact_prompt_1200 = (
        f"{category}メール本文から項目を抽出し、JSONのみ返してください。\n"
        "未記載は null または []。\n\n"
        f"本文:\n{body_text[:1200]}"
    )
    compact_prompt_600 = (
        f"{category}メール本文から項目を抽出し、JSONのみ返してください。\n"
        "未記載は null または []。\n\n"
        f"本文:\n{body_text[:600]}"
    )
    ultra_safe_body_text = _strip_problematic_unicode(body_text)
    compact_prompt_300_safe = (
        f"{category}メール本文から項目を抽出し、JSONのみ返してください。\n"
        "未記載は null または []。\n\n"
        f"本文:\n{ultra_safe_body_text[:300]}"
    )
    # 一部のサーバー実装で本文先頭が JSON 断片だと parse input 400 を返すことがあるため、
    # 最終手段として波括弧を中立化した本文も用意する。
    ultra_safe_neutralized_body_text = ultra_safe_body_text.replace("{", "（").replace("}", "）")
    compact_prompt_300_neutralized = (
        f"{category}メール本文から項目を抽出し、JSONのみ返してください。\n"
        "未記載は null または []。\n\n"
        f"本文:\n{ultra_safe_neutralized_body_text[:300]}"
    )

    request_variants: list[tuple[str, str]] = []

    # system プロンプト（スキーマ全文）が長すぎると n_keep 超過を起こすため、
    # 長文時は初回から軽量 system 指示を利用する。
    use_heavy_system_prompt = len(system_content) <= 2000
    primary_system_content = system_content if use_heavy_system_prompt else compact_system_content

    request_variants.append((primary_system_content, prompt))

    shortened_prompt = prompt[:12000]
    if shortened_prompt != prompt:
        # 長文ケース向けの短縮版
        request_variants.append((primary_system_content, shortened_prompt))

    # 400 parse input 向けの軽量フォールバック（常に最後に用意）
    request_variants.append((compact_system_content, compact_prompt_2500))
    request_variants.append((compact_system_content, compact_prompt_1200))
    request_variants.append((compact_system_content, compact_prompt_600))
    request_variants.append((compact_system_content, compact_prompt_300_safe))
    request_variants.append((compact_system_content, compact_prompt_300_neutralized))

    resp: requests.Response | None = None
    last_http_error: requests.HTTPError | None = None

    for i, (system_variant, prompt_variant) in enumerate(request_variants):
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_variant},
                {"role": "user", "content": prompt_variant},
            ],
            "temperature": 0.0,
            "max_tokens": max_tokens,
        }

        resp = requests.post(endpoint, json=payload, timeout=timeout)
        if resp.ok:
            break

        is_retryable_400 = (
            resp.status_code == 400
            and (
                "Failed to parse input" in resp.text
                or "n_keep" in resp.text
                or "context length" in resp.text
                or "Context size" in resp.text
            )
        )
        can_retry_with_shorter_prompt = i < len(request_variants) - 1

        if is_retryable_400 and can_retry_with_shorter_prompt:
            logger.warning(
                "LM Studio API retryable error: status=%s, body=%s",
                resp.status_code,
                resp.text,
            )
        else:
            logger.error(
                "LM Studio API error: status=%s, body=%s",
                resp.status_code,
                resp.text,
            )

        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            last_http_error = e
            if is_retryable_400 and can_retry_with_shorter_prompt:
                logger.warning(
                    "LM Studio request retry with fallback payload: category=%s, chars=%s -> %s",
                    category,
                    len(prompt_variant),
                    len(request_variants[i + 1][1]),
                )
                continue
            raise

    if resp is None:
        if last_http_error is not None:
            raise last_http_error
        raise RuntimeError("LM Studio request failed before response")

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
        repaired = _sanitize_json_like_content(content)
        try:
            parsed = json.loads(repaired)
            logger.warning("JSON parse repaired by sanitizer")
        except json.JSONDecodeError as repaired_error:
            logger.warning(
                "JSON parse failed (sanitized), applying fallback: content=%s, error=%s",
                repaired,
                repaired_error,
            )
            parsed = {
                "raw_content": content,
                "sanitized_content": repaired,
                "sanitize_error": "fallback_applied_callsite",
            }
            logger.warning("JSON parse fallback applied at call-site")
    return json.dumps(parsed, ensure_ascii=False)


def _process_single_record_for_lm(
    endpoint: str,
    model: str,
    body_text: str,
    category: str,
    timeout: int,
    max_tokens: int,
) -> tuple[str, dict[str, Any]]:
    """単一レコードのLM問い合わせ結果を返す（DB更新は行わない）。"""
    attempt_max_tokens = max(1, int(max_tokens))
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            json_text = _call_lmstudio(
                endpoint=endpoint,
                model=model,
                body_text=body_text,
                category=category,
                timeout=timeout,
                max_tokens=attempt_max_tokens,
            )
            break
        except RuntimeError as e:
            last_error = e
            # 応答JSONが途中で切れた場合を想定し、段階的に生成上限を拡張する。
            if "invalid JSON" in str(e) and attempt < 2:
                next_max_tokens = attempt_max_tokens * 2
                logger.warning(
                    "LM JSON parse retry: attempt=%s, category=%s, max_tokens=%s -> %s",
                    attempt + 1,
                    category,
                    attempt_max_tokens,
                    next_max_tokens,
                )
                attempt_max_tokens = next_max_tokens
                continue
            raise

    if last_error is not None and "json_text" not in locals():
        raise last_error

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
    max_tokens: int = 512,
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
        processed_count = 0
        with ThreadPoolExecutor(max_workers=normalized_workers) as executor:
            for record_id, body_text in records:
                future = executor.submit(
                    _process_single_record_for_lm,
                    endpoint,
                    model,
                    body_text,
                    category,
                    timeout,
                    max_tokens,
                )
                futures[future] = record_id
                logger.debug(f"LM処理を投入: table={table_name}, id={record_id}")

            for future in as_completed(futures):
                record_id = futures[future]
                processed_count += 1
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
                    logger.info(
                        f"LM処理成功: table={table_name}, id={record_id}, progress={processed_count}/{len(records)}"
                    )
                except Exception as e:
                    total_error += 1
                    logger.error(
                        f"LM処理失敗: table={table_name}, id={record_id}, progress={processed_count}/{len(records)}, error={e}"
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
