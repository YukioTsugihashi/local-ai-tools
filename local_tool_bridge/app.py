"""
Open WebUIからKiloのローカルツールを呼ぶ受付(ハブ)。

入口は3つだけで、ツールごとに増やさない。
  GET  /health    生存確認
  GET  /v1/tools  繋がっているツールの一覧
  POST /v1/run    ツール名を指定して実行

ツールの追加は local_tool_bridge/connectors/ へファイルを1枚足すだけで、
このファイルは編集しない。処理本体は tools/ 側にあり、ここには置かない。
"""

from __future__ import annotations

import base64
import re
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from local_tool_bridge import page
from local_tool_bridge.job_lock import JobLock
from local_tool_bridge.hub import (
    ROOT,
    VENV_PYTHON,
    RunContext,
    discover,
    load_specs,
    neighbour_status,
)


WORK_ROOT = ROOT / "work" / "openwebui"
# ローカルLLMとGPUを共有するため、同時に1件しか流さない。
# プロセス間ロックなので、別プロセスの管理アプリ(追加ツールの受入・スモーク)も
# 同じ順番待ちに並べる(TOOL_PACKAGE_DECISIONS.md D-9)
_RUN_LOCK = JobLock()
MAX_CONTENT_CHARS = 150_000_000


class InputFile(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_b64: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS)


class RunRequest(BaseModel):
    tool: str = Field(min_length=1, max_length=64)
    files: list[InputFile] = Field(default_factory=list)
    instruction: str = Field(default="", max_length=10_000)
    options: dict[str, str] = Field(default_factory=dict)


class OutputFile(BaseModel):
    filename: str
    content_b64: str


class RunResponse(BaseModel):
    tool: str
    files: list[OutputFile]
    message: str
    skipped: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ToolInfo(BaseModel):
    name: str
    summary: str
    accepts: list[str]


class StatusResponse(BaseModel):
    tools: list[ToolInfo]
    errors: list[str] = Field(default_factory=list)


# 既存Pipeが呼んでいる旧形式。登録済みPipeを壊さないため残す
class ExcelCommentRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    workbook_b64: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS)
    instruction: str = Field(default="全対象シートへコメントを生成して書き込む", max_length=10_000)


class ExcelCommentResponse(BaseModel):
    filename: str
    workbook_b64: str
    report_filename: str | None = None
    report_b64: str | None = None
    message: str


def safe_filename(filename: str, accepts: tuple[str, ...]) -> str:
    """受け取ったファイル名から、作業ディレクトリへ置ける安全な名前を作る。"""
    name = Path(filename).name
    suffix = Path(name).suffix.lower()
    if accepts and suffix not in accepts:
        raise ValueError(f"対応形式は{'と'.join(accepts)}です")
    # ー(長音符 U+30FC)は ァ-ヶ(U+30A1-U+30F6)の外にあるため、範囲だけでは
    # 落ちて「データ」が「デ_タ」になる。々(踊り字)と半角カナも同様に残す
    stem = re.sub(r"[^0-9A-Za-zぁ-んァ-ヶーｦ-ﾟ一-龠々〆._-]+", "_", Path(name).stem).strip("._")
    return f"{stem or 'input'}{suffix}"


def run_tool(request: RunRequest) -> RunResponse:
    specs = load_specs()
    spec = specs.get(request.tool)
    if spec is None:
        raise ValueError(f"繋がっていないツールです: {request.tool}(利用可能: {'、'.join(sorted(specs))})")
    if not request.files:
        raise ValueError("ファイルが添付されていません")
    if len(request.files) > spec.max_files:
        raise ValueError(f"{spec.name} が受け取れるファイルは{spec.max_files}件までです")
    unknown = sorted(set(request.options) - set(spec.options))
    if unknown:
        raise ValueError(
            f"{spec.name} が受け付けないオプションです: {'、'.join(unknown)}"
            f"(利用可能: {'、'.join(spec.options) or 'なし'})"
        )

    job_dir = WORK_ROOT / uuid.uuid4().hex
    job_dir.mkdir(parents=True, exist_ok=False)
    try:
        inputs: list[Path] = []
        for item in request.files:
            try:
                content = base64.b64decode(item.content_b64, validate=True)
            except ValueError as error:
                raise ValueError(f"{item.filename} の内容を復号できません") from error
            target = job_dir / safe_filename(item.filename, spec.accepts)
            target.write_bytes(content)
            inputs.append(target)

        context = RunContext(
            root=ROOT,
            python=VENV_PYTHON,
            work_dir=job_dir,
            inputs=inputs,
            instruction=request.instruction,
            options=dict(request.options),
        )
        with _RUN_LOCK:
            result = spec.run(context)
        return RunResponse(
            tool=spec.name,
            files=[
                OutputFile(
                    filename=path.name,
                    content_b64=base64.b64encode(path.read_bytes()).decode("ascii"),
                )
                for path in result.files
                if path.is_file()
            ],
            message=result.message,
            skipped=result.skipped,
            notes=result.notes,
        )
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


app = FastAPI(title="Kilo Local Tool Hub", version="2.0.0")


@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    """繋がっているツールを目で見るための表示専用ページ。"""
    specs, errors = discover()
    return HTMLResponse(page.render(specs, errors, neighbour_status()))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/tools", response_model=list[ToolInfo])
def tools() -> list[ToolInfo]:
    return [
        ToolInfo(name=spec.name, summary=spec.summary, accepts=list(spec.accepts))
        for spec in sorted(load_specs().values(), key=lambda item: item.name)
    ]


@app.get("/v1/status", response_model=StatusResponse)
def status() -> StatusResponse:
    """一覧に加えて、読み込めなかった接続役の理由も返す。"""
    specs, errors = discover()
    return StatusResponse(
        tools=[
            ToolInfo(name=spec.name, summary=spec.summary, accepts=list(spec.accepts))
            for _, spec in sorted(specs.items())
        ],
        errors=errors,
    )


@app.post("/v1/run", response_model=RunResponse)
def run(request: RunRequest) -> RunResponse:
    try:
        return run_tool(request)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/v1/excel/comment", response_model=ExcelCommentResponse)
def excel_comment(request: ExcelCommentRequest) -> ExcelCommentResponse:
    """旧形式の入口。登録済みPipeが更新されるまでの互換用。"""
    result = run(
        RunRequest(
            tool="excel_comment",
            files=[InputFile(filename=request.filename, content_b64=request.workbook_b64)],
            instruction=request.instruction,
        )
    )
    if not result.files:
        raise HTTPException(status_code=500, detail="処理済みファイルが返りませんでした")
    workbook, *rest = result.files
    report = rest[0] if rest else None
    message = result.message
    if result.skipped:
        message = f"{message}\n\n処理できなかったもの:\n  - " + "\n  - ".join(result.skipped)
    if result.notes:
        message = f"{message}\n\n補足(処理は成功しています):\n  - " + "\n  - ".join(result.notes)
    return ExcelCommentResponse(
        filename=workbook.filename,
        workbook_b64=workbook.content_b64,
        report_filename=report.filename if report else None,
        report_b64=report.content_b64 if report else None,
        message=message,
    )
