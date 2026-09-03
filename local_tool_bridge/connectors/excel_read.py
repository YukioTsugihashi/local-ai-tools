"""Excelの読み取り(シート一覧・値検索・範囲表示・CSV出力)。"""

from __future__ import annotations

from local_tool_bridge.hub import (
    RunContext,
    RunResult,
    ToolSpec,
    run_repo_script,
    saved_paths,
)

# 操作ごとの引数。判定はPipe側の正規表現で済んでおり、ここでは組み立てるだけ
OPERATIONS = ("index", "find", "range", "table")


def _script_args(context: RunContext) -> list[str]:
    operation = context.options.get("operation", "index")
    source = str(context.inputs[0])
    if operation == "index":
        return [source, "--index-only", "--no-csv"]
    if operation == "find":
        query = context.options.get("query", "").strip()
        if not query:
            raise ValueError("検索語が指定されていません")
        return [source, "--find", query, "--context-rows", "2", "--no-csv"]
    if operation == "range":
        sheet = context.options.get("sheet", "").strip()
        cell_range = context.options.get("range", "").strip()
        if not sheet or not cell_range:
            raise ValueError("範囲表示にはシート名と範囲の両方が必要です")
        return [source, "--sheet", sheet, "--range", cell_range, "--no-csv"]
    if operation == "table":
        # CSVはファイルとして返すため --no-csv を付けない
        sheet = context.options.get("sheet", "").strip()
        return [source, "--rows", "0"] + (["--csv-sheet", sheet] if sheet else [])
    raise ValueError(f"未対応の操作です: {operation}(利用可能: {'、'.join(OPERATIONS)})")


def run(context: RunContext) -> RunResult:
    operation = context.options.get("operation", "index")
    output = run_repo_script(context, "tools/excel_reader.py", _script_args(context))
    files = saved_paths(output, context.root) if operation == "table" else []
    return RunResult(files=files, message=output)


SPEC = ToolSpec(
    name="excel_read",
    summary="Excelのシート一覧・値検索・範囲表示・CSV出力(読み取り専用)",
    accepts=(".xlsx", ".xlsm"),
    run=run,
    options=("operation", "query", "sheet", "range"),
)
