"""文字起こしの健康診断。議事録にする前に、直すべき箇所を洗い出す。

LLMは使わない。**規則で分かることだけ**を機械的に数える。
「たぶんこうだろう」という推測はしない(推測すると、直す必要のない箇所まで
直させてしまい、かえって手間が増えるため)。

見るもの:
  1. 発言者が分からない行
  2. 同じ内容の繰り返し
  3. 長すぎて読みにくいかたまり
  4. 数値・日時・担当らしき記述(転記ミスが起きやすいので目視してほしい箇所)
  5. 文字化け・記号の乱れ
"""

from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata

# 発言者らしき書き出し。「山田:」「山田さん」「○○課長」など
SPEAKER = re.compile(
    r"^\s*(?:[○◯●・\-*]\s*)?[^\s:：]{1,20}\s*(?:[:：]|さん|氏|課長|部長|主任|先生)"
)
# 読みにくさの目安。日本語の議事録で1文が長すぎると確認が難しくなる
LONG_CHUNK_CHARS = 200
# 数値・日時・担当らしき記述(転記ミスが起きやすい)。
# 「第5回」のような序数は回数の情報ではないので拾わない(誤検出を減らす)
NUMBERISH = re.compile(
    r"(?<!第)\d+\s*(?:年|月|日|時|分|件|名|人|円|%|％|割|回|床|階)|"
    r"\d{1,2}\s*[:：]\s*\d{2}|"
    r"(?:担当|責任者|窓口)\s*[:：はがのを]?\s*\S+"
)
# 発言らしさの目安。句読点で終わる/含む行だけを発言とみなす。
# 見出し・タイトル行(「第5回 運営会議」など)を発言者不明として指摘しないため
UTTERANCE = re.compile(r"[。、．，.?!？!]")
# 文字化け。置換文字(U+FFFD)と、本文には現れない制御文字
BROKEN_MARKS = re.compile("[�\x00-\x08\x0b\x0c\x0e-\x1f]")
# 「、、、、」のような記号の繰り返し(音声認識の取りこぼしで出やすい)
NOISY_PUNCT = re.compile(r"([、。,\.\-ー―…])\1{3,}")


def _normalized(line: str) -> str:
    """重複判定用に、表記の揺れを均す。"""
    text = unicodedata.normalize("NFKC", line)
    return re.sub(r"[\s、。,\.!?！？]+", "", text)


