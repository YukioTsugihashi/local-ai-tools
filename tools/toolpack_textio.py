r"""添付ファイルを「文字」として読むための、コア側の共通部品。

設計の正本は TOOL_PACKAGE_DECISIONS.md(D-5・D-9)。

**なぜコア側に置くか**

利用者が持ってくるのは `.txt` ばかりではない。実際、院内の文字起こしは
25件すべて `.docx` だった(台帳 5-20)。各パッケージが自前で読み方を書くと、
形式ごとの対応がツールの数だけ散らばり、直すのも数え上げるのも難しくなる。
そこで**読み取りだけをコアが持ち**、パッケージからは1行で使えるようにする。

**どこで動くか**

このファイルはランナーがジョブ直下へ複製し、隔離した子プロセスの中で
読み込まれる。つまり **`.docx` などの解析も隔離の中で行われる**。
利用者が持ち込んだファイルを、権限のある親プロセスで解析しない。
ジョブ直下は読めるが書けないため、ツール側から差し替えることはできない。

**方針**

- 読めない形式は**推測しない**。何の形式で、どうすれば読めるかを返す
- 文字コードは判定する。判定できなければ、その旨を返して止まる
- 表や発表資料は「文字が拾える範囲で」拾う。体裁は再現しない
"""

from __future__ import annotations

import json
import os
import re
import zipfile

# 文字としてそのまま読める形式(文字コードの判定だけ行う)
PLAIN_SUFFIXES = (
    ".txt", ".text", ".md", ".markdown", ".csv", ".tsv",
    ".log", ".json", ".yaml", ".yml", ".ini", ".rst",
)
# 字幕・文字起こしの形式(時刻の行を落として本文だけにする)
CAPTION_SUFFIXES = (".vtt", ".srt")
# しるし付きの文書(タグを落として本文だけにする)
MARKUP_SUFFIXES = (".html", ".htm", ".xml")
# Office 形式
WORD_SUFFIXES = (".docx",)
SLIDE_SUFFIXES = (".pptx",)
BOOK_SUFFIXES = (".xlsx", ".xlsm")
OLD_BOOK_SUFFIXES = (".xls",)

SUPPORTED_SUFFIXES = (
    PLAIN_SUFFIXES + CAPTION_SUFFIXES + MARKUP_SUFFIXES
    + WORD_SUFFIXES + SLIDE_SUFFIXES + BOOK_SUFFIXES + OLD_BOOK_SUFFIXES
)

# 読めないと分かっている形式。**黙って失敗させず、直し方を示す**
KNOWN_UNSUPPORTED = {
    ".pdf": "PDFは読めません。元のWordやテキストを添付してください。",
    ".doc": "古い形式のWordです。Wordで開いて .docx として保存し直してください。",
    ".ppt": "古い形式のPowerPointです。開いて .pptx として保存し直してください。",
    ".rtf": "RTFは読めません。Wordで開いて .docx として保存し直してください。",
    ".odt": "ODTは読めません。Wordで開いて .docx として保存し直してください。",
    ".pages": "Pages形式は読めません。Wordで開ける形式に書き出してください。",
    ".msg": "Outlookのメール形式は読めません。本文をテキストに貼り付けてください。",
    ".eml": "メール形式は読めません。本文をテキストに貼り付けてください。",
    ".zip": "圧縮ファイルは読めません。中のファイルを取り出して添付してください。",
    ".7z": "圧縮ファイルは読めません。中のファイルを取り出して添付してください。",
}

# 読み込む上限。これを超える添付は、途中まで読んで黙って続けたりしない
MAX_BYTES = 100 * 1024 * 1024
# 文字コードの判定に使う先頭部分
SNIFF_BYTES = 256 * 1024

_VTT_CUE = re.compile(r"^\s*(\d+\s*$|WEBVTT|NOTE\b|STYLE\b|REGION\b)")
_TIMECODE = re.compile(r"\d{1,2}:\d{2}(:\d{2})?[.,]\d{1,3}\s*-->")


class UnreadableFile(Exception):
    """読めなかった。**利用者がそのまま読める日本語で理由と直し方を持つ。**"""


def supported_suffixes() -> list[str]:
    """tool.json の inputs.accepts へそのまま書ける一覧。"""
    return list(SUPPORTED_SUFFIXES)


