"""パッケージ同梱の単体テスト(受入時に隔離環境で実行される)。

テストからは自分のパッケージを import できる。
実データは使わず、その場で作った文字列だけで確かめる。
"""

from __future__ import annotations

import json
import pathlib

PACKAGE = pathlib.Path(__file__).resolve().parents[1]


def test_宣言と実装が食い違っていない() -> None:
    meta = json.loads((PACKAGE / "tool.json").read_text(encoding="utf-8"))
    assert meta["id"] == "example_line_count"
    assert meta["outputs"]["produces"] == [".txt"]


def test_受け入れる形式がコア側の対応一覧と揃っている() -> None:
    """読み取りはコアの toolpack_textio が行う。
    宣言だけ広げても読めないし、狭いままだと読めるのに断ってしまう。"""
    import toolpack_textio

    meta = json.loads((PACKAGE / "tool.json").read_text(encoding="utf-8"))
    assert meta["inputs"]["accepts"] == toolpack_textio.supported_suffixes()


def test_依頼例が判定語に一致する() -> None:
    import re

    meta = json.loads((PACKAGE / "tool.json").read_text(encoding="utf-8"))
    patterns = [re.compile(p) for p in meta["routing"]["patterns"]]
    for example in meta["routing"]["examples"]:
        assert any(p.search(example) for p in patterns), example
