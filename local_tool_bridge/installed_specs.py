"""
registry に登録された追加ツールを ToolSpec として供給する。

設計の正本は TOOL_PACKAGE_DECISIONS.md(D-3・D-4・D-9)。

- **registry.json だけが有効版の正本。** ここは呼ばれるたびに読み直すので、
  追加・更新・ロールバックがサービス再起動なしで反映される
- 壊れた項目は**そのパッケージだけ**を errors へ隔離する(1枚で全部が消えない)
- 実行は必ず `tools/toolpack_runner.py`(隔離ランナー)へ委譲する。
  **ここには業務ロジックも実行手段も持たない**(ハブの境界。09-hub-connector.md)

`tools/` を sys.path へ足しているのは、doctor.py が
`open_webui_deploy` を読むのと同じやり方である(tools/ はパッケージではない)。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from local_tool_bridge.hub import ROOT, RunContext, RunResult, ToolSpec

_TOOLS_DIR = ROOT / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

# テストが実領域(additional-tools/)へ触れずに済むよう差し替えられるようにする
STORE_ROOT: Path | None = None

# 失敗時に利用者へ返す言い換え。**生の stderr は載せない**(台帳 D-9)
_ADVICE = {
    "E-TIMEOUT": "制限時間内に終わりませんでした。対象を小さくして試してください",
    "E-DENIED": "ツールが許可されていない操作を行おうとしたため中止しました",
    "E-ISOLATION": "隔離実行を準備できませんでした。管理者へ連絡してください",
    "E-SPAWN": "ツールを起動できませんでした。管理者へ連絡してください",
    "E-EMPTY": "ツールが結果を返しませんでした",
    "E-MULTILINE": "ツールが余計な出力を行いました",
    "E-JSON": "ツールの結果を読み取れませんでした",
}


def _make_store():
    import toolpack_store

    return toolpack_store.ToolpackStore(STORE_ROOT or toolpack_store.DEFAULT_STORE_ROOT)


def _record_failure(store, tool_id: str, version: str, code: str, report) -> None:
    """診断を1行だけ残す。本文・氏名・元ファイル名・生の出力は書かない(台帳 D-9)。"""
    try:
        store.logs.mkdir(parents=True, exist_ok=True)
        line = "\t".join([
            time.strftime("%Y-%m-%dT%H:%M:%S"), tool_id, version, code,
            str(report.returncode), f"{report.duration_seconds:.1f}s",
        ])
        with open(store.logs / f"run_{time.strftime('%Y%m%d')}.log", "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass  # 診断が残せなくても実行結果の報告は妨げない


def _build_spec(store, tool_id: str, version: str) -> ToolSpec:
    package_dir = store.package_dir(tool_id, version)
    tool_json = json.loads((package_dir / "tool.json").read_text(encoding="utf-8"))
    if tool_json.get("id") != tool_id:
        raise ValueError(f"tool.json の id が台帳と食い違います({tool_json.get('id')!r})")

    inputs_spec = tool_json.get("inputs") or {}
    accepts = tuple(inputs_spec.get("accepts") or ())
    max_files = int(inputs_spec.get("max") or 1)
    if not accepts:
        raise ValueError("tool.json の inputs.accepts がありません")

    def run(context: RunContext) -> RunResult:
        import toolpack_runner

        # 入力には固有のidを振る(順序や元ファイル名を役割の根拠にしない。台帳 D-5)。
        # role は利用者が明示したときだけ入る。初期版のハブ経路では None
        job_inputs = [
            (f"in-{index}", None, path, path.name)
            for index, path in enumerate(context.inputs, start=1)
        ]
        # ジョブ領域はハブの作業ディレクトリの中に作る。app.py が実行後に
        # まるごと消すので、成果物の取りこぼしも置き去りも起きない
        report = toolpack_runner.run_tool_package(
            package_dir, tool_json,
            inputs=job_inputs, instruction=context.instruction,
            job_root=context.work_dir / "toolpack",
        )
        if not report.ok:
            _record_failure(store, tool_id, version, report.code, report)
            raise RuntimeError(
                _ADVICE.get(report.code, "ツールの実行に失敗しました")
                + f"(コード: {report.code})"
            )
        result = report.result
        if result.status == "user_error":
            # 利用者が直せる失敗。app.py の既存経路で 422 になる(台帳 D-5)
            raise ValueError(result.message)
        return RunResult(
            files=list(result.files),
            message=result.message,
            skipped=list(result.skipped),
            notes=list(result.notes),
        )

    return ToolSpec(
        name=tool_id,
        summary=tool_json.get("summary") or tool_id,
        accepts=accepts,
        run=run,
        max_files=max_files,
    )


def load() -> tuple[dict[str, ToolSpec], list[str]]:
    """registry の有効版を ToolSpec にして返す。呼ばれるたびに読み直す。"""
    specs: dict[str, ToolSpec] = {}
    errors: list[str] = []
    try:
        store = _make_store()
        active = store.active_tools()
    except Exception as error:
        return {}, [f"追加ツールの台帳を読めません({type(error).__name__}: {error})"]

    for tool_id, info in sorted(active.items()):
        version = info["version"]
        try:
            specs[tool_id] = _build_spec(store, tool_id, version)
        except Exception as error:
            errors.append(
                f"追加ツール {tool_id} {version}: 読み込みに失敗"
                f"({type(error).__name__}: {error})"
            )
    return specs, errors
