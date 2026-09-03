"""
title: 文章処理(ツール版)
id: dify_bridge
description: 文章処理ブリッジ(port 8008)経由でローカルLLMの文章処理を実行する。長文は自動分割され、結果は埋め込み表示でモデルに再要約させない。ケバ取りは既定でLLM判定併用の強版。結果はテキストファイルとしても自動添付する。埋め込みページのHTMLはブリッジ側(/v1/render/result_page)が組み立てる
author: local
version: 0.9.0
requirements: httpx
"""

import inspect
import io
import json
import re
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

import httpx
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

# ケバ取り依頼の言い回し。小型ローカルLLMが要約系関数へ誤ルーティングしても、
# 依頼文がこれに一致すればケバ取りへ引き戻すための保険として使う。
KEBATORI_REQUEST_PATTERN = re.compile(
    r"ケバ[取と]り|けば[取と]り|毛羽[取とど]り|フィラー(?:の|を)?除去"
    r"|言いよどみ(?:の|を)?(?:除去|削除|カット)"
)
OTHER_REQUEST_PATTERN = re.compile(r"要約|要点|サマリ|まとめ|議事録")
# ルールのみの高速版を明示的に指す語。これが依頼文にあるときだけ強版を外す。
FAST_KEBATORI_PATTERN = re.compile(
    r"高速|簡易|軽く|軽めに|さっと|サッと|クイック|急ぎ|ルールのみ"
)

OPERATION_LABELS = {
    "summarize": "要約",
    "rewrite": "文章整形",
    "minutes": "議事録",
    "kebatori": "ケバ取り",
    "kebatori_plus": "ケバ取り強",
    "refine": "指示修正",
}


