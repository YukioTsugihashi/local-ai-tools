"""
title: Excel分析
id: excel_analysis
author: local
version: 1.0.0
description: 添付したExcel(xlsx)のシート一覧・値の検索・範囲表示・CSV出力・コメント生成を、LLMを経由せず依頼文のキーワードで確実に実行するパイプ。ファイルはOpen WebUIのファイルストアからIDで直接読み込む。
model_description: Excel(xlsx)のシート一覧・値の検索・範囲表示・CSV出力・コメント生成をローカルで確実に実行します。Excelファイルを添付して、下のボタンを押すか依頼を入力してください。
suggestion: シート一覧 | ブックの構成と各シートの大きさ | シート一覧を見せて
suggestion: 値の検索 | 部分一致でセルを探す(『』で囲む) | 『◯◯』を検索して
suggestion: 範囲表示 | 指定した範囲を表で表示 | 『シート名』のA1:F20を見せて
suggestion: CSV出力 | シートをCSVファイルにして添付 | 『シート名』シートをCSVで出して
suggestion: コメント生成 | Excelにコメントを追加します | 分析内容を書く
requirements: openpyxl,httpx
"""

from __future__ import annotations

import base64
import inspect
import io
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx
from openpyxl import load_workbook
from pydantic import BaseModel, Field

EventEmitter = Callable[[dict[str, Any]], Awaitable[None]]

# 依頼文からの操作判定。上から順に評価し、最初に一致した操作だけを実行する。
# (文章処理パイプと同じ思想: LLMに判断させず、正規表現で確実に振り分ける)
OPERATION_PATTERNS = [
    ("range", re.compile(r"[A-Za-z]{1,3}\d{1,7}\s*[:：]\s*[A-Za-z]{1,3}\d{1,7}")),
    ("find", re.compile(r"検索|探して|さがして|どこにある|見つけて")),
    ("table", re.compile(r"[cC][sS][vV]|ｃｓｖ|表として出|表を出|書き出して|エクスポート")),
    ("comment", re.compile(r"コメント|分析内容|意見|評価")),
    ("index", re.compile(r"シート|構成|一覧|全体|概要|何が入って|どんな内容")),
]
# ハブの excel_read へ渡す操作。comment だけ別ツール(excel_comment)になる
READ_OPERATIONS = ("index", "find", "range", "table")
OPERATION_LABELS = {
    "index": "シート一覧",
    "find": "値の検索",
    "range": "範囲表示",
    "table": "CSV出力",
    "comment": "コメント生成",
}

# セル範囲(A1:C10)の抽出
RANGE_REF_PATTERN = re.compile(
    r"([A-Za-z]{1,3}\d{1,7})\s*[:：]\s*([A-Za-z]{1,3}\d{1,7})"
)
# 「」『』""'' で囲まれた語(検索語・シート名の指定)
QUOTED_PATTERN = re.compile(r"[「『\"']([^「」『』\"']+)[」』\"']")
# 引用符が無いときの検索語(「○○を検索」「○○を探して」)
FIND_TARGET_PATTERN = re.compile(
    r"([^\s、。「」『』]+?)\s*(?:を|の場所を|のセルを)\s*(?:検索|探して|さがして|見つけて)"
)

EXCEL_EXTENSIONS = (".xlsx", ".xlsm")

# 会話状態(最後に使ったファイルID)を保持する上限。古い会話から捨てる。
MAX_TRACKED_CHATS = 64
# 1会話あたりで「処理済み」と覚えておく添付IDの上限
MAX_TRACKED_FILE_IDS = 200

HELP_MESSAGE = (
    "Excelファイル(xlsx)を添付して、やりたいことを一緒に書いてください。\n\n"
    "- **シート一覧**: 「シート一覧を見せて」「どんな内容?」\n"
    "- **値の検索**(部分一致): 「『合計』を検索して」「4月はどこにある?」\n"
    "- **範囲表示**: 「Sheet1のA1:F20を見せて」(シート名は「」で囲むと確実)\n"
    "- **CSV出力**: 「『集計』シートをCSVで出して」\n"
    "- **コメント生成**: 「全対象シートにコメントを作って」"
    "(表と記入例を基に生成・検証して、処理済みExcelを添付)\n\n"
    "一度添付したファイルは、同じ会話なら添付し直さなくても続けて使えます。"
)


