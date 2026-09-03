"""Excelの全対象シートへコメントを生成して書き込む接続。"""

from __future__ import annotations

import json

from local_tool_bridge.hub import RunContext, RunResult, ToolSpec, resolved_path, run_repo_script


def run(context: RunContext) -> RunResult:
    output = run_repo_script(
        context,
        "tools/excel_comment_build.py",
        [str(context.inputs[0]), "--instruction", context.instruction],
    )
    result = json.loads(output.splitlines()[-1])
    files = [resolved_path(context.root, result["workbook"])]
    if result.get("report"):
        files.append(resolved_path(context.root, result["report"]))
    skipped = list(result.get("skipped") or [])
    # 文体整形を省いた理由・適用した書式は「補足」。書込みは成功しているので
    # skipped へ混ぜない(混ぜると成功が「処理できなかったもの」として出る)
    notes = list(result.get("notes") or [])
    return RunResult(
        files=files,
        message=(
            f"処理できなかったシートが{len(skipped)}件あります"
            if skipped
            else "全対象シートへコメントを生成・挿入しました"
        ),
        skipped=skipped,
        notes=notes,
    )


SPEC = ToolSpec(
    name="excel_comment",
    summary="Excelの表を読み、全対象シートへコメントを生成して書き込む",
    accepts=(".xlsx", ".xlsm"),
    run=run,
)
