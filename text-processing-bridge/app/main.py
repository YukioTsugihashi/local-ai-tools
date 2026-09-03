"""
FastAPIアプリとエンドポイント定義。

処理の本体は持たない。汎用処理は processing._process、
構造化議事録は structured.run_structured_minutes に委譲する。
"""

from __future__ import annotations

import base64
import binascii
import json
import time
import uuid
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from app.archive import save_import, save_output
from app.export import FORMAT_MIME, render_output
from app.kebatori import KebatoriProfiles
from app.local_workflow import LocalWorkflowClient
from app.models import (
    ExportRequest,
    ExportResponse,
    KebatoriRequest,
    MinutesDocxRequest,
    MinutesRequest,
    RefineRequest,
    ResultPageRequest,
    ResultPageResponse,
    RewriteRequest,
    StructuredMinutesRequest,
    StructuredMinutesResponse,
    SummarizeRequest,
    TextProcessingResponse,
)
from app.oplog import log_event
from app.processing import _process
from app.render_minutes import render as render_minutes_bytes
from app.result_page import build_result_page
from app.structured import run_structured_minutes


@lru_cache
def get_workflow_client() -> LocalWorkflowClient:
    return LocalWorkflowClient()


@lru_cache
def get_kebatori_profiles() -> KebatoriProfiles:
    return KebatoriProfiles.from_yaml()


app = FastAPI(
    title="Open WebUI文章処理API",
    version="1.0.0",
    description="Open WebUIからローカルLLMの文章処理を呼び出すAPIです。",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)
UI_PATH = Path(__file__).with_name("text_tools.html")


@app.middleware("http")
async def log_operations(request: Request, call_next):
    """
    PIIレスの運用ログ。/v1/ 配下だけを記録する。

    本文・名簿・LLM入出力は記録しない。応答からは件数・実行IDなど
    ホワイトリストの数値・識別子だけを抜き出す。
    """
    if not request.url.path.startswith("/v1/"):
        return await call_next(request)

    entry: dict = {
        "request_id": uuid.uuid4().hex[:12],
        "method": request.method,
        "path": request.url.path,
    }
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as error:
        entry.update(
            status=500,
            duration_ms=round((time.perf_counter() - started) * 1000),
            error=type(error).__name__,
        )
        log_event(**entry)
        raise

    entry.update(
        status=response.status_code,
        duration_ms=round((time.perf_counter() - started) * 1000),
    )

    # テキスト系APIの応答だけ、規模と実行IDをホワイトリストで抜き出す。
    # docx等を含むrender系は読まない(サイズが大きく、記録する値も無い)。
    content_type = response.headers.get("content-type", "")
    if request.url.path.startswith("/v1/text/") and "application/json" in content_type:
        body = b"".join([chunk async for chunk in response.body_iterator])
        response = Response(
            content=body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )
        if len(body) <= 2_000_000:
            try:
                payload = json.loads(body)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                for key in (
                    "operation",
                    "source_chars",
                    "result_chars",
                    "within_limit",
                    "workflow_run_id",
                    "blocks",
                    "assigned_remarks",
                    "agenda_inferred",
                    "agenda_chars",
                    "roster_chars",
                    "roster_names",
                ):
                    if key in payload:
                        entry[key] = payload[key]
                if isinstance(payload.get("warnings"), list):
                    entry["warnings"] = len(payload["warnings"])

    log_event(**entry)
    return response


@app.get("/text-tools", response_class=HTMLResponse, include_in_schema=False)
async def text_tools() -> str:
    return UI_PATH.read_text(encoding="utf-8")


@app.get("/health", operation_id="health_check", summary="稼働確認")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/version", operation_id="get_version", summary="バージョン確認")
async def version() -> dict[str, str]:
    return {"version": app.version}


