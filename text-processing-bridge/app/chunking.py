from __future__ import annotations

import re

PARAGRAPH_BREAK = re.compile(r"\n{2,}")
SENTENCE_ENDINGS = ("。", "！", "？", "!", "?")


def split_text(text: str, chunk_chars: int) -> list[str]:
    """
    長文をchunk_chars以内のブロックへ分割する。

    分割位置は段落境界、行境界、文末の優先順で探し、どれも無い場合だけ
    chunk_charsの位置で切る。全ブロックを結合すると必ず元の文章に戻る。
    """
    if chunk_chars <= 0:
        raise ValueError("chunk_charsは1以上にしてください。")
    if len(text) <= chunk_chars:
        return [text]

    minimum_cut = max(1, chunk_chars // 2)
    chunks: list[str] = []
    rest = text
    while len(rest) > chunk_chars:
        cut = _find_cut_position(rest[:chunk_chars], minimum_cut)
        chunks.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        chunks.append(rest)
    return chunks


def split_text_with_overlap(
    text: str, chunk_chars: int, overlap_chars: int
) -> list[str]:
    """
    各ブロックの先頭に、直前ブロックの末尾を文脈として重ねて返す。

    分割境界をまたぐ発言の取りこぼしを減らすための議事録抽出向け。
    重ねた部分は両方のブロックから抽出され得るが、統合側の近似重複除去が
    吸収する。結合しても原文に戻らないため、ケバ取り・整形のような
    「結果を連結して原文全体を再構成する」用途には使えない。
    """
    base = split_text(text, chunk_chars)
    if overlap_chars <= 0 or len(base) < 2:
        return base

    result = [base[0]]
    for previous, current in zip(base, base[1:]):
        tail = previous[-overlap_chars:]
        # 発言の途中から始まらないよう、重ね部分は行頭に揃える。
        newline = tail.find("\n")
        if 0 <= newline < len(tail) - 1:
            tail = tail[newline + 1 :]
        result.append(tail + current if tail.strip() else current)
    return result


def _find_cut_position(window: str, minimum_cut: int) -> int:
    paragraph_cut = None
    for match in PARAGRAPH_BREAK.finditer(window):
        if match.end() >= minimum_cut:
            paragraph_cut = match.end()
    if paragraph_cut is not None:
        return paragraph_cut

    newline_cut = window.rfind("\n") + 1
    if newline_cut >= minimum_cut:
        return newline_cut

    for index in range(len(window) - 1, minimum_cut - 2, -1):
        if window[index] in SENTENCE_ENDINGS:
            return index + 1

    return len(window)
