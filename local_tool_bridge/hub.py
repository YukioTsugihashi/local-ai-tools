"""
Open WebUIからKiloのローカルツールを呼ぶためのハブ。

このモジュールと `connectors/` は「受け取って渡すだけ」の役であり、
業務ロジック(データ解析・文章生成・Excel操作)を持ってはいけない。
処理本体は必ず `tools/` のスクリプトへ置き、ここからは `run_repo_script` で呼ぶ。
過去に接続役の中へ処理を書き写した結果、同じ機能が2箇所へ分裂して
経路ごとに結果が変わる事故が起きたため、この境界はテストで固定してある
(tools/test_local_tool_bridge.py の「ハブに業務ロジックを置かない」検査)。

新しいツールの繋ぎ方は .kilo/rules/09-hub-connector.md を参照。
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = ROOT / "text-processing-bridge" / ".venv" / "Scripts" / "python.exe"
# 1件の処理に許す時間。Excel1ブック29シートで数分かかることがある
SCRIPT_TIMEOUT_SECONDS = 1800
# サービスはpythonw(コンソール無し)で動くため、子プロセスを普通に起動すると
# 実行のたびに黒いコンソール窓が開く。利用者の画面を邪魔しないよう抑止する
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class RunContext:
    """接続役へ渡す実行条件。"""

    root: Path
    python: Path
    work_dir: Path
    inputs: list[Path]
    instruction: str
    # 操作の種類や検索語など、依頼文から取り出せない指定。
    # Pipe側が正規表現で判定した結果をそのまま渡す(LLMには判定させない)
    options: dict[str, str] = field(default_factory=dict)


@dataclass
class RunResult:
    """接続役が返す結果。files は呼び出し元へ返す成果物。"""

    files: list[Path] = field(default_factory=list)
    message: str = ""
    # 処理できなかったもの。利用者が対処する必要がある
    skipped: list[str] = field(default_factory=list)
    # 成功したうえでの補足。対処は要らないので skipped と混ぜない
    # (混ぜると、成功した処理が「処理できなかったもの」として出る)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ToolSpec:
    """接続するツール1つの宣言。"""

    name: str
    summary: str
    accepts: tuple[str, ...]
    run: Callable[[RunContext], RunResult]
    max_files: int = 1
    # 受け付けるオプション名。宣言していないキーは受付側で弾く
    options: tuple[str, ...] = ()


def run_repo_script(context: RunContext, script: str, args: list[str]) -> str:
    """
    tools/ のスクリプトを実行して標準出力を返す。

    接続役に許された唯一の実行手段。これ以外でsubprocessやHTTPを呼ぶと、
    処理本体がハブ側へ入り込み、Kilo側の実装と分裂する。
    """
    result = subprocess.run(
        [str(context.python), str(context.root / script), *args],
        cwd=context.root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=SCRIPT_TIMEOUT_SECONDS,
        check=False,
        creationflags=NO_WINDOW,
    )
    # 成功時はstdoutだけを返す(docstringどおり)。接続役は最終行をJSONとして
    # 読むため、stderrを混ぜると壊れる。openpyxlはグラフを含むブックで
    # UserWarning(chartのdata source)をstderrへ出すので、混ぜるとJSONの後ろに
    # 警告が並んでコメント生成が全滅する(2026-08-17に実発生)。
    if result.returncode:
        detail = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        raise RuntimeError(f"{script} が失敗しました:\n{detail[-4000:]}")
    return (result.stdout or "").strip()


def resolved_path(root: Path, value: str) -> Path:
    """スクリプトが返した相対パスをリポジトリ基準の絶対パスへ直す。"""
    path = Path(value)
    return path if path.is_absolute() else root / path


SAVED_PATH_PATTERN = re.compile(
    r"(?:保存しました(?:\([^\r\n]*\))?|CSV出力完了):\s*(.+\.(?:xlsx|xlsm|docx|pptx|csv|txt|md))",
    re.IGNORECASE,
)


def saved_paths(output: str, root: Path) -> list[Path]:
    """「保存しました...: パス」行から成果物のパスを拾う共通ヘルパ。"""
    return [
        resolved_path(root, match.group(1).strip())
        for match in SAVED_PATH_PATTERN.finditer(output)
    ]


def discover() -> tuple[dict[str, ToolSpec], list[str]]:
    """
    connectors/ 配下のモジュールを読み、SPEC と読み込めなかった理由を返す。

    ツール追加時に既存ファイルを編集させないための自動発見である。
    壊れた1枚で全部が繋がらなくなると原因を追えないため、
    失敗はそのファイルだけに閉じ込め、理由を持ち帰って画面へ出す。
    """
    from local_tool_bridge import connectors

    specs: dict[str, ToolSpec] = {}
    errors: list[str] = []
    for module in sorted(pkgutil.iter_modules(connectors.__path__), key=lambda item: item.name):
        try:
            loaded = importlib.import_module(f"{connectors.__name__}.{module.name}")
        except Exception as error:
            errors.append(f"{module.name}.py: 読み込みに失敗 ({type(error).__name__}: {error})")
            continue
        spec = getattr(loaded, "SPEC", None)
        if spec is None:
            errors.append(f"{module.name}.py: SPEC がありません")
            continue
        if not isinstance(spec, ToolSpec):
            errors.append(f"{module.name}.py: SPEC が ToolSpec ではありません")
            continue
        if spec.name in specs:
            errors.append(f"{module.name}.py: ツール名が重複しています ({spec.name})")
            continue
        specs[spec.name] = spec

    # 第2の供給源: registry に登録された追加ツール(TOOL_PACKAGE_DECISIONS.md D-4)。
    # 毎回読み直すので、追加・ロールバックがサービス再起動なしで反映される。
    # 名前が衝突したときは**既存の接続役を必ず残し、追加ツール側だけを拒否**する
    # (持ち込んだものが既存ツールを消す構造にしない)
    try:
        from local_tool_bridge import installed_specs

        added, added_errors = installed_specs.load()
    except Exception as error:
        added, added_errors = {}, [
            f"追加ツールを読み込めません ({type(error).__name__}: {error})"
        ]
    for name, spec in added.items():
        if name in specs:
            added_errors.append(
                f"追加ツール {name}: 既存の接続役と名前が衝突するため繋ぎません(既存を優先)"
            )
            continue
        specs[name] = spec
    errors.extend(added_errors)
    return specs, errors


def load_specs() -> dict[str, ToolSpec]:
    """繋がっているツールだけを返す(読み込めなかった理由は discover が持つ)。"""
    return discover()[0]


# 状態画面へ並べる周辺サービス。ハブを通らない経路も一箇所で見えるようにする
NEIGHBOUR_SERVICES: tuple[tuple[str, str, int], ...] = (
    ("文章処理ブリッジ", "127.0.0.1", 8008),
    ("Open WebUI", "127.0.0.1", 3000),
    ("Ollama", "127.0.0.1", 11434),
)


def port_is_open(host: str, port: int, timeout: float = 0.4) -> bool:
    """
    ポートが開いているかだけを見る。

    HTTPクライアントを使わないのは、ハブから業務的な呼び出しを
    できないようにしておくため(生死確認にはTCP接続で足りる)。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def neighbour_status() -> list[tuple[str, int, bool]]:
    """周辺サービスの生死。表示専用。"""
    return [
        (name, port, port_is_open(host, port)) for name, host, port in NEIGHBOUR_SERVICES
    ]


def backing_script(spec: ToolSpec) -> str:
    """接続役が呼んでいる tools/ のスクリプト名を、宣言のソースから拾う。"""
    try:
        source = inspect.getsource(spec.run)
    except (OSError, TypeError):
        return ""
    match = re.search(r'"(tools/[\w./-]+\.py)"', source)
    return match.group(1) if match else ""