def _decode(raw: bytes, filename: str) -> str:
    """文字コードを判定して復号する。判定できなければ推測せずに止める。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")

    try:
        from charset_normalizer import from_bytes

        best = from_bytes(raw[:SNIFF_BYTES]).best()
    except Exception:
        best = None
    if best is not None and best.encoding:
        try:
            return raw.decode(best.encoding, errors="strict")
        except (UnicodeDecodeError, LookupError):
            pass

    # 判定に失敗したときだけ、日本語で実際に使われる順に試す
    for encoding in ("utf-8", "cp932", "euc_jp", "utf-16"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    raise UnreadableFile(
        f"「{filename}」の文字コードを判別できませんでした。"
        "UTF-8 で保存し直して、もう一度お試しください。"
    )


def _read_plain(path: str, filename: str) -> str:
    with open(path, "rb") as handle:
        return _decode(handle.read(), filename)


def _read_caption(path: str, filename: str) -> str:
    """字幕・文字起こし形式から、時刻や連番を落として本文だけを取り出す。"""
    lines = []
    previous = None
    for line in _read_plain(path, filename).splitlines():
        stripped = line.strip()
        if not stripped or _TIMECODE.search(stripped) or _VTT_CUE.match(stripped):
            continue
        # 同じ発話が繰り返し出力される字幕があるため、直前と同じ行は落とす
        if stripped == previous:
            continue
        previous = stripped
        lines.append(stripped)
    return "\n".join(lines)


def _read_markup(path: str, filename: str) -> str:
    source = _read_plain(path, filename)
    try:
        from bs4 import BeautifulSoup
    except Exception as error:  # pragma: no cover - 同梱済みだが保険
        raise UnreadableFile(
            f"「{filename}」を読むための部品がありません({type(error).__name__})。"
        ) from error
    soup = BeautifulSoup(source, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text("\n")


def _read_word(path: str, filename: str) -> str:
    """Word文書。段落と表の中身を、出てくる順に近い形で拾う。"""
    try:
        import docx
    except Exception as error:  # pragma: no cover
        raise UnreadableFile(
            f"「{filename}」を読むための部品がありません({type(error).__name__})。"
        ) from error
    try:
        document = docx.Document(path)
    except Exception as error:
        raise UnreadableFile(
            f"「{filename}」をWord文書として開けませんでした。"
            "ファイルが壊れていないか、パスワードが掛かっていないか確かめてください。"
        ) from error

    parts = [paragraph.text.strip() for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            # 結合セルは同じ文字が並ぶので、続く重複だけ畳む
            merged = [
                cell for index, cell in enumerate(cells)
                if cell and (index == 0 or cell != cells[index - 1])
            ]
            if merged:
                parts.append("\t".join(merged))
    return "\n".join(part for part in parts if part)


def _read_slides(path: str, filename: str) -> str:
    """発表資料。各ページの文字と、ノート欄を拾う。"""
    try:
        from pptx import Presentation
    except Exception as error:  # pragma: no cover
        raise UnreadableFile(
            f"「{filename}」を読むための部品がありません({type(error).__name__})。"
        ) from error
    try:
        presentation = Presentation(path)
    except Exception as error:
        raise UnreadableFile(
            f"「{filename}」をPowerPointとして開けませんでした。"
        ) from error

    parts = []
    for number, slide in enumerate(presentation.slides, start=1):
        parts.append(f"--- {number}ページ ---")
        for shape in slide.shapes:
            text = getattr(shape, "text", "") or ""
            if text.strip():
                parts.append(text.strip())
        notes = getattr(slide, "notes_slide", None) if slide.has_notes_slide else None
        note_text = getattr(getattr(notes, "notes_text_frame", None), "text", "") or ""
        if note_text.strip():
            parts.append(f"(ノート) {note_text.strip()}")
    return "\n".join(parts)


def _read_book(path: str, filename: str) -> str:
    """Excel。シートごとに、値の入っているセルをタブ区切りで並べる。"""
    try:
        from openpyxl import load_workbook
    except Exception as error:  # pragma: no cover
        raise UnreadableFile(
            f"「{filename}」を読むための部品がありません({type(error).__name__})。"
        ) from error
    try:
        book = load_workbook(path, data_only=True, read_only=True)
    except Exception as error:
        raise UnreadableFile(
            f"「{filename}」をExcelとして開けませんでした。"
            "パスワードが掛かっていないか確かめてください。"
        ) from error

    parts = []
    try:
        for sheet in book.worksheets:
            parts.append(f"--- {sheet.title} ---")
            for row in sheet.iter_rows(values_only=True):
                cells = ["" if value is None else str(value).strip() for value in row]
                while cells and not cells[-1]:
                    cells.pop()
                if any(cells):
                    parts.append("\t".join(cells))
    finally:
        book.close()
    return "\n".join(parts)


def _read_old_book(path: str, filename: str) -> str:
    try:
        import xlrd
    except Exception as error:  # pragma: no cover
        raise UnreadableFile(
            f"「{filename}」を読むための部品がありません({type(error).__name__})。"
        ) from error
    try:
        book = xlrd.open_workbook(path)
    except Exception as error:
        raise UnreadableFile(
            f"「{filename}」を古い形式のExcelとして開けませんでした。"
            ".xlsx として保存し直すと読めることがあります。"
        ) from error

    parts = []
    for sheet in book.sheets():
        parts.append(f"--- {sheet.name} ---")
        for index in range(sheet.nrows):
            cells = ["" if value is None else str(value).strip() for value in sheet.row_values(index)]
            while cells and not cells[-1]:
                cells.pop()
            if any(cells):
                parts.append("\t".join(cells))
    return "\n".join(parts)


_READERS = (
    (PLAIN_SUFFIXES, _read_plain),
    (CAPTION_SUFFIXES, _read_caption),
    (MARKUP_SUFFIXES, _read_markup),
    (WORD_SUFFIXES, _read_word),
    (SLIDE_SUFFIXES, _read_slides),
    (BOOK_SUFFIXES, _read_book),
    (OLD_BOOK_SUFFIXES, _read_old_book),
)


def read_text(path: str, filename: str | None = None) -> str:
    """添付ファイルを文字として読む。読めなければ UnreadableFile を投げる。

    filename は利用者へ見せる名前(ジョブ内の内部名ではなく元の名前)。
    """
    display = filename or os.path.basename(path)
    suffix = os.path.splitext(display)[1].lower()
    if not suffix:
        suffix = os.path.splitext(path)[1].lower()

    if not os.path.isfile(path):
        raise UnreadableFile(f"「{display}」が見つかりませんでした。")
    size = os.path.getsize(path)
    if size == 0:
        raise UnreadableFile(f"「{display}」は中身が空でした。")
    if size > MAX_BYTES:
        raise UnreadableFile(
            f"「{display}」は {size / 1024 / 1024:.0f}MB あり、"
            f"上限の {MAX_BYTES // 1024 // 1024}MB を超えています。"
        )

    if suffix in KNOWN_UNSUPPORTED:
        raise UnreadableFile(f"「{display}」: {KNOWN_UNSUPPORTED[suffix]}")

    # 拡張子と中身が食い違うことは実際にある(.docx を .txt へ付け替える等)。
    # 中身で分かるなら、文字コードの話にすり替えずに本当の理由を返す
    actual = _content_kind(path)
    if actual and actual != _SUFFIX_KIND.get(suffix, ""):
        raise UnreadableFile(f"「{display}」: {_KIND_ADVICE[actual]}")

    for suffixes, reader in _READERS:
        if suffix in suffixes:
            text = reader(path, display)
            if not text.strip():
                raise UnreadableFile(
                    f"「{display}」から文字を取り出せませんでした。"
                    "画像だけの資料ではないか確かめてください。"
                )
            return text

    raise UnreadableFile(
        f"「{display}」({suffix or '拡張子なし'})は対応していない形式です。"
        f"対応しているのは {'、'.join(SUPPORTED_SUFFIXES)} です。"
    )


# 中身から分かる形式と、そのときの案内
_KIND_ADVICE = {
    "pdf": "中身はPDFでした。PDFは読めません。元のWordやテキストを添付してください。",
    "ole": "中身は古いOffice形式でした。.docx / .xlsx / .pptx で保存し直してください。",
    "word": "中身はWord文書でした。拡張子を .docx にしてください。",
    "slides": "中身はPowerPointでした。拡張子を .pptx にしてください。",
    "book": "中身はExcelでした。拡張子を .xlsx にしてください。",
    "zip": "中身は圧縮ファイルでした。取り出して添付してください。",
}
# 拡張子から期待される中身(食い違ったときだけ知らせるため)
_SUFFIX_KIND = {
    **{suffix: "word" for suffix in WORD_SUFFIXES},
    **{suffix: "slides" for suffix in SLIDE_SUFFIXES},
    **{suffix: "book" for suffix in BOOK_SUFFIXES},
    **{suffix: "ole" for suffix in OLD_BOOK_SUFFIXES},
}


def _content_kind(path: str) -> str:
    """中身の先頭から形式を見分ける。分からなければ空文字。"""
    try:
        with open(path, "rb") as handle:
            head = handle.read(8)
    except OSError:
        return ""
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "ole"
    if not head.startswith(b"PK\x03\x04"):
        return ""
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
    except Exception:
        return "zip"
    if "word/document.xml" in names:
        return "word"
    if any(name.startswith("ppt/") for name in names):
        return "slides"
    if any(name.startswith("xl/") for name in names):
        return "book"
    return "zip"


def read_request_input(request: dict, job_dir: str, index: int = 0) -> tuple[str, str]:
    """request.json の入力を1件読み、(本文, 表示名)を返す。

    パッケージ側で毎回同じ組み立てを書かなくて済むようにするための入口。
    """
    inputs = request.get("inputs") or []
    if index >= len(inputs):
        raise UnreadableFile("ファイルが添付されていません。")
    entry = inputs[index]
    display = entry.get("filename") or os.path.basename(entry.get("path", ""))
    return read_text(os.path.join(job_dir, entry["path"]), display), display


def main() -> int:  # pragma: no cover - 手元で中身を確かめるための補助
    import argparse

    parser = argparse.ArgumentParser(description="添付ファイルを文字として読む")
    parser.add_argument("path")
    parser.add_argument("--json", action="store_true", help="JSONで出す")
    args = parser.parse_args()
    try:
        text = read_text(args.path)
    except UnreadableFile as error:
        print(json.dumps({"ok": False, "message": str(error)}, ensure_ascii=False))
        return 1
    if args.json:
        print(json.dumps({"ok": True, "text": text}, ensure_ascii=False))
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