class Pipe:
    class Valves(BaseModel):
        MAX_FILE_MB: int = Field(
            default=100,
            description="読み込むExcelファイルの上限サイズ(MB)",
        )
        MAX_FIND_RESULTS: int = Field(
            default=50,
            description="検索で表示する最大ヒット数",
        )
        MAX_RANGE_CELLS: int = Field(
            default=1000,
            description="範囲表示で許可する最大セル数",
        )
        MAX_EXPORT_ROWS: int = Field(
            default=50000,
            description="CSV出力で許可する最大行数",
        )
        LOCAL_TOOL_BRIDGE_URL: str = Field(
            default="http://host.docker.internal:8010",
            description="Windows上のKiloローカルツール受付URL",
        )
        COMMENT_TIMEOUT_SECONDS: int = Field(
            default=1800,
            description="KiloのExcelコメント処理タイムアウト秒数",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()
        # chat_id -> {"files": [(file_id, name), ...]}
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
        """
        今回処理すべき添付だけを選ぶ。

        Open WebUIの `__files__` は**会話全体**のファイルを渡してくる。
        そのまま使うと、2回目以降の依頼で前のメッセージの添付まで作り直す
        (2ファイル添付して2回依頼すると4件処理される)。
        処理済みIDを覚えて、新しいものだけを対象にする。
        新しい添付が無いときは、直前に扱ったファイルへの続けての依頼とみなす。
        """
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
        # 会話が長くなっても、覚えるのは直近ぶんだけでよい
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
    def _excel_attachments(files: Optional[list]) -> list[tuple[str, str]]:
        """__files__ からExcel添付の (file_id, ファイル名) を集める。"""
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
            if file_id and name.lower().endswith(EXCEL_EXTENSIONS):
                results.append((str(file_id), name))
        return results

    async def _load_workbook_bytes(self, file_id: str, name: str) -> bytes:
        """Open WebUIのファイルストアからExcelの生バイト列を取得する。

        文章処理パイプが使う抽出済みテキスト(data.content)はxlsxでは使えない
        ため、ストレージ上の実ファイルを読む。
        """
        from open_webui.models.files import Files

        file_model = Files.get_file_by_id(str(file_id))
        if inspect.isawaitable(file_model):
            file_model = await file_model
        if file_model is None:
            raise RuntimeError(
                f"{name}: ファイルを取得できませんでした。添付し直してください"
            )

        stored_path = getattr(file_model, "path", None) or (
            (getattr(file_model, "meta", None) or {}).get("path")
        )
        if not stored_path:
            raise RuntimeError(
                f"{name}: ファイルの保存場所が分かりませんでした。添付し直してください"
            )

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
    # 結果ファイルの保存と添付(文章処理パイプと同じ方式)
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
            "metadata": {"source": "excel_analysis_pipe", "generated": True},
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
        (文章処理パイプと同じ実装。片方だけ直すと写経先が腐る)
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

    # ------------------------------------------------------------------
    # 読み取り処理はWindows側のハブ(tools/excel_reader.py)へ委譲する。
    # 以前はここでopenpyxlを直接扱っており、excel_reader.pyと二重実装だった。
    # ------------------------------------------------------------------
    async def _run_hub(
        self,
        tool: str,
        name: str,
        data: bytes,
        options: dict,
        user_info: Optional[dict],
        request: Any,
        event_emitter: Optional[EventEmitter],
        registry: Optional[list] = None,
    ) -> str:
        payload = {
            "tool": tool,
            "files": [
                {
                    "filename": name,
                    "content_b64": base64.b64encode(data).decode("ascii"),
                }
            ],
            "options": options,
        }
        async with httpx.AsyncClient(timeout=self.valves.COMMENT_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{self.valves.LOCAL_TOOL_BRIDGE_URL.rstrip('/')}/v1/run",
                json=payload,
            )
        if response.status_code >= 400:
            raise RuntimeError(f"ローカルツール {tool} HTTP {response.status_code}: {response.text[:1000]}")
        result = response.json()

        for item in result.get("files", []):
            file_bytes = base64.b64decode(item["content_b64"])
            filename = str(item["filename"])
            stored = await self._store_file(
                request=request,
                user_info=user_info,
                filename=filename,
                file_bytes=file_bytes,
                mime_type=self._mime_for(filename),
            )
            await self._attach_file(event_emitter, str(stored["id"]), filename, registry)

        message = str(result.get("message") or "")
        skipped = result.get("skipped") or []
        if skipped:
            message += "\n\n処理できなかったもの:\n  - " + "\n  - ".join(skipped)
        # 補足は「対処が要らない情報」。処理できなかったものと同じ見出しに
        # 並べると、成功したのに失敗したように見える(実際そう見えていた)
        notes = result.get("notes") or []
        if notes:
            message += "\n\n補足(処理は成功しています):\n  - " + "\n  - ".join(notes)
        return message

    @staticmethod
    def _mime_for(filename: str) -> str:
        lowered = filename.lower()
        if lowered.endswith(".xlsm"):
            return "application/vnd.ms-excel.sheet.macroEnabled.12"
        if lowered.endswith(".xlsx"):
            return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if lowered.endswith(".csv"):
            return "text/csv; charset=utf-8"
        return "application/octet-stream"

    @staticmethod
    def _sheet_names(data: bytes) -> list[str]:
        """依頼文からシートを選ぶために名前だけ読む(処理はハブ側)。"""
        workbook = load_workbook(io.BytesIO(data), read_only=True)
        try:
            return list(workbook.sheetnames)
        finally:
            workbook.close()

    # ------------------------------------------------------------------
    # 依頼文の解釈(純関数。テストはtools/test_excel_pipe_routing.py)
    # ------------------------------------------------------------------
    @staticmethod
    def detect_operation(text: str) -> str:
        for operation, pattern in OPERATION_PATTERNS:
            if pattern.search(text):
                return operation
        return "index"

    @staticmethod
    def extract_quoted(text: str) -> list[str]:
        return [m.strip() for m in QUOTED_PATTERN.findall(text) if m.strip()]

    @staticmethod
    def extract_find_query(text: str) -> str:
        quoted = Pipe.extract_quoted(text)
        if quoted:
            return quoted[0]
        match = FIND_TARGET_PATTERN.search(text)
        return match.group(1).strip() if match else ""

    @staticmethod
    def extract_range(text: str) -> str:
        match = RANGE_REF_PATTERN.search(text)
        return f"{match.group(1)}:{match.group(2)}" if match else ""

    @staticmethod
    def pick_sheet_name(text: str, sheet_names: list[str]) -> str:
        """依頼文からシート名を決める。指定が無ければ先頭シート。"""
        for candidate in Pipe.extract_quoted(text):
            if candidate in sheet_names:
                return candidate
        for sheet_name in sheet_names:
            if sheet_name and sheet_name in text:
                return sheet_name
        return sheet_names[0] if sheet_names else ""

    # ------------------------------------------------------------------
    # 本体(依頼の解釈と各操作への振り分けだけを行う)
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
        # タイトル生成などのバックグラウンドタスクには定型で応答する
        if __task__:
            return "Excel分析"

        message_text = self._latest_user_message(body)
        chat_id = __chat_id__ or ""

        attachments = self._pending_attachments(
            chat_id, self._excel_attachments(__files__)
        )
        if not attachments:
            if __files__:
                return (
                    "対応しているのはExcelファイル(.xlsx / .xlsm)だけです。\n\n"
                    + HELP_MESSAGE
                )
            return HELP_MESSAGE
        self._remember_handled(chat_id, attachments)

        operation = self.detect_operation(message_text)

        results: list[str] = []
        # 添付済みファイルの累積。複数添付しても最後の1件しか残らないのを防ぐ
        attached: list = []
        for file_id, name in attachments:
            try:
                data = await self._load_workbook_bytes(file_id, name)
                if operation == "comment":
                    tool, options = "excel_comment", {}
                else:
                    tool = "excel_read"
                    options = {"operation": operation if operation in READ_OPERATIONS else "index"}
                    if options["operation"] == "find":
                        query = self.extract_find_query(message_text)
                        if not query:
                            results.append(
                                "検索語が分かりませんでした。"
                                "「『合計』を検索して」のように『』で囲んでください"
                            )
                            continue
                        options["query"] = query
                    elif options["operation"] in ("range", "table"):
                        # どのシートを指しているかの判断だけはPipe側で行う(操作判定と同じ理由)
                        options["sheet"] = self.pick_sheet_name(
                            message_text, self._sheet_names(data)
                        )
                        if options["operation"] == "range":
                            options["range"] = self.extract_range(message_text)
                results.append(
                    await self._run_hub(
                        tool,
                        name,
                        data,
                        options,
                        __user__,
                        __request__,
                        __event_emitter__,
                        attached,
                    )
                )
            except RuntimeError as error:
                results.append(f"エラー: {error}")
            except Exception as error:  # 想定外はファイル名付きで報告する
                results.append(
                    f"エラー: {name} の処理に失敗しました"
                    f"({type(error).__name__}: {error})"
                )

        label = OPERATION_LABELS.get(operation, operation)
        return f"実行した操作: {label}\n\n" + "\n\n".join(results)
