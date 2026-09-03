"""
title: 追加ツールひな型
id: toolpack_template
author: local
version: 1.0.0
description: 追加ツール用の固定Pipeひな型。生成時に tool.json の宣言値へ置き換わる。
model_description: このひな型は直接登録しない。追加ツールの登録時に値が差し込まれる。
suggestion: 実行 | 追加ツールを実行する | 実行して
requirements: httpx

追加ツールの生成Pipeは**このひな型だけ**から作る(TOOL_PACKAGE_DECISIONS.md D-4)。
オンラインAIにPipe本体を書かせない。差し込めるのは検証済みの
表示名・ID・説明・入力条件・判定語・依頼例だけである。

このファイル自体が動くPipeの形をしているのは、
`tools/test_webui_shared_blocks.py` の写経ズレ検査を効かせるためである
(ファイル添付処理は各Pipeへ写経するしかなく、ズレると事故になる)。

置換される目印(生成器 toolpack_pipegen.py が差し替える):
  __TOOL_ID__ / __TOOL_TITLE__ / __TOOL_DESCRIPTION__ /
  __TOOL_PATTERN__ / __TOOL_ACCEPTS__ / __TOOL_MAX_FILES__
"""

from __future__ import annotations

import base64
import inspect
import io
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx
from pydantic import BaseModel, Field

EventEmitter = Callable[[dict[str, Any]], Awaitable[None]]

# 追加ツールの識別子。ハブ(/v1/run)へ渡すツール名と一致する
TOOL_NAME = "__TOOL_ID__"
# 受け付ける拡張子(tool.json の inputs.accepts)
ACCEPTED_SUFFIXES = ("__TOOL_ACCEPTS__",)
MAX_FILES = 1  # __TOOL_MAX_FILES__

# 1ツール1操作。静的なリテラルのままにしておくことで、
# open_webui_deploy.py のチップ照合(check_suggestion_routing)が効き続ける
OPERATION_PATTERNS = [
    ("run", re.compile(r"__TOOL_PATTERN__")),
]

HELP_MESSAGE = (
    "__TOOL_SUMMARY__\n\n"
    "対応しているファイル: __TOOL_ACCEPTS_TEXT__\n\n"
    "依頼の例:\n"
    "__TOOL_EXAMPLES__"
)

# 会話状態の保持上限(既存Pipeと同じ考え方)
MAX_TRACKED_CHATS = 64
MAX_TRACKED_FILE_IDS = 200