class Tools:
    class Valves(BaseModel):
        BRIDGE_BASE_URL: str = Field(
            default="http://host.docker.internal:8008",
            description="文章処理ブリッジのベースURL",
        )

        TIMEOUT_SECONDS: int = Field(
            default=1800,
            description="ブリッジ呼び出しのタイムアウト秒数(長文分割処理を含む)",
        )

        KEBATORI_PROFILE: str = Field(
            default="default",
            description="ケバ取りで使用するプロファイル名(ブリッジのkebatori.yamlで定義)",
        )

        KEBATORI_DEFAULT_STRONG: bool = Field(
            default=True,
            description=(
                "ケバ取り依頼を既定で「ケバ取り強」として実行する。"
                "LLMが削除候補のつなぎ語を引用列挙し、コード側が検証(12字以内・"
                "句点/漢字/数字を含む候補は拒否・前後境界チェック・総削除率30%上限)"
                "したうえで機械的に削除する。言い換え・要約は構造的に発生しない。"
                "無効にするとルールのみの高速版が既定になる"
            ),
        )

        RETURN_AS_EMBED: bool = Field(
            default=True,
            description=(
                "処理結果を埋め込み欄へ直接表示する。"
                "長文をローカルLLMが再要約するのを防ぐため、通常は有効のまま使用する"
            ),
        )

        PREFER_ATTACHED_FILES: bool = Field(
            default=True,
            description="添付ファイルの抽出本文がある場合、最新メッセージ本文より優先する",
        )

        ATTACH_RESULT_FILE: bool = Field(
            default=True,
            description=(
                "処理結果をテキストファイルとしてチャットへ自動添付する。"
                "埋め込み表示ではLLMに本文を渡さないため、LLM経由のファイル出力ツールでは"
                "全文を書き出せない。確実に全文を残すには有効のまま使用する"
            ),
        )

    def __init__(self):
        self.valves = self.Valves()

    @staticmethod
    def _content_to_text(content: Any) -> str:
        """Open WebUIのmessage.contentからテキスト部分だけを取得する。"""
        if isinstance(content, str):
            return content

        if isinstance(content, list):
            texts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue

                if item.get("type") == "text":
                    value = item.get("text", "")
                    if value is not None:
                        texts.append(str(value))

            return "\n".join(texts)

        if isinstance(content, dict):
            value = content.get("text", "")
            return "" if value is None else str(value)

        return ""

    def _get_latest_message_by_role(
        self,
        messages: Optional[list],
        role: str,
    ) -> str:
        if not messages:
            return ""

        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            if message.get("role") != role:
                continue

            text = self._content_to_text(message.get("content", ""))
            if text.strip():
                return text

        return ""

    @staticmethod
    def _extract_file_contents(files: Optional[list]) -> list[tuple[str, str]]:
        """
        Open WebUIの__files__から文書パーサーによる抽出本文を取得する。

        返り値:
            [(ファイル名, 抽出本文), ...]
        """
        extracted: list[tuple[str, str]] = []

        if not files:
            return extracted

        for entry in files:
            if not isinstance(entry, dict):
                continue

            file_info = entry.get("file")
            if not isinstance(file_info, dict):
                file_info = entry.get("files")
            if not isinstance(file_info, dict):
                file_info = entry

            data = file_info.get("data")
            if not isinstance(data, dict):
                continue

            content = data.get("content", "")
            if not isinstance(content, str) or not content.strip():
                continue

            filename = (
                file_info.get("filename")
                or file_info.get("name")
                or entry.get("name")
                or "添付ファイル"
            )
            extracted.append((str(filename), content))

        return extracted

    @staticmethod
    def _resolve_inputs(
        messages: Optional[list],
        files: Optional[list],
        metadata: Optional[dict],
    ) -> tuple[list, list]:
        """
        Open WebUI 0.9系はツールへ__messages__/__files__を渡さない。
        __metadata__のuser_message/filesから同等の情報を復元する。

        ファイルは**最新メッセージの添付を最優先**にする。__files__ と
        __metadata__["files"] は会話全体のファイルを持つため、そちらを先に
        見ると、2回目以降の依頼で前のメッセージの添付まで巻き込む。
        """
        meta = metadata or {}
        user_message = meta.get("user_message")

        resolved_messages = list(messages) if messages else []
        if not resolved_messages:
            if isinstance(user_message, dict) and user_message:
                restored = dict(user_message)
                restored.setdefault("role", "user")
                resolved_messages = [restored]

        # 今回のメッセージに添付があれば、それだけが処理対象
        resolved_files: list = []
        if isinstance(user_message, dict):
            resolved_files = list(user_message.get("files") or [])
        # 添付が今回のメッセージに無いときだけ、会話全体から拾う
        if not resolved_files:
            resolved_files = list(files) if files else []
        if not resolved_files:
            resolved_files = list(meta.get("files") or [])

        return resolved_messages, resolved_files

    @staticmethod
    async def _load_full_file_contents(files: Optional[list]) -> list[tuple[str, str]]:
        """
        添付ファイルの全文をOpen WebUIのファイルストアからIDで直接取得する。

        Open WebUIは添付文書をRAGで断片化してプロンプトへ入れるため、
        プロンプト経由では全文が揃わない。IDから抽出済み本文を直接読むことで
        長文の途中欠落を防ぐ。
        """
        results: list[tuple[str, str]] = []
        if not files:
            return results

        try:
            from open_webui.models.files import Files
        except Exception:
            return results

        for entry in files:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") not in (None, "file"):
                continue

            file_info = entry.get("file") if isinstance(entry.get("file"), dict) else {}
            file_id = entry.get("id") or file_info.get("id")
            name = (
                entry.get("name")
                or file_info.get("filename")
                or file_info.get("name")
                or "添付ファイル"
            )

            content = ""
            if file_id:
                try:
                    file_model = Files.get_file_by_id(str(file_id))
                    if inspect.isawaitable(file_model):
                        file_model = await file_model
                    if file_model is not None:
                        content = ((file_model.data or {}).get("content") or "").strip()
                except Exception:
                    content = ""

            if content:
                results.append((str(name), content))
        return results

    async def _get_source_text(
        self,
        messages: Optional[list],
        files: Optional[list],
        metadata: Optional[dict] = None,
    ) -> tuple[str, str]:
        """
        処理対象本文を取得する。

        RETURN:
            (本文, 取得元)
        """
        messages, files = self._resolve_inputs(messages, files, metadata)

        # まずIDから全文を取る。取れないときだけプロンプト同梱の抽出本文へ落とす。
        file_contents = await self._load_full_file_contents(files)
        if not file_contents:
            file_contents = self._extract_file_contents(files)
        message_text = self._get_latest_message_by_role(messages, "user")

        if self.valves.PREFER_ATTACHED_FILES and file_contents:
            if len(file_contents) == 1:
                filename, content = file_contents[0]
                return content, f"file:{filename}"

            combined = "\n\n".join(
                f"===== {filename} =====\n{content}"
                for filename, content in file_contents
            )
            return combined, f"files:{len(file_contents)}"

        if message_text.strip():
            return message_text, "latest_user_message"

        if file_contents:
            if len(file_contents) == 1:
                filename, content = file_contents[0]
                return content, f"file:{filename}"

            combined = "\n\n".join(
                f"===== {filename} =====\n{content}"
                for filename, content in file_contents
            )
            return combined, f"files:{len(file_contents)}"

        return "", "not_found"

    def _is_kebatori_request(
        self,
        messages: Optional[list],
        metadata: Optional[dict] = None,
    ) -> bool:
        """
        依頼文がケバ取り指定かを判定する。

        依頼の指示は本文の先頭か末尾に書かれることが多いため、
        文字起こし本文中の語句に反応しないよう先頭・末尾だけを見る。
        """
        messages, _ = self._resolve_inputs(messages, None, metadata)
        request_text = self._get_latest_message_by_role(messages, "user")
        if not request_text:
            return False

        zone = request_text[:200] + "\n" + request_text[-200:]
        if not KEBATORI_REQUEST_PATTERN.search(zone):
            return False
        return OTHER_REQUEST_PATTERN.search(zone) is None

    async def _redirect_to_kebatori(
        self,
        called_operation: str,
        __messages__: Optional[list],
        __files__: Optional[list],
        __user__: Optional[dict],
        __request__: Any,
        __metadata__: Optional[dict],
        __event_emitter__: Optional[Callable],
    ) -> Any:
        await self._emit_status(
            __event_emitter__,
            f"ケバ取りの依頼と判断したため、{called_operation}ではなくケバ取りを実行します",
            False,
        )
        return await self.remove_kebatori(
            __messages__=__messages__,
            __files__=__files__,
            __user__=__user__,
            __request__=__request__,
            __metadata__=__metadata__,
            __event_emitter__=__event_emitter__,
        )

    @staticmethod
    async def _emit_status(
        emitter: Optional[Callable],
        description: str,
        done: bool,
    ) -> None:
        if emitter is None:
            return

        try:
            await emitter(
                {
                    "type": "status",
                    "data": {
                        "description": description,
                        "done": done,
                        "hidden": False,
                    },
                }
            )
        except Exception:
            # ステータス表示の失敗で本文処理を止めない
            pass

    async def _render_result(
        self,
        *,
        operation: str,
        result: str,
        source_chars: int,
        result_chars: int,
        source_type: str,
        workflow_run_id: str,
        warnings: list[str],
        result_chars_crlf: Optional[int] = None,
        attached_filename: str = "",
    ) -> Any:
        """
        長文結果をHTMLResponseとして直接表示する。

        Open WebUIの通常Tool返り値はローカルLLMへ戻され、最終回答生成時に
        短縮・要約される可能性がある。HTMLResponseにすると全文は埋め込み欄へ
        直接表示され、LLMには結果本文を渡さずメタデータだけを返せる。
        ページのHTML本体はブリッジ /v1/render/result_page が組み立てる
        (見た目の変更はブリッジ側 app/result_page.py だけで完結する)。
        """
        operation_label = OPERATION_LABELS.get(operation, operation)

        print(
            "[DIFY_TEXT_PROCESSING] "
            f"operation={operation} "
            f"source={source_type} "
            f"input_chars={source_chars} "
            f"output_chars={result_chars} "
            f"workflow_run_id={workflow_run_id or '-'}"
        )

        if not self.valves.RETURN_AS_EMBED:
            notice = f" / {' '.join(warnings)}" if warnings else ""
            return (
                f"【{operation_label}: 入力{source_chars}字 → 出力{result_chars}字{notice}】\n"
                f"{result}"
            )

        try:
            url = (
                self.valves.BRIDGE_BASE_URL.rstrip("/") + "/v1/render/result_page"
            )
            timeout = httpx.Timeout(
                float(self.valves.TIMEOUT_SECONDS), connect=30.0
            )
            async with httpx.AsyncClient(timeout=timeout) as client:
                page = await client.post(
                    url,
                    json={
                        "operation_label": operation_label,
                        "result": result,
                        "source_chars": source_chars,
                        "result_chars": result_chars,
                        "result_chars_crlf": result_chars_crlf,
                        "source_type": source_type,
                        "workflow_run_id": workflow_run_id,
                        "warnings": warnings,
                        "attached_filename": attached_filename,
                    },
                )
            page.raise_for_status()
            html_content = str(page.json()["html"])
        except Exception:
            # ページ生成に失敗しても結果は失わない。平文で全文を返す。
            notice = f" / {' '.join(warnings)}" if warnings else ""
            return (
                f"【{operation_label}: 入力{source_chars}字 → "
                f"出力{result_chars}字{notice}】\n{result}"
            )

        response = HTMLResponse(
            content=html_content,
            headers={"Content-Disposition": "inline"},
        )

        # LLMには全文を返さず、埋め込み表示済みという事実だけを渡す。
        # これにより最終回答生成時の再要約・途中切れを避ける。
        message = (
            "全文は埋め込み欄に表示済みです。本文を再掲・要約しないでください。"
            "他の処理を追加で実行しないでください。"
        )
        if attached_filename:
            message += (
                f"結果は{attached_filename}として添付済みです。"
                "ファイル出力ツールを呼ばないでください。"
            )
        context = {
            "status": "success",
            "message": message,
            "operation": operation,
            "source_chars": source_chars,
            "result_chars": result_chars,
            "result_chars_crlf": result_chars_crlf,
            "attached_filename": attached_filename or None,
            "source_type": source_type,
            "workflow_run_id": workflow_run_id or None,
            "warnings": warnings,
        }

        return response, context

    # ------------------------------------------------------------------
    # 文字数カウント
    # ------------------------------------------------------------------
    @staticmethod
    def _count_chars(text: str) -> tuple[int, int]:
        """
        文字数を2通りで数える。

        エディタや文書ソフトは改行をCRLF(2文字)として数えることがあり、
        Python側のlen()(改行=1文字)とはずれる。上限判定は必ず多い側で行い、
        どちらで開いても指定文字数を超えないようにする。

        RETURN:
            (lf_count, crlf_count)
        """
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        lf_count = len(normalized)
        return lf_count, lf_count + normalized.count("\n")

    async def count_lines_and_chars(
        self,
        __messages__: Optional[list] = None,
        __files__: Optional[list] = None,
        __user__: Optional[dict] = None,
        __request__: Any = None,
        __metadata__: Optional[dict] = None,
        __event_emitter__: Optional[Callable] = None,
    ) -> str:
        """
        貼り付けた文章の文字数と行数を数えて返す。

        :return: 文字数と行数の統計情報
        """
        text, source_type = await self._get_source_text(
            __messages__, __files__, __metadata__
        )
        if not text.strip():
            return "エラー: 処理対象の文章を取得できませんでした。"

        lines = text.splitlines()
        line_count = len(lines)
        char_count, char_count_crlf = self._count_chars(text)

        result = (
            f"文字数: {char_count}文字\n"
            f"改行CRLF換算: {char_count_crlf}文字\n"
            f"行数: {line_count}行\n"
            f"取得元: {source_type}"
        )

        return result

    # ------------------------------------------------------------------
    # 結果ファイルの保存と添付
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
            "metadata": {"source": "dify_text_processing", "generated": True},
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
        event_emitter: Optional[Callable], file_id: str, filename: str
    ) -> None:
        if event_emitter is None:
            return
        await event_emitter(
            {
                "type": "files",
                "data": {
                    "files": [
                        {
                            "type": "file",
                            "id": file_id,
                            "name": filename,
                            "url": f"/api/v1/files/{file_id}/content?attachment=true",
                        }
                    ]
                },
            }
        )

    async def _emit_result_file(
        self,
        operation: str,
        result: str,
        user_info: Optional[dict],
        request: Any,
        event_emitter: Optional[Callable],
    ) -> str:
        """結果を全文テキストとして保存し、チャットへ添付する。"""
        if not self.valves.ATTACH_RESULT_FILE:
            return ""

        label = OPERATION_LABELS.get(operation, operation)
        filename = f"{label}_{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
        try:
            stored = await self._store_file(
                request=request,
                user_info=user_info,
                filename=filename,
                file_bytes=result.encode("utf-8-sig"),
                mime_type="text/plain; charset=utf-8",
            )
            await self._attach_file(
                event_emitter,
                str(stored["id"]),
                str(stored.get("filename") or filename),
            )
            return str(stored.get("filename") or filename)
        except Exception as exc:
            await self._emit_status(
                event_emitter, f"結果ファイルの添付に失敗しました: {exc}", False
            )
            return ""

    async def _run_bridge(
        self,
        operation: str,
        payload: dict,
        source_type: str,
        __event_emitter__: Optional[Callable] = None,
        __user__: Optional[dict] = None,
        __request__: Any = None,
    ) -> Any:
        """文章処理ブリッジのエンドポイントを呼び出し、結果を表示用に整える。"""
        operation_label = OPERATION_LABELS.get(operation, operation)
        source_chars = len(str(payload.get("text", "")))

        await self._emit_status(
            __event_emitter__,
            f"{operation_label}を実行しています(入力{source_chars}文字)",
            False,
        )

        url = (
            self.valves.BRIDGE_BASE_URL.rstrip("/")
            + f"/v1/text/{operation}"
        )
        timeout = httpx.Timeout(
            timeout=float(self.valves.TIMEOUT_SECONDS),
            connect=30.0,
        )

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=payload)
        except httpx.TimeoutException:
            await self._emit_status(
                __event_emitter__,
                "文章処理がタイムアウトしました",
                True,
            )
            return (
                "エラー: 文章処理がタイムアウトしました。"
                f"待機時間は{self.valves.TIMEOUT_SECONDS}秒です。"
            )
        except httpx.RequestError as exc:
            await self._emit_status(
                __event_emitter__,
                "文章処理ブリッジへの接続に失敗しました",
                True,
            )
            return f"エラー: 文章処理ブリッジへ接続できませんでした: {exc}"

        if response.status_code >= 400:
            detail = ""
            try:
                detail = str(response.json().get("detail", ""))
            except ValueError:
                detail = response.text[:500]
            await self._emit_status(
                __event_emitter__,
                f"文章処理ブリッジがHTTP {response.status_code}を返しました",
                True,
            )
            return (
                f"エラー: 文章処理ブリッジがHTTP {response.status_code}を返しました。\n"
                f"{detail}"
            )

        try:
            body = response.json()
        except ValueError:
            return (
                "エラー: 文章処理ブリッジからJSON以外の応答が返されました。\n"
                f"{response.text[:500]}"
            )

        result = body.get("text")
        if not isinstance(result, str):
            return (
                "エラー: 文章処理ブリッジの応答に本文がありません。\n"
                f"{json.dumps(body, ensure_ascii=False)[:500]}"
            )

        warnings = [
            str(warning)
            for warning in (body.get("warnings") or [])
            if str(warning).strip()
        ]
        result_chars, result_chars_crlf = self._count_chars(result)

        attached = await self._emit_result_file(
            operation, result, __user__, __request__, __event_emitter__
        )

        await self._emit_status(
            __event_emitter__,
            f"完了しました(入力{source_chars}文字 → 出力{result_chars}文字)",
            True,
        )

        return await self._render_result(
            operation=operation,
            result=result,
            source_chars=int(body.get("source_chars") or source_chars),
            result_chars=result_chars,
            result_chars_crlf=result_chars_crlf,
            source_type=source_type,
            workflow_run_id=str(body.get("workflow_run_id") or ""),
            warnings=warnings,
            attached_filename=attached,
        )

    async def summarize_text(
        self,
        max_chars: int = 0,
        __messages__: Optional[list] = None,
        __files__: Optional[list] = None,
        __user__: Optional[dict] = None,
        __request__: Any = None,
        __metadata__: Optional[dict] = None,
        __event_emitter__: Optional[Callable] = None,
    ) -> Any:
        """
        「要約して」「まとめて」など要約を明示的に依頼された場合だけ、
        最新のユーザーメッセージまたは添付文書を要約する。
        「ケバ取り」「けばとり」「毛羽取り」「フィラー除去」の依頼には
        この関数ではなくremove_kebatoriを使う。
        依頼された処理だけを実行し、他の関数を続けて呼ばない。

        :param max_chars: 要約結果の文字数上限。既定は0で文字数無制限。
            ユーザーが「800字以内」のように文字数を明示したときだけ、その数値を渡す。
        :return: 要約結果
        """
        if self._is_kebatori_request(__messages__, __metadata__):
            return await self._redirect_to_kebatori(
                "要約",
                __messages__,
                __files__,
                __user__,
                __request__,
                __metadata__,
                __event_emitter__,
            )

        text, source_type = await self._get_source_text(
            __messages__, __files__, __metadata__
        )
        if not text.strip():
            return "エラー: 処理対象の文章を取得できませんでした。"

        payload: dict = {"text": text}
        if isinstance(max_chars, int) and max_chars > 0:
            payload["max_chars"] = max_chars

        return await self._run_bridge(
            "summarize",
            payload,
            source_type,
            __event_emitter__,
            __user__,
            __request__,
        )

    async def rewrite_text(
        self,
        __messages__: Optional[list] = None,
        __files__: Optional[list] = None,
        __user__: Optional[dict] = None,
        __request__: Any = None,
        __metadata__: Optional[dict] = None,
        __event_emitter__: Optional[Callable] = None,
    ) -> Any:
        """
        最新のユーザーメッセージまたは添付文書を、情報を削らず読みやすく整える。
        要約には使用しない。「ケバ取り」「フィラー除去」の依頼には
        この関数ではなくremove_kebatoriを使う。

        :return: 文章整形結果
        """
        if self._is_kebatori_request(__messages__, __metadata__):
            return await self._redirect_to_kebatori(
                "文章整形",
                __messages__,
                __files__,
                __user__,
                __request__,
                __metadata__,
                __event_emitter__,
            )

        text, source_type = await self._get_source_text(
            __messages__, __files__, __metadata__
        )
        if not text.strip():
            return "エラー: 処理対象の文章を取得できませんでした。"

        return await self._run_bridge(
            "rewrite",
            {"text": text},
            source_type,
            __event_emitter__,
            __user__,
            __request__,
        )

    async def create_minutes(
        self,
        max_chars: int = 0,
        __messages__: Optional[list] = None,
        __files__: Optional[list] = None,
        __user__: Optional[dict] = None,
        __request__: Any = None,
        __metadata__: Optional[dict] = None,
        __event_emitter__: Optional[Callable] = None,
    ) -> Any:
        """
        最新のユーザーメッセージまたは添付文書を、詳細な議事録形式に整理する。
        「ケバ取り」「フィラー除去」の依頼にはremove_kebatoriを使う。

        :param max_chars: 議事録の文字数上限。0なら制限なし
        :return: 議事録
        """
        if self._is_kebatori_request(__messages__, __metadata__):
            return await self._redirect_to_kebatori(
                "議事録",
                __messages__,
                __files__,
                __user__,
                __request__,
                __metadata__,
                __event_emitter__,
            )

        text, source_type = await self._get_source_text(
            __messages__, __files__, __metadata__
        )
        if not text.strip():
            return "エラー: 処理対象の文章を取得できませんでした。"

        payload: dict = {"text": text}
        if isinstance(max_chars, int) and max_chars > 0:
            payload["max_chars"] = max_chars

        return await self._run_bridge(
            "minutes",
            payload,
            source_type,
            __event_emitter__,
            __user__,
            __request__,
        )

    async def remove_kebatori(
        self,
        __messages__: Optional[list] = None,
        __files__: Optional[list] = None,
        __user__: Optional[dict] = None,
        __request__: Any = None,
        __metadata__: Optional[dict] = None,
        __event_emitter__: Optional[Callable] = None,
    ) -> Any:
        """
        「ケバ取り」「けばとり」「毛羽取り」「フィラー除去」「言いよどみの削除」を
        依頼されたときは、必ずこの関数を使う。
        最新のユーザーメッセージまたは添付文書から、言いよどみ・口癖・つなぎ語だけを
        機械的に除去する。既定では「ケバ取り強」として実行し、定義済みルールに加えて
        LLMが検出した削除候補をコード側で検証してから削除する。削除のみを行うため、
        言い換え・要約・並べ替えは発生しない。文章量はほとんど減らない。
        「高速」「簡易」「ルールのみ」と指定された場合はルールのみの高速版になる。
        要約、言い換え、並べ替えには使用しない。

        :return: 毛羽取り結果
        """
        text, source_type = await self._get_source_text(
            __messages__, __files__, __metadata__
        )
        if not text.strip():
            return "エラー: 処理対象の文章を取得できませんでした。"

        # 指示は本文の先頭か末尾に書かれることが多いため、その範囲だけで判定する。
        resolved_messages, _ = self._resolve_inputs(
            __messages__, __files__, __metadata__
        )
        request_text = self._get_latest_message_by_role(
            resolved_messages, "user"
        )
        zone = request_text[:200] + "\n" + request_text[-200:]
        use_strong = self.valves.KEBATORI_DEFAULT_STRONG and not (
            FAST_KEBATORI_PATTERN.search(zone)
        )
        operation = "kebatori_plus" if use_strong else "kebatori"

        payload = {
            "text": text,
            "profile": self.valves.KEBATORI_PROFILE,
        }

        return await self._run_bridge(
            operation,
            payload,
            source_type,
            __event_emitter__,
            __user__,
            __request__,
        )

    async def refine_text(
        self,
        instruction: str,
        __messages__: Optional[list] = None,
        __files__: Optional[list] = None,
        __user__: Optional[dict] = None,
        __request__: Any = None,
        __metadata__: Optional[dict] = None,
        __event_emitter__: Optional[Callable] = None,
    ) -> Any:
        """
        最新のユーザーメッセージまたは添付文書へ、指定された修正だけを反映する。

        :param instruction: 具体的な修正指示
        :return: 修正後の文章
        """
        text, source_type = await self._get_source_text(
            __messages__, __files__, __metadata__
        )
        if not text.strip():
            return "エラー: 処理対象の文章を取得できませんでした。"

        if not instruction or not instruction.strip():
            return "エラー: 修正指示が空です。"

        return await self._run_bridge(
            "refine",
            {"text": text, "instruction": instruction},
            source_type,
            __event_emitter__,
            __user__,
            __request__,
        )
