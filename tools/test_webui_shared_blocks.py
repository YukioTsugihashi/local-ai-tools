"""
Pipe/Tool間で写経している共通処理が、ファイルごとに腐っていないかを見る。

Open WebUIのPipeはコンテナ内で1ファイル単体として実行されるため、
リポジトリの共通モジュールをimportできない(open_webui_deploy.py も禁止している)。
つまり「ファイルを預けて会話へ添付する」処理は各ファイルへ写すしかない。

**消せない重複なので、ズレたら気づけることで代替する。**
1箇所だけ直して他を忘れると、ここが落ちる。実際に一度そうなっていた
(複数ファイル添付の修正が文章処理パイプにしか入っておらず、
 Excel分析では成果物と確認用レポートのうち1件しか画面に残らなかった)。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent
# 写経元。ここを直したら、写経先すべてへ同じ内容を反映する
CANONICAL = "open_webui_text_processing_pipe.py"
# プラグイン名を入れるラベル。ここだけはファイルごとに違ってよい
SOURCE_LABEL = re.compile(r"'source': '[a-z_]+'")


def _plugin_files() -> list[Path]:
    files = [
        path
        for path in TOOLS.glob("open*webui*.py")
        if not path.name.startswith("test_") and path.name != "open_webui_deploy.py"
    ]
    # 追加ツールの生成Pipeは additional-tools/ 側(Git管理外)にできるため、
    # 既存テストの走査に入らない。**生成元のひな型を代わりに見張る**
    # (ひな型がズレれば、以後に作られる全部の追加Pipeがズレる)
    template = TOOLS / "toolpack_pipe_template.py"
    if template.is_file():
        files.append(template)
    return sorted(files)


def _method_source(path: Path, name: str) -> str | None:
    """メソッド本体を、書式の揺れを消した形で取り出す。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            body = ast.unparse(node)
            # docstringの有無は差とみなさない(説明文はファイルごとに書いてよい)
            body = re.sub(r'"""[\s\S]*?"""', "", body, count=1)
            return SOURCE_LABEL.sub("'source': '<plugin>'", body).strip()
    return None


def _sharing_plugins(method: str, marker: str) -> dict[str, str]:
    """同じ方式を採っているファイルだけを集める(別方式のものは対象外)。"""
    found = {}
    for path in _plugin_files():
        source = _method_source(path, method)
        if source and marker in source:
            found[path.name] = source
    return found


@pytest.mark.parametrize(
    "method,marker",
    [
        # Open WebUIのファイルストアへ預ける処理
        ("_store_file", "upload_file_handler"),
        # 会話へ添付する処理。累積送信しないと複数添付が1件に潰れる
        ("_attach_file", "chat:message:files"),
    ],
)
def test_shared_block_is_identical_across_plugins(method: str, marker: str) -> None:
    sources = _sharing_plugins(method, marker)
    if len(sources) < 2:
        pytest.skip(f"{method} を同じ方式で持つプラグインが1つ以下です")
    assert CANONICAL in sources, f"{CANONICAL} に {method} がありません"

    different = sorted(name for name, src in sources.items() if src != sources[CANONICAL])
    assert not different, (
        f"{method} が写経先でズレています: {'、'.join(different)}\n"
        "Open WebUIのPipeは共通モジュールをimportできないため写経しているが、"
        f"1箇所だけ直すと他が古いまま残る。\n"
        f"{CANONICAL} の内容を、上記のファイルへそのまま反映してください。"
    )


def _emits_cumulative(source: str) -> bool:
    """
    累積送信を実際に行っているか。

    docstringや注釈に文字列があるだけでは通さない(説明だけ残して実装を
    落とした状態を見逃すため)。辞書リテラルの値として現れることを求める。
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for value in node.values:
            if isinstance(value, ast.Constant) and value.value == "chat:message:files":
                return True
    return False


def _attaches_inside_loop(source: str) -> bool:
    """ループの中で添付しているか(=複数ファイルを送りうるか)をASTで判定する。"""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.For, ast.AsyncFor)):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "_attach_file"
            ):
                return True
    return False


def test_multi_file_plugins_send_cumulative_list() -> None:
    """
    複数ファイルを添付するプラグインは、累積送信をしないと1件しか残らない。

    Open WebUIはfilesイベントを画面側で「置き換え」処理するため。
    ループの外で1回だけ添付するプラグインは対象外。
    """
    for path in _plugin_files():
        source = path.read_text(encoding="utf-8")
        if not _attaches_inside_loop(source):
            continue
        assert _emits_cumulative(source), (
            f"{path.name} はループ内で添付しているのに累積送信していない。"
            "画面には最後の1件しか残らない"
        )


def test_plugins_do_not_import_repository_modules() -> None:
    """
    Pipeはコンテナ内で単体実行されるため、リポジトリのモジュールは見えない。

    importを書くとデプロイは通っても実行時に落ちる。
    """
    forbidden = re.compile(r"^\s*(?:from|import)\s+(tools|app|local_tool_bridge)\b", re.M)
    for path in _plugin_files():
        source = path.read_text(encoding="utf-8")
        assert not forbidden.search(source), (
            f"{path.name} がリポジトリ内モジュールをimportしている。"
            "Pipeは自己完結で書く(必要な処理はハブ経由で呼ぶ)"
        )