class Pipe:
    class Valves(BaseModel):
        LOCAL_TOOL_BRIDGE_URL: str = Field(
            default="http://host.docker.internal:8010",
            description="Windows上のローカルツール受付URL",
        )
        TIMEOUT_SECONDS: int = Field(
            default=1800, description="ツール実行のタイムアウト秒数"
        )
        MAX_FILE_MB: int = Field(default=100, description="読み込む添付の上限サイズ(MB)")

    def __init__(self) -> None:
        self.valves = self.Valves()
        self._chats: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # 会話状態
    # ------------------------------------------------------------------
    def _remember(self, chat_id: str, **fields) -> None:
        if not chat_id:
            return
        state = self._chats.setdefault(chat_id, {})
        state.update(fields)
        while len(self._chats) > MAX_TRACKED_CHATS:
            self._chats.pop(next(iter(self._chats)))

    def _recall(self, chat_id: str) -> dict:
        return self._chats.get(chat_id or "", {})

    def _pending_attachments(
        self, chat_id: str, candidates: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        """今回処理すべき添付だけを選ぶ(__files__ は会話全体を渡してくるため)。"""
        handled = set(self._recall(chat_id).get("handled") or [])
        fresh = [item for item in candidates if item[0] not in handled]
        if fresh:
            return fresh
        return list(self._recall(chat_id).get("files") or [])

    def _remember_handled(self, chat_id: str, attachments: list[tuple[str, str]]) -> None:
        handled = list(self._recall(chat_id).get("handled") or [])
        for file_id, _ in attachments:
            if file_id not in handled:
                handled.append(file_id)
        self._remember(chat_id, files=attachments, handled=handled[-MAX_TRACKED_FILE_IDS:])

    # ------------------------------------------------------------------
    # 入力の取り出し
    # ------------------------------------------------------------------
    @staticmethod
    def _content_to_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    value = item.get("text")
                    if value:
                        texts.append(str(value))
            return "\n".join(texts)
        return ""

    def _latest_user_message(self, body: dict) -> str:
        for message in reversed(body.get("messages") or []):
            if isinstance(message, dict) and message.get("role") == "user":
                text = self._content_to_text(message.get("content"))
                if text.strip():
                    return text
        return ""

    @staticmethod
    def _attachments(files: Optional[list]) -> list[tuple[str, str]]:
        results: list[tuple[str, str]] = []
        for entry in files or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") not in (None, "file"):
                continue
            file_info = entry.get("file") if isinstance(entry.get("file"), dict) else {}
            file_id = entry.get("id") or file_info.get("id")
            name = str(
                entry.get("name")
                or file_info.get("filename")
                or file_info.get("name")
                or ""
            )
            if file_id and name.lower().endswith(ACCEPTED_SUFFIXES):
                results.append((str(file_id), name))
        return results

    async def _load_bytes(self, file_id: str, name: str) -> bytes:
        from open_webui.models.files import Files

        file_model = Files.get_file_by_id(str(file_id))
        if inspect.isawaitable(file_model):
            file_model = await file_model
        if file_model is None:
            raise RuntimeError(f"{name}: ファイルを取得できませんでした。添付し直してください")
        stored_path = getattr(file_model, "path", None) or (
            (getattr(file_model, "meta", None) or {}).get("path")
        )
        if not stored_path:
            raise RuntimeError(f"{name}: ファイルの保存場所が分かりませんでした")

        from open_webui.storage.provider import Storage

        local_path = Storage.get_file(stored_path)
        if inspect.isawaitable(local_path):
            local_path = await local_path
        data = Path(str(local_path)).read_bytes()
        limit = self.valves.MAX_FILE_MB * 1024 * 1024
        if len(data) > limit:
            raise RuntimeError(
                f"{name}: ファイルが大きすぎます"
                f"({len(data) / (1024 * 1024):.1f}MB > 上限{self.valves.MAX_FILE_MB}MB)"
            )
        return data

    # ------------------------------------------------------------------
    # 結果ファイルの保存と添付(写経元: open_webui_text_processing_pipe.py)
    # ------------------------------------------------------------------
    @staticmethod
    async def _store_file(
        request: Any,
        user_info: Optional[dict],
        filename: str,
        file_bytes: bytes,
        mime_type: str,
    ) -> dict[str, Any]:
        if request is None:
            raise RuntimeError("__request__を取得できませんでした")
        if not user_info or not user_info.get("id"):
            raise RuntimeError("__user__からユーザーIDを取得できませんでした")

        from open_webui.models.users import Users
        from open_webui.routers.files import upload_file_handler
        from starlette.datastructures import Headers, UploadFile

        user_model = Users.get_user_by_id(str(user_info["id"]))
        if inspect.isawaitable(user_model):
            user_model = await user_model
        if user_model is None:
            raise RuntimeError("Open WebUIのユーザー情報を読み込めませんでした")

        upload_kwargs: dict[str, Any] = {
            "file": io.BytesIO(file_bytes),
            "filename": filename,
            "headers": Headers({"content-type": mime_type}),
        }
        if "size" in inspect.signature(UploadFile).parameters:
            upload_kwargs["size"] = len(file_bytes)
        upload_file = UploadFile(**upload_kwargs)

        handler_values: dict[str, Any] = {
            "file": upload_file,
            "metadata": {"source": "toolpack_template", "generated": True},
            "process": False,
            "process_in_background": False,
            "user": user_model,
            "background_tasks": None,
            "db": None,
        }
        supported = inspect.signature(upload_file_handler).parameters
        handler_kwargs = {k: v for k, v in handler_values.items() if k in supported}

        try:
            result = upload_file_handler(request, **handler_kwargs)
            if inspect.isawaitable(result):
                result = await result
        finally:
            close_result = upload_file.close()
            if inspect.isawaitable(close_result):
                await close_result

        if isinstance(result, dict):
            data = result
        elif hasattr(result, "model_dump"):
            data = result.model_dump()
        elif hasattr(result, "dict"):
            data = result.dict()
        else:
            raise RuntimeError("ファイル登録結果を解釈できませんでした")

        if not data.get("id"):
            raise RuntimeError("ファイル登録結果にIDがありません")
        return data

    @staticmethod
    async def _attach_file(
        event_emitter: Optional[EventEmitter],
        file_id: str,
        filename: str,
        registry: Optional[list] = None,
    ) -> None:
        """
        結果ファイルをメッセージに添付する。

        Open WebUIはfilesイベントをDB側では「追記」、画面側では「置き換え」で
        処理する。1件だけ送ると画面のチップが毎回上書きされ、複数添付しても
        最後の1件しかダウンロードできなくなる。そこで、
        - files: 新規1件のみ(DB保存用。サーバーが既存分へ追記する)
        - chat:message:files: 累積全件(画面表示用。DBには保存されない)
        の2つを送り、画面とDBの両方に全ファイルを残す。
        """
        if event_emitter is None:
            return
        entry = {
            "type": "file",
            "id": file_id,
            "name": filename,
            "url": f"/api/v1/files/{file_id}/content?attachment=true",
        }
        await event_emitter({"type": "files", "data": {"files": [entry]}})
        if registry is not None:
            registry.append(entry)
            await event_emitter(
                {"type": "chat:message:files", "data": {"files": list(registry)}}
            )

    @staticmethod
    def _mime_for(filename: str) -> str:
        lowered = filename.lower()
        if lowered.endswith(".xlsm"):
            return "application/vnd.ms-excel.sheet.macroEnabled.12"
        if lowered.endswith(".xlsx"):
            return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if lowered.endswith(".docx"):
            return (
                "application/vnd.openxmlformats-officedocument"
                ".wordprocessingml.document"
            )
        if lowered.endswith(".csv"):
            return "text/csv; charset=utf-8"
        if lowered.endswith(".png"):
            return "image/png"
        if lowered.endswith(".txt") or lowered.endswith(".md"):
            return "text/plain; charset=utf-8"
        return "application/octet-stream"

    # ------------------------------------------------------------------
    # ハブへの委譲(処理本体は追加ツール側。ここは受け渡しだけ)
    # ------------------------------------------------------------------
    async def _run_hub(
        self,
        name: str,
        data: bytes,
        instruction: str,
        user_info: Optional[dict],
        request: Any,
        event_emitter: Optional[EventEmitter],
        registry: Optional[list] = None,
    ) -> str:
        payload = {
            "tool": TOOL_NAME,
            "files": [
                {"filename": name, "content_b64": base64.b64encode(data).decode("ascii")}
            ],
            "instruction": instruction,
        }
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{self.valves.LOCAL_TOOL_BRIDGE_URL.rstrip('/')}/v1/run", json=payload
            )
        if response.status_code >= 400:
            # 利用者へは中身の文章だけを見せる。HTTPの符号やJSONの殻を出すと、
            # せっかくの案内が読めなくなる(2026-08-31に実際そうなった)
            detail = ""
            try:
                body = response.json()
                detail = str(body.get("detail") or "") if isinstance(body, dict) else ""
            except Exception:
                detail = ""
            raise RuntimeError(detail or f"処理できませんでした(HTTP {response.status_code})")
        result = response.json()

        for item in result.get("files", []):
            file_bytes = base64.b64decode(item["content_b64"])
            filename = str(item["filename"])
            stored = await self._store_file(
                request=request, user_info=user_info, filename=filename,
                file_bytes=file_bytes, mime_type=self._mime_for(filename),
            )
            await self._attach_file(event_emitter, str(stored["id"]), filename, registry)

        message = str(result.get("message") or "")
        skipped = result.get("skipped") or []
        if skipped:
            message += "\n\n処理できなかったもの:\n  - " + "\n  - ".join(skipped)
        # 補足は対処が要らない情報。処理できなかったものと同じ見出しに並べない
        notes = result.get("notes") or []
        if notes:
            message += "\n\n補足(処理は成功しています):\n  - " + "\n  - ".join(notes)
        return message

    # ------------------------------------------------------------------
    # 本体
    # ------------------------------------------------------------------
    async def pipe(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __request__: Any = None,
        __files__: Optional[list] = None,
        __event_emitter__: Optional[EventEmitter] = None,
        __task__: Optional[str] = None,
        __chat_id__: Optional[str] = None,
    ) -> str:
        if __task__:
            return "__TOOL_TITLE__"

        message_text = self._latest_user_message(body)
        chat_id = __chat_id__ or ""
        attachments = self._pending_attachments(chat_id, self._attachments(__files__))
        if not attachments:
            if __files__:
                return (
                    f"対応しているのは {'・'.join(ACCEPTED_SUFFIXES)} だけです。\n\n"
                    + HELP_MESSAGE
                )
            return HELP_MESSAGE
        if len(attachments) > MAX_FILES:
            return f"このツールが受け取れるファイルは{MAX_FILES}件までです。"
        self._remember_handled(chat_id, attachments)

        results: list[str] = []
        attached: list = []
        for file_id, name in attachments:
            try:
                data = await self._load_bytes(file_id, name)
                results.append(
                    await self._run_hub(
                        name, data, message_text, __user__, __request__,
                        __event_emitter__, attached,
                    )
                )
            except RuntimeError as error:
                results.append(f"エラー: {error}")
            except Exception as error:
                results.append(
                    f"エラー: {name} の処理に失敗しました({type(error).__name__}: {error})"
                )
        return "\n\n".join(results)