@app.post(
    "/v1/text/summarize",
    response_model=TextProcessingResponse,
    operation_id="summarize_text",
    summary="文章を指定文字数以内で要約",
    description="指定文字数以内の要約が必要な場合だけ使用します。原文にない情報は追加しません。",
)
async def summarize_text(
    request: SummarizeRequest,
    client: LocalWorkflowClient = Depends(get_workflow_client),
) -> TextProcessingResponse:
    return await _process(
        operation="summarize",
        text=request.text,
        max_chars=request.max_chars,
        client=client,
        initial_retry_instruction=request.instruction,
    )


@app.post(
    "/v1/text/rewrite",
    response_model=TextProcessingResponse,
    operation_id="rewrite_text",
    summary="内容を変えずに文章を整形",
    description="内容を変えずに文章を読みやすく整える場合だけ使用します。要約や情報追加は行いません。",
)
async def rewrite_text(
    request: RewriteRequest,
    client: LocalWorkflowClient = Depends(get_workflow_client),
) -> TextProcessingResponse:
    return await _process(
        operation="rewrite",
        text=request.text,
        client=client,
        initial_retry_instruction=request.instruction,
    )


@app.post(
    "/v1/text/minutes",
    response_model=TextProcessingResponse,
    operation_id="create_minutes",
    summary="会議内容を固定形式の議事録へ整理",
    description="会議内容を「概要・決定事項・対応事項」の固定形式へ整理する場合だけ使用します。",
)
async def create_minutes(
    request: MinutesRequest,
    client: LocalWorkflowClient = Depends(get_workflow_client),
) -> TextProcessingResponse:
    return await _process(
        operation="minutes",
        text=request.text,
        max_chars=request.max_chars,
        client=client,
        initial_retry_instruction=request.instruction,
    )


@app.post(
    "/v1/text/kebatori",
    response_model=TextProcessingResponse,
    operation_id="remove_kebatori",
    summary="設定済みの毛羽を機械的に除去",
    description="設定済みのフィラーなど、定義済みの毛羽だけを機械的に除去する場合だけ使用します。",
)
async def remove_kebatori(
    request: KebatoriRequest,
    client: LocalWorkflowClient = Depends(get_workflow_client),
    profiles: KebatoriProfiles = Depends(get_kebatori_profiles),
) -> TextProcessingResponse:
    try:
        profile = profiles.get(request.profile)
    except KeyError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    return await _process(
        operation="kebatori",
        text=request.text,
        client=client,
        profile_name=profile.name,
        rules=profile.workflow_rules(),
        initial_retry_instruction=request.instruction,
    )


@app.post(
    "/v1/text/kebatori_plus",
    response_model=TextProcessingResponse,
    operation_id="remove_kebatori_plus",
    summary="ルールとLLM判定を併用して毛羽を強めに除去",
    description=(
        "定義済みルールに加えて、LLMが検出した削除候補(言い直し・つなぎ語など)を"
        "検証のうえ機械的に削除します。削除のみを行い、言い換えは行いません。"
    ),
)
async def remove_kebatori_plus(
    request: KebatoriRequest,
    client: LocalWorkflowClient = Depends(get_workflow_client),
    profiles: KebatoriProfiles = Depends(get_kebatori_profiles),
) -> TextProcessingResponse:
    try:
        profile = profiles.get(request.profile)
    except KeyError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    return await _process(
        operation="kebatori_plus",
        text=request.text,
        client=client,
        profile_name=profile.name,
        rules=profile.workflow_rules(),
        initial_retry_instruction=request.instruction,
    )


@app.post(
    "/v1/text/refine",
    response_model=TextProcessingResponse,
    operation_id="refine_text",
    summary="処理結果を指定どおり修正",
    description="文章処理後の結果へ具体的な修正指示を反映する場合だけ使用します。",
)
async def refine_text(
    request: RefineRequest,
    client: LocalWorkflowClient = Depends(get_workflow_client),
) -> TextProcessingResponse:
    return await _process(
        operation="refine",
        text=request.text,
        client=client,
        initial_retry_instruction=request.instruction,
    )