def _excerpt(line: str, limit: int = 30) -> str:
    """報告に載せる抜粋。**必要最小限だけ**にする。"""
    text = line.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def find_unknown_speaker(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """発言らしいのに発言者が分からない行。

    短い相槌や見出し行まで拾うと指摘だらけになって使えない。
    ある程度の長さがあり、句読点を含む行だけを発言とみなす。
    そのぶん、句読点の無い発言は見逃す(誤検出を減らす側に倒している)。
    """
    return [
        (number, line) for number, line in lines
        if len(line.strip()) >= 10
        and UTTERANCE.search(line)
        and not SPEAKER.match(line)
    ]


def find_duplicates(lines: list[tuple[int, str]]) -> list[tuple[int, int, str]]:
    """同じ内容が繰り返されている箇所。(前の行番号, 後の行番号, 元の行)。"""
    seen: dict[str, int] = {}
    found: list[tuple[int, int, str]] = []
    for number, line in lines:
        key = _normalized(line)
        if len(key) < 12:
            continue  # 短い相槌は重複として扱わない
        if key in seen:
            found.append((seen[key], number, line))
        else:
            seen[key] = number
    return found


def find_long_chunks(lines: list[tuple[int, str]]) -> list[tuple[int, int]]:
    """長すぎて読みにくいかたまり。(行番号, 文字数)。"""
    return [
        (number, len(line.strip()))
        for number, line in lines
        if len(line.strip()) > LONG_CHUNK_CHARS
    ]


def find_numberish(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """数値・日時・担当らしき記述(目視してほしい箇所)。"""
    found: list[tuple[int, str]] = []
    for number, line in lines:
        match = NUMBERISH.search(line)
        if match:
            found.append((number, match.group(0).strip()))
    return found


def find_broken_text(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """文字化け・記号の乱れ。"""
    found: list[tuple[int, str]] = []
    for number, line in lines:
        if BROKEN_MARKS.search(line):
            found.append((number, "文字化けらしき記号"))
        elif NOISY_PUNCT.search(line):
            found.append((number, "記号の繰り返し"))
    return found


def diagnose(text: str) -> tuple[str, list[str], list[str]]:
    """診断して (報告文, 対処が要るもの, 補足) を返す。

    対処が要るもの(skipped)と補足(notes)を混ぜない。混ぜると、直さなくてよい
    ものまで「問題」に見えてしまう。
    """
    raw_lines = text.splitlines()
    lines = [
        (number, line) for number, line in enumerate(raw_lines, start=1)
        if line.strip()
    ]
    if not lines:
        return "中身が空でした。", ["文字起こしの中身がありません"], []

    unknown = find_unknown_speaker(lines)
    duplicates = find_duplicates(lines)
    long_chunks = find_long_chunks(lines)
    numberish = find_numberish(lines)
    broken = find_broken_text(lines)

    report = [
        f"文字起こしを診断しました(全 {len(raw_lines)} 行 / 中身のある行 {len(lines)} 行)。",
        "",
    ]
    skipped: list[str] = []
    notes: list[str] = []

    def section(title: str, count: int, samples: list[str]) -> None:
        report.append(f"■ {title}: {count} 件")
        report.extend(f"    {sample}" for sample in samples[:3])
        if count > 3:
            report.append(f"    ほか {count - 3} 件")
        report.append("")

    section("発言者が分からない行", len(unknown),
            [f"{number}行目: {_excerpt(line)}" for number, line in unknown])
    section("同じ内容の繰り返し", len(duplicates),
            [f"{first}行目と{second}行目: {_excerpt(line)}"
             for first, second, line in duplicates])
    section("長すぎて読みにくいかたまり", len(long_chunks),
            [f"{number}行目: {size} 文字" for number, size in long_chunks])
    section("数値・日時・担当の記述(目視してください)", len(numberish),
            [f"{number}行目: {value}" for number, value in numberish])
    section("文字化け・記号の乱れ", len(broken),
            [f"{number}行目: {reason}" for number, reason in broken])

    # 対処が要るもの(直さないと議事録にできない)
    if unknown:
        skipped.append(f"発言者が分からない行が {len(unknown)} 件あります(誰の発言か補ってください)")
    if broken:
        skipped.append(f"文字化けらしき箇所が {len(broken)} 件あります(元の音声で確認してください)")
    if duplicates:
        skipped.append(f"同じ内容の繰り返しが {len(duplicates)} 件あります(重複を消してください)")

    # 対処が要らない補足(誤りとは限らない)
    if long_chunks:
        notes.append(f"長いかたまりが {len(long_chunks)} 件あります(読みやすさの問題で、誤りとは限りません)")
    if numberish:
        notes.append(f"数値・日時・担当の記述が {len(numberish)} 件あります(転記ミスが起きやすい箇所です)")
    if not skipped:
        report.append("対処が必要な問題は見つかりませんでした。")

    return "\n".join(report).rstrip(), skipped, notes


def main() -> int:
    parser = argparse.ArgumentParser(description="文字起こしの健康診断")
    parser.add_argument("--request", required=True)
    args = parser.parse_args()

    request = json.loads(open(args.request, encoding="utf-8").read())
    job_dir = os.path.dirname(args.request)

    # 読み取りはコア側の共通部品に任せる(D-5)。Word・字幕・Excelなど、
    # 対応形式が増えてもここは変わらない。読めないときは理由と直し方が返る
    import toolpack_textio

    try:
        text, _ = toolpack_textio.read_request_input(request, job_dir)
    except toolpack_textio.UnreadableFile as error:
        print(json.dumps({
            "status": "user_error", "message": str(error),
            "files": [], "skipped": [], "notes": [],
        }, ensure_ascii=False))
        return 0

    message, skipped, notes = diagnose(text)
    print(json.dumps({
        "status": "ok", "message": message, "files": [],
        "skipped": skipped, "notes": notes,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
