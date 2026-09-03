from __future__ import annotations

import pytest

from app.chunking import split_text, split_text_with_overlap


def test_short_text_is_returned_as_single_chunk() -> None:
    assert split_text("短い文章です。", 100) == ["短い文章です。"]


def test_chunks_reassemble_to_source() -> None:
    text = ("あ" * 50 + "。") * 40
    chunks = split_text(text, 500)

    assert "".join(chunks) == text
    assert len(chunks) > 1
    assert all(len(chunk) <= 500 for chunk in chunks)


def test_split_prefers_paragraph_boundary() -> None:
    text = "あ" * 300 + "。\n\n" + "い" * 300 + "。"
    chunks = split_text(text, 400)

    assert chunks[0].endswith("\n\n")
    assert chunks[1].startswith("い")


def test_split_falls_back_to_sentence_end() -> None:
    text = "短い文です。" * 200
    chunks = split_text(text, 100)

    assert "".join(chunks) == text
    assert all(chunk.endswith("。") for chunk in chunks[:-1])


def test_text_without_boundaries_is_hard_cut() -> None:
    text = "あ" * 250
    chunks = split_text(text, 100)

    assert chunks == ["あ" * 100, "あ" * 100, "あ" * 50]


def test_invalid_chunk_size_is_rejected() -> None:
    with pytest.raises(ValueError):
        split_text("文章", 0)


def test_overlap_prepends_previous_tail() -> None:
    lines = [f"発言{i}です。" for i in range(100)]
    text = "\n".join(lines)
    chunks = split_text_with_overlap(text, 300, 60)

    assert len(chunks) > 1
    base = split_text(text, 300)
    # 先頭ブロックは元のまま。以降は直前ブロック末尾の行が先頭に重なる。
    assert chunks[0] == base[0]
    for i in range(1, len(base)):
        assert chunks[i].endswith(base[i])
        overlap = chunks[i][: len(chunks[i]) - len(base[i])]
        assert overlap  # 重ね部分が付いている
        assert base[i - 1].endswith(overlap)  # 直前ブロックの末尾と一致
        assert not overlap.startswith("\n")  # 行頭に揃っている


def test_overlap_zero_matches_plain_split() -> None:
    text = ("短い文です。" * 30 + "\n") * 20
    assert split_text_with_overlap(text, 500, 0) == split_text(text, 500)


def test_overlap_single_chunk_unchanged() -> None:
    assert split_text_with_overlap("短い文章です。", 100, 500) == ["短い文章です。"]