# ---------------------------------------------------------------------------
# レンダリング系(Pipe/Tool専用の内部API)。
# include_in_schema=False にして、OpenAPI経由でLLMの道具として
# 見えないようにする(実行判断をLLMに渡さないため)。
# ---------------------------------------------------------------------------


@app.post(
    "/v1/render/export",
    response_model=ExportResponse,
    include_in_schema=False,
)
async def render_export(request: ExportRequest) -> ExportResponse:
    file_bytes, mime = render_output(request.format, request.content, request.title)
    # 出来上がりを output/ にも残す。Excel経路(ハブ)と揃える
    save_output(f"{request.title or '処理結果'}.{request.format}", file_bytes)
    return ExportResponse(
        mime=mime,
        data_b64=base64.b64encode(file_bytes).decode("ascii"),
    )


@app.post(
    "/v1/render/minutes_docx",
    response_model=ExportResponse,
    include_in_schema=False,
)
async def render_minutes_docx(request: MinutesDocxRequest) -> ExportResponse:
    try:
        template_bytes = base64.b64decode(request.template_b64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise HTTPException(
            status_code=422, detail="template_b64をbase64として解釈できません"
        ) from error

    try:
        file_bytes = render_minutes_bytes(template_bytes, request.document)
    except Exception as error:
        raise HTTPException(
            status_code=422,
            detail=f"テンプレートへの流し込みに失敗しました: {error}",
        ) from error

    title = ((request.document.get("ヘッダ") or {}).get("タイトル") or "議事録")
    save_output(f"{title}.docx", file_bytes)
    return ExportResponse(
        mime=FORMAT_MIME["docx"],
        data_b64=base64.b64encode(file_bytes).decode("ascii"),
    )


@app.post(
    "/v1/render/result_page",
    response_model=ResultPageResponse,
    include_in_schema=False,
)
async def render_result_page(request: ResultPageRequest) -> ResultPageResponse:
    return ResultPageResponse(
        html=build_result_page(
            operation_label=request.operation_label,
            result=request.result,
            source_chars=request.source_chars,
            result_chars=request.result_chars,
            source_type=request.source_type,
            workflow_run_id=request.workflow_run_id,
            warnings=request.warnings,
            result_chars_crlf=request.result_chars_crlf,
            attached_filename=request.attached_filename,
        )
    )


@app.post(
    "/v1/text/minutes_structured",
    response_model=StructuredMinutesResponse,
    operation_id="create_structured_minutes",
    summary="次第の骨格に沿って議事録を構造化",
    description=(
        "会議次第から議題・項目・資料番号・担当を確定させ、文字起こしの発言を"
        "各項目へ振り分けます。長文は分割して処理し、項目ごとにコード側で統合"
        "するため、分割による形式崩れが起きません。"
    ),
)
async def create_structured_minutes(
    request: StructuredMinutesRequest,
    client: LocalWorkflowClient = Depends(get_workflow_client),
    profiles: KebatoriProfiles = Depends(get_kebatori_profiles),
) -> StructuredMinutesResponse:
    # 受け取った中身を控える。次第と名簿が本当に届いていたのかを、後から
    # 実物で確かめられるようにする(2026-08-18に「渡したのに議事録へ乗らない」の
    # 切り分けで、記録が無く推測しかできなかった)。失敗しても処理は続ける
    save_import("議事録_本文", request.text)
    save_import("議事録_次第", request.agenda_text)
    save_import("議事録_名簿", request.roster_text)
    return await run_structured_minutes(request, client, profiles)


@app.post(
    "/v1/text/custom_comment",
    operation_id="create_custom_comment",
    summary="診療科別コメントを生成(未完成・無効化中)",
    description="ブリッジ版は未実装のため、仕様確定と正式実装まで501を返す。",
)
async def create_custom_comment() -> None:
    raise HTTPException(
        status_code=501,
        detail="ブリッジ版の診療科別コメント生成は未実装です。正式実装までこのAPIは利用できません。",
    )
