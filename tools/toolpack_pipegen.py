r"""tool.json から追加ツールの Pipe を生成する(固定ひな型への差し込みだけ)。

設計の正本は TOOL_PACKAGE_DECISIONS.md(D-4・D-12)。

- 生成元は `tools/toolpack_pipe_template.py` **だけ**。
  オンラインAIにPipe本体を書かせない
- 差し込めるのは**検証済みの宣言値だけ**(表示名・ID・説明・入力条件・判定語・依頼例)
- **新規追加モデルは常に「未承認」として作る。**
  承認状態の切り替えは D-14 以降で、ここでは先取りしない
- 生成先は `additional-tools/installed/<id>/<version>/generated/`(Git管理外)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "tools" / "toolpack_pipe_template.py"

# 未承認であることを見て分かるようにする接頭辞(台帳 D-12)。
# パッケージ側には書かせず、生成時に必ずここで付ける
UNAPPROVED_PREFIX = "【未承認】"

ID_PATTERN = re.compile(r"^[a-z0-9_]{3,32}$")


class PipeGenError(RuntimeError):
    """生成できない。理由と対処を含める。"""


def pipe_filename(tool_id: str) -> str:
    return f"open_webui_{tool_id}_pipe.py"


def _escaped(value: str) -> str:
    """ひな型のPython文字列リテラルへ安全に差し込める形にする。"""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").strip()


def render(tool_json: dict, *, approved: bool = False, template: str | None = None) -> str:
    """tool.json から Pipe のソースを作る。

    approved は D-14 以降で使う。**初期版の呼び出しは常に False**(未承認)。
    """
    tool_id = tool_json.get("id", "")
    if not ID_PATTERN.match(tool_id):
        raise PipeGenError(f"tool.json の id が規約に合いません: {tool_id!r}")

    routing = tool_json.get("routing") or {}
    patterns = routing.get("patterns") or []
    examples = routing.get("examples") or []
    suggestions = routing.get("suggestions") or []
    inputs = tool_json.get("inputs") or {}
    accepts = [str(item).lower() for item in (inputs.get("accepts") or [])]
    if not patterns or not accepts:
        raise PipeGenError("routing.patterns と inputs.accepts が要ります")

    display_name = str(tool_json.get("display_name") or tool_id)
    if not approved:
        display_name = f"{UNAPPROVED_PREFIX}{display_name}"

    source = template if template is not None else TEMPLATE_PATH.read_text(encoding="utf-8")

    # docstring(Open WebUIのモデル画面に出る情報)を作り直す
    suggestion_lines = []
    for item in suggestions:
        title = _escaped(str(item.get("title", "")))
        subtitle = _escaped(str(item.get("subtitle", "")))
        content = _escaped(str(item.get("content", "")))
        suggestion_lines.append(f"suggestion: {title} | {subtitle} | {content}")
    if not suggestion_lines:
        for example in examples:
            suggestion_lines.append(f"suggestion: 実行 | 依頼の例 | {_escaped(str(example))}")

    summary = _escaped(str(tool_json.get("summary") or tool_id))
    header = "\n".join([
        '"""',
        f"title: {display_name}",
        f"id: {tool_id}",
        "author: local",
        f"version: {tool_json.get('version', '1.0.0')}",
        f"description: {summary}",
        f"model_description: {summary}",
        *suggestion_lines,
        "requirements: httpx",
        "",
        "このファイルは tools/toolpack_pipe_template.py から自動生成されている。",
        "直接編集しない(次の生成で失われる)。直すならひな型か tool.json を直す。",
        '"""',
    ])
    source = re.sub(r'^"""[\s\S]*?"""', header, source, count=1)

    # 判定語は「いずれかに一致」でまとめる。静的リテラルのまま保つことで、
    # open_webui_deploy.py のチップ照合が生成後も効き続ける(台帳 D-4)
    combined = "|".join(f"(?:{pattern})" for pattern in patterns)
    # 改行はソース上の \n(エスケープ表記)として差し込む。実際の改行を入れると
    # ひな型の文字列リテラルが壊れ、逆に空白へ潰すと案内が1行に潰れる
    example_lines = "\\n".join(f"  - {_escaped(str(item))}" for item in examples) or "  - 依頼を書いてください"
    replacements = {
        "__TOOL_ID__": tool_id,
        "__TOOL_TITLE__": _escaped(display_name),
        "__TOOL_SUMMARY__": summary,
        "__TOOL_ACCEPTS_TEXT__": _escaped("、".join(accepts)),
        "__TOOL_EXAMPLES__": example_lines,
        "__TOOL_PATTERN__": combined.replace("\\", "\\\\"),
    }
    for token, value in replacements.items():
        source = source.replace(token, value)

    accepts_literal = ", ".join(f'"{suffix}"' for suffix in accepts)
    source = re.sub(
        r"^ACCEPTED_SUFFIXES = \(.*?\)$",
        f"ACCEPTED_SUFFIXES = ({accepts_literal},)" if len(accepts) == 1
        else f"ACCEPTED_SUFFIXES = ({accepts_literal})",
        source, count=1, flags=re.MULTILINE,
    )
    max_files = int(inputs.get("max") or 1)
    source = re.sub(
        r"^MAX_FILES = .*$", f"MAX_FILES = {max_files}", source, count=1, flags=re.MULTILINE
    )
    # 写経元ラベルは生成物ごとに変えてよい唯一の箇所
    source = source.replace("'source': 'toolpack_template'", f"'source': '{tool_id}'")
    source = source.replace('"source": "toolpack_template"', f'"source": "{tool_id}"')
    return source


def generate(package_dir: Path, generated_dir: Path, *, approved: bool = False) -> Path:
    """パッケージの tool.json から Pipe を生成し、generated/ へ書いて返す。"""
    tool_json = json.loads((Path(package_dir) / "tool.json").read_text(encoding="utf-8"))
    source = render(tool_json, approved=approved)
    generated_dir = Path(generated_dir)
    generated_dir.mkdir(parents=True, exist_ok=True)
    target = generated_dir / pipe_filename(tool_json["id"])
    target.write_text(source, encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="tool.json から追加ツールのPipeを生成する")
    parser.add_argument("package", help="パッケージ(tool.json のある)ディレクトリ")
    parser.add_argument("--out", required=True, help="生成先の generated/ ディレクトリ")
    args = parser.parse_args(argv)
    target = generate(Path(args.package), Path(args.out))
    print(f"生成しました: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
