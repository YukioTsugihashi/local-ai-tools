"""
title: 文章処理
id: text_processing
author: local
version: 3.0.0
description: LLMを経由せず、依頼文のキーワードで文章処理(ケバ取り・ケバ取り強・要約・議事録・整形)を確実に実行するパイプ。添付ファイルは全文を直接読み込み、結果はファイルとして添付する。会話の続きの追加指示は前回の処理を前提に再実行する。結果ファイルの生成(docx/xlsx/csv)と議事録テンプレートへの流し込みはブリッジ側(/v1/render/*)で行う
model_description: ケバ取り・要約・議事録・整形をローカルで確実に実行します。ファイルを添付して、下のボタンを押すか処理内容を入力してください。結果はテキストファイルとして自動添付されます。
suggestion: ケバ取り | フィラー・つなぎ語を除去(LLM判定併用) | これのケバ取りをお願いします
suggestion: 要約 | 文字数の指定なし | これを要約してください
suggestion: 議事録 | 概要・決定事項・対応事項 | これの議事録を作成してください
suggestion: 文章整形 | 内容を変えず読みやすく | これを整形してください
requirements: httpx
"""

from __future__ import annotations

import base64
import inspect
import io
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx
from pydantic import BaseModel, Field

EventEmitter = Callable[[dict[str, Any]], Awaitable[None]]

# 依頼文からの処理判定。上から順に評価し、一致した処理をすべて実行する。
OPERATION_PATTERNS = [
    ("kebatori", re.compile(r"ケバ[取と]り|けば[取と]り|毛羽|フィラー|言いよどみ")),
    ("rewrite", re.compile(r"整形|清書|読みやすく|リライト")),
    ("summarize", re.compile(r"要約|要点|サマリ|まとめ")),
    ("minutes", re.compile(r"議事録")),
]
OPERATION_LABELS = {
    "kebatori": "ケバ取り",
    "kebatori_plus": "ケバ取り強",
    "rewrite": "文章整形",
    "summarize": "要約",
    "minutes": "議事録",
}
# ケバ取りは既定でLLM判定併用の「ケバ取り強」を実行する(Valvesで変更可)。
# この語が併記されたときだけ、ルールのみの高速版へ切り替える。
FAST_KEBATORI_PATTERN = re.compile(
    r"高速|簡易|軽く|軽めに|さっと|サッと|クイック|急ぎ|ルールのみ"
)
# 明示的に強版を指す語(既定が高速版設定のときに強版へ切り替える)。
STRONG_KEBATORI_PATTERN = re.compile(
    r"しっかり|強め|強く|強で|徹底|ガッツリ|がっつり|入念|念入り|ケバ取り強|強ケバ"
)
MAX_CHARS_PATTERN = re.compile(r"(\d{2,6})\s*(?:文字|字)\s*(?:以内|程度|目安|で)")
COMMAND_LINE_PATTERN = re.compile(
    r"して|お願|ください|下さい|頼み|たのみ|よろしく|実行|かけて"
)

# 出力形式の指定。依頼文にこれがあれば、その形式でも結果ファイルを作る。
# LLMの判断を挟まないため、書いてあれば必ずその形式が出る。
FORMAT_PATTERNS = [
    ("docx", re.compile(r"[wWｗＷ]ord|ワード|ﾜｰﾄﾞ|docx|DOCX")),
    ("xlsx", re.compile(r"[eE]xcel|エクセル|ｴｸｾﾙ|xlsx|XLSX|表計算|スプレッドシート")),
    ("csv", re.compile(r"csv|CSV|ｃｓｖ")),
    ("md", re.compile(r"マークダウン|[mM]arkdown|\.md\b|md形式|MD形式")),
    ("txt", re.compile(r"テキストファイル|txt|TXT|プレーンテキスト")),
]
FORMAT_LABELS = {
    "txt": "テキスト",
    "md": "Markdown",
    "docx": "Word",
    "csv": "CSV",
    "xlsx": "Excel",
}
FORMAT_MIME = {
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "docx": (
        "application/vnd.openxmlformats-officedocument"
        ".wordprocessingml.document"
    ),
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

# 処理済みの会話では、処理名の無いメッセージは原則すべて追加指示として扱う。
# 言い回しは無限にあるため「指示語を探す」方式では取りこぼす。
# 代わりに、明らかに指示ではないものだけを除外する。
NOT_INSTRUCTION_PATTERN = re.compile(
    r"^\s*(?:"
    r"こんにちは|こんばんは|おはよう|やあ|どうも|はじめまして"
    r"|ありがとう|ありがと|thanks?|thank you|サンクス|助かった|助かります"
    r"|了解|りょうかい|わかった|分かった|承知|ok|okay|オーケー|はい|うん|そう"
    r"|お疲れ(?:さま|様)?|おつかれ(?:さま)?|さようなら|バイバイ|またね"
    r"|すごい|いいね|なるほど|へえ|ふーん"
    r")(?:です|でした|ございます)?[\s。、!！?？…~〜ー]*$",
    re.IGNORECASE,
)
# 追加指示として扱う最短の長さ。これ未満の断片は誤爆を避けて無視する。
MIN_INSTRUCTION_CHARS = 3
# 次第が無いときの議事録の書式。完成済み議事録の体裁(発言者名：内容)に合わせる。
PLAIN_MINUTES_STYLE = (
    "出力形式を指定します。次の構成で、すべて箇条書きで出力してください。\n"
    "概要：\n"
    "（会議全体の要旨を2〜4文で）\n"
    "協議内容：\n"
    "・発言者名：発言の要旨（1発言1行、会議の順のとおり）\n"
    "（質問とその回答は必ずセットで、やり取りの順のまま載せる。短い回答も省かない）\n"
    "決定事項：\n"
    "・（決まったことだけを1件1行）\n"
    "対応事項：\n"
    "・（誰が何をいつまでに行うか。1件1行）\n"
    "\n"
    "規則:\n"
    "- 発言者名は文字起こしから確実に分かる場合だけ書きます。"
    "名乗った発言、名指しで紹介された直後の発言などです。\n"
    "- 分からない発言者は名前を書かず、半角スペース2つに続けて「：」で始めます。"
    "議長・担当者・医師のような役割名で埋めることは禁止します。推測もしません。\n"
    "- 会議の開催日時や時刻の羅列は出力しません。\n"
    "- 数値・金額・件数・期限・固有名詞は省略しません。"
)

# 次第(アジェンダ)らしさの判定。文字起こしと見分けるために使う。
AGENDA_HINT = re.compile(r"次\s*第|議\s*題|次回開催予定")

# 出席者名簿らしさの判定。名簿を文字起こしと取り違えると、名簿から
# 発言0件の議事録が生まれ、本物の文字起こしが名簿として捨てられる。
ROSTER_NAME_HINTS = ("名簿", "出席者", "参加者", "メンバー", "roster")
ROSTER_COLUMN_WORDS = ("氏名", "役職", "職名", "所属", "部署", "委員", "出席", "欠席")
# 表で来るのは名簿。文字起こしは docx / txt で来る
ROSTER_EXTENSIONS = (".xlsx", ".xlsm", ".xls", ".csv")

# 会話状態を保持する上限。古い会話から捨てる。
MAX_TRACKED_CHATS = 64
# 1会話あたりで「処理済み」と覚えておく添付IDの上限
MAX_TRACKED_FILE_IDS = 200

HELP_MESSAGE = (
    "処理内容を指定してください。使える指示は次のとおりです。\n\n"
    "- **ケバ取り**(フィラー・つなぎ語の除去): 「ケバ取りして」\n"
    "- **高速ケバ取り**(定義済みルールのみ・数秒): 「高速ケバ取りして」\n"
    "- **文章整形**(内容を変えず読みやすく): 「整形して」\n"
    "- **要約**: 「要約して」「800字以内で要約して」\n"
    "- **議事録**: 「議事録にして」\n\n"
    "文章はファイル添付(推奨)か、指示と一緒に貼り付けてください。"
    "複数指定(例: 「ケバ取りと議事録をお願い」)もできます。\n\n"
    "出力形式は依頼文に書けば切り替わります(既定はテキスト)。\n"
    "- 「議事録をWordで出して」→ .docx\n"
    "- 「要約をExcelで」→ .xlsx / 「CSVで」→ .csv / 「Markdownで」→ .md\n"
    "- 「WordとExcelで」のように複数同時も可\n"
    "- 処理済みなら「Wordで出して」だけで、やり直さず前回結果を書き出します"
)


class Pipe:
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
            description="ケバ取りで使用するプロファイル名",
        )
        KEBATORI_DEFAULT_STRONG: bool = Field(
            default=True,
            description=(
                "ケバ取り依頼を既定で「ケバ取り強」(LLM判定併用)として実行する。"
                "無効にするとルールのみの高速版が既定になる"
            ),
        )
        MINUTES_TEMPLATE_PATH: str = Field(
            default="/app/backend/data/templates/minutes_template.docx",
            description=(
                "議事録テンプレートのdocxパス。次第を一緒に添付したとき、"
                "この書式で議事録を出力する。空にすると通常の議事録になる"
            ),
        )

        DEFAULT_OUTPUT_FORMAT: str = Field(
            default="txt",
            description=(
                "出力形式の指定が依頼文に無いときに使う既定形式。"
                "txt / md / docx / csv / xlsx のいずれか"
            ),
        )

        INLINE_RESULT_MAX_CHARS: int = Field(
            default=1000,
            description="この文字数以下の結果はチャットにも本文を表示する",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()
        # chat_id -> {"base_text", "base_name", "operations", "result"}
        # base_text は処理の土台となる本文。ケバ取り・整形のような非圧縮の処理は
        # 結果で土台を更新し、要約・議事録のような圧縮処理は土台を変えない。
        # これにより「要約して」→「もっと詳細に」で原文へ戻って再実行できる。
        self._chats: dict[str, dict] = {}

    def _remember(self, chat_id: str, **fields) -> None:
        if not chat_id:
            return
        state = self._chats.setdefault(chat_id, {})
        state.update(fields)
        while len(self._chats) > MAX_TRACKED_CHATS:
            self._chats.pop(next(iter(self._chats)))

    def _recall(self, chat_id: str) -> dict:
        return self._chats.get(chat_id or "", {})

    def _pending_files(self, chat_id: str, files: Optional[list]) -> list:
        """
        今回処理すべき添付だけを選ぶ。

        Open WebUIの `__files__` は**会話全体**のファイルを渡してくる。
        そのまま使うと、2回目以降の依頼で前のメッセージの添付まで巻き込む
        (前の議事録と新しい文字起こしが1本に混ざる)。
        処理済みIDを覚えて、新しいものだけを対象にする。
        新しい添付が無いときは空を返し、会話の土台(前回の処理結果)へ委ねる。
        """
        handled = set(self._recall(chat_id).get("handled") or [])
        fresh = []
        for entry in files or []:
            if not isinstance(entry, dict):
                continue
            file_info = entry.get("file") if isinstance(entry.get("file"), dict) else {}
            file_id = entry.get("id") or file_info.get("id")
            if file_id and str(file_id) in handled:
                continue
            fresh.append(entry)
        return fresh

    def _remember_handled(self, chat_id: str, files: Optional[list]) -> None:
        handled = list(self._recall(chat_id).get("handled") or [])
        for entry in files or []:
            if not isinstance(entry, dict):
                continue
            file_info = entry.get("file") if isinstance(entry.get("file"), dict) else {}
            file_id = entry.get("id") or file_info.get("id")
            if file_id and str(file_id) not in handled:
                handled.append(str(file_id))
        # 会話が長くなっても、覚えるのは直近ぶんだけでよい
        self._remember(chat_id, handled=handled[-MAX_TRACKED_FILE_IDS:])

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
    async def _load_full_file_contents(
        files: Optional[list],
    ) -> list[tuple[str, str]]:
        """
        添付ファイルの全文をOpen WebUIのファイルストアからIDで直接取得する。

        RAG(検索)経由の断片ではなく、抽出済みの全文を使うことで
        長文の途中欠落を防ぐ。
        """
        results: list[tuple[str, str]] = []
        if not files:
            return results

        from open_webui.models.files import Files

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

            if not content:
                data = file_info.get("data") if isinstance(file_info.get("data"), dict) else {}
                content = (data.get("content") or "").strip()

            if content:
                results.append((str(name), content))
        return results

    @staticmethod
    def _strip_command_lines(text: str) -> str:
        """貼り付け本文から、指示だけの短い行(先頭・末尾)を取り除く。"""
        lines = text.split("\n")

        def is_command(line: str) -> bool:
            stripped = line.strip()
            if not stripped or len(stripped) > 60:
                return False
            if not any(p.search(stripped) for _, p in OPERATION_PATTERNS):
                return False
            return bool(COMMAND_LINE_PATTERN.search(stripped))

        while lines and is_command(lines[0]):
            lines.pop(0)
        while lines and is_command(lines[-1]):
            lines.pop()
        return "\n".join(lines).strip()

    # ------------------------------------------------------------------
    # 出力形式(ファイル生成の実体はブリッジ /v1/render/* にある)
    # ------------------------------------------------------------------
    @staticmethod
    def _safe_filename(stem: str, extension: str) -> str:
        raw = Path((stem or "output").strip()).name
        name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", raw).strip(" .")
        return f"{(name or 'output')[:120]}.{extension}"

    async def _render_output(
        self, output_format: str, content: str, title: str
    ) -> tuple[bytes, str]:
        """指定形式のバイト列とMIMEタイプをブリッジから受け取る。"""
        url = self.valves.BRIDGE_BASE_URL.rstrip("/") + "/v1/render/export"
        timeout = httpx.Timeout(float(self.valves.TIMEOUT_SECONDS), connect=30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                url,
                json={
                    "content": content,
                    "format": output_format,
                    "title": title,
                },
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"出力ファイルの生成に失敗しました(HTTP {response.status_code})"
            )
        body = response.json()
        return base64.b64decode(body["data_b64"]), str(body["mime"])

    async def _render_minutes_docx(
        self, template_bytes: bytes, document: dict
    ) -> bytes:
        """テンプレート書式の議事録docxをブリッジで生成する。"""
        url = self.valves.BRIDGE_BASE_URL.rstrip("/") + "/v1/render/minutes_docx"
        timeout = httpx.Timeout(float(self.valves.TIMEOUT_SECONDS), connect=30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                url,
                json={
                    "document": document,
                    "template_b64": base64.b64encode(template_bytes).decode(
                        "ascii"
                    ),
                },
            )
        if response.status_code >= 400:
            try:
                detail = str(response.json().get("detail", ""))[:300]
            except ValueError:
                detail = response.text[:300]
            raise RuntimeError(f"HTTP {response.status_code}: {detail}")
        return base64.b64decode(response.json()["data_b64"])

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
            "metadata": {"source": "text_processing_pipe", "generated": True},
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
    async def _emit_status(
        event_emitter: Optional[EventEmitter], description: str, done: bool
    ) -> None:
        if event_emitter is None:
            return
        try:
            await event_emitter(
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
            pass

    async def _export_only(
        self,
        *,
        result_text: str,
        label: str,
        output_formats: list[str],
        user_info: Optional[dict],
        request: Any,
        event_emitter: Optional[EventEmitter],
        registry: Optional[list] = None,
    ) -> str:
        """処理はやり直さず、直前の結果を指定形式で書き出し直す。"""
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        attached: list[str] = []
        failures: list[str] = []

        for output_format in output_formats:
            filename = self._safe_filename(f"{label}_{timestamp}", output_format)
            try:
                file_bytes, mime_type = await self._render_output(
                    output_format, result_text, f"{label} {timestamp}"
                )
                stored = await self._store_file(
                    request=request,
                    user_info=user_info,
                    filename=filename,
                    file_bytes=file_bytes,
                    mime_type=mime_type,
                )
                await self._attach_file(
                    event_emitter,
                    str(stored["id"]),
                    str(stored.get("filename") or filename),
                    registry,
                )
                attached.append(str(stored.get("filename") or filename))
            except Exception as exc:
                failures.append(
                    f"{FORMAT_LABELS.get(output_format, output_format)}: {exc}"
                )

        lines = [f"前回の{label}の結果を書き出しました({len(result_text):,}字)。"]
        if attached:
            lines.append("✅ " + "、".join(f"**{n}**" for n in attached))
        if failures:
            lines.append("❌ 出力に失敗: " + " / ".join(failures))
        return "\n\n".join(lines)


    # ------------------------------------------------------------------
    # 議事録テンプレート出力
    # ------------------------------------------------------------------
    @staticmethod
    def _agenda_score(name: str, text: str) -> int:
        """次第らしさの採点。構造の特徴で文字起こしと見分ける。"""
        score = 0
        if "次第" in name:
            score += 6
        if re.search(r"次\s*第", text[:300]):
            score += 4
        if re.search(r"[・･]{4,}", text):
            score += 3  # 点線リーダーは次第特有
        score += min(len(re.findall(r"資料\s*[0-9０-９]", text)), 5)
        if "次回開催予定" in text:
            score += 2
        if re.search(r"議\s*題", text):
            score += 2
        # 番号付き項目行(① … / 1 …)が並ぶのも次第の特徴
        if len(re.findall(r"^[\s　]*[①-⑳0-9０-９][\s　.．]", text, re.M)) >= 3:
            score += 3
        # 文字起こしの特徴: 長い・会話文が多い
        if len(text) > 4000:
            score -= 8
        elif len(text) > 2500:
            score -= 4
        spoken = len(re.findall(r"です。|ます。|ですね|ですけど|んで、", text))
        if spoken > 20:
            score -= 5
        elif spoken > 8:
            score -= 2
        return score

    @staticmethod
    def _split_agenda(contents: list[tuple[str, str]]) -> tuple[str, list]:
        """
        添付から次第を1つ選び分ける。

        語の出現数だけでは、文字起こし内の「議題について…」という発話に
        反応して誤認するため、構造の特徴まで採点し、しきい値未満なら
        次第なしとみなす。
        """
        if len(contents) < 2:
            return "", contents

        scored = sorted(
            (
                (Pipe._agenda_score(name, text), -len(text), index)
                for index, (name, text) in enumerate(contents)
            ),
            reverse=True,
        )
        best_score, _, best_index = scored[0]
        if best_score < 6:
            return "", contents

        agenda_text = contents[best_index][1]
        rest = [c for i, c in enumerate(contents) if i != best_index]
        return agenda_text, rest

    @staticmethod
    def _roster_score(name: str, text: str) -> int:
        """出席者名簿らしさの採点。次第と同じく、構造の特徴で見分ける。"""
        score = 0
        lowered = name.lower()
        if any(hint in name or hint in lowered for hint in ROSTER_NAME_HINTS):
            score += 6
        if lowered.endswith(ROSTER_EXTENSIONS):
            score += 3
        head = text[:600]
        score += min(sum(word in head for word in ROSTER_COLUMN_WORDS), 4)
        # 文字起こしの特徴: 長い・会話文が多い。名簿はどちらでもない
        if len(text) > 4000:
            score -= 8
        elif len(text) > 2000:
            score -= 3
        spoken = len(re.findall(r"です。|ます。|ですね|ですけど|んで、", text))
        if spoken > 20:
            score -= 6
        elif spoken > 8:
            score -= 2
        return score

    @staticmethod
    def _split_roster(contents: list[tuple[str, str]]) -> tuple[str, list]:
        """
        添付から出席者名簿を選び分ける。

        以前は「次第があるとき、2番目以降の添付が名簿」という順番頼みだった。
        そのため次第→名簿→文字起こしの順で渡すと、名簿を文字起こしとして
        処理し、本物の文字起こしを名簿として捨てていた(発言0件。2026-08-18に実発生)。
        次第が無いときは名簿を見分けもせず、名簿から空の議事録が1本できていた。
        順番ではなく中身で選ぶ。
        """
        if len(contents) < 2:
            return "", contents
        picked = [
            index
            for index, (name, text) in enumerate(contents)
            if Pipe._roster_score(name, text) >= 6
        ]
        # 全部が名簿に見えるときは選ばない(本文が無くなるため)
        if not picked or len(picked) >= len(contents):
            return "", contents
        roster_text = "\n".join(contents[index][1] for index in picked)
        rest = [item for index, item in enumerate(contents) if index not in set(picked)]
        return roster_text, rest

    async def _run_structured_minutes(self, payload: dict) -> dict:
        url = self.valves.BRIDGE_BASE_URL.rstrip("/") + "/v1/text/minutes_structured"
        timeout = httpx.Timeout(float(self.valves.TIMEOUT_SECONDS), connect=30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload)
        if response.status_code >= 400:
            try:
                detail = str(response.json().get("detail", ""))[:500]
            except ValueError:
                detail = response.text[:500]
            raise RuntimeError(f"HTTP {response.status_code}: {detail}")
        return response.json()

    async def _export_minutes_docx(
        self,
        document: dict,
        user_info,
        request,
        event_emitter,
        stem: str = "",
        registry: Optional[list] = None,
    ) -> str:
        """テンプレート書式の議事録docxを作って添付する。"""
        path = (self.valves.MINUTES_TEMPLATE_PATH or "").strip()
        if not path or not os.path.exists(path):
            return f"❌ 議事録テンプレートが見つかりません: {path or '(未設定)'}"

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        prefix = f"議事録_{stem}_" if stem else "議事録_"
        filename = self._safe_filename(f"{prefix}{timestamp}", "docx")
        try:
            with open(path, "rb") as handle:
                template_bytes = handle.read()
            file_bytes = await self._render_minutes_docx(template_bytes, document)
            stored = await self._store_file(
                request=request,
                user_info=user_info,
                filename=filename,
                file_bytes=file_bytes,
                mime_type=FORMAT_MIME["docx"],
            )
            await self._attach_file(
                event_emitter,
                str(stored["id"]),
                str(stored.get("filename") or filename),
                registry,
            )
            return f"✅ **{stored.get('filename') or filename}** を添付しました"
        except Exception as exc:
            return f"❌ 議事録の書き出しに失敗しました: {exc}"

    async def _minutes_for_each_file(
        self,
        contents: list,
        output_formats: list,
        user_info,
        request,
        event_emitter,
        registry: Optional[list] = None,
    ) -> str:
        """
        複数の文字起こしを1ファイルずつ議事録にして個別に添付する。

        各ファイルとも単一ファイルと同じ構造化経路を通る。次第が無いので
        議題構成は推定(黄マーカー)になり、テンプレート書式のWordが付く。
        """
        sections: list[str] = []
        total = len(contents)

        for index, (name, content) in enumerate(contents, start=1):
            await self._emit_status(
                event_emitter,
                f"議事録を作成しています({index}/{total}: {name})",
                False,
            )
            try:
                result = await self._run_structured_minutes(
                    {"text": content, "agenda_text": "", "instruction": ""}
                )
            except httpx.TimeoutException:
                sections.append(f"❌ **{name}**: タイムアウトしました")
                continue
            except Exception as exc:
                sections.append(f"❌ **{name}**: 失敗しました({exc})")
                continue

            document = result.get("document") or {}
            note = await self._export_minutes_docx(
                document,
                user_info,
                request,
                event_emitter,
                stem=Path(name).stem,
                registry=registry,
            )
            line = f"✅ **{name}**: 発言{result.get('assigned_remarks', 0)}件 {note}"
            warnings = [
                str(w) for w in (result.get("warnings") or []) if str(w).strip()
            ]
            if warnings:
                line += "\n  - " + " / ".join(warnings)
            sections.append(line)
            await self._emit_status(
                event_emitter, f"議事録が完了しました({index}/{total})", True
            )

        sections.append(
            "🟩 文字起こしから特定 / 🟨 推定 / 🟥 不明(要記入)"
        )
        sections.append(
            "個別の修正指示は、対象のファイルだけを添付した新しいチャットで行ってください。"
        )
        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # ブリッジ呼び出し
    # ------------------------------------------------------------------
    async def _run_bridge(self, operation: str, payload: dict) -> dict:
        url = self.valves.BRIDGE_BASE_URL.rstrip("/") + f"/v1/text/{operation}"
        timeout = httpx.Timeout(float(self.valves.TIMEOUT_SECONDS), connect=30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload)
        if response.status_code >= 400:
            try:
                detail = str(response.json().get("detail", ""))[:500]
            except ValueError:
                detail = response.text[:500]
            raise RuntimeError(f"HTTP {response.status_code}: {detail}")
        body = response.json()
        if not isinstance(body.get("text"), str):
            raise RuntimeError("ブリッジの応答に本文がありません")
        return body

    # ------------------------------------------------------------------
    # 依頼文の解釈
    # ------------------------------------------------------------------
    @staticmethod
    def _detect_operations(zone: str) -> list[str]:
        """依頼文(先頭・末尾の判定範囲)から実行する処理を拾う。"""
        return [op for op, pattern in OPERATION_PATTERNS if pattern.search(zone)]

    def _resolve_output_formats(self, message_text: str) -> tuple[list[str], list[str]]:
        """
        出力形式は依頼文全体から拾う。複数指定(「WordとExcelで」)にも対応する。

        返り値は (依頼文で明示された形式, 実際に使う形式)。
        明示が無ければ既定形式(Valves)を使う。
        """
        requested = [
            fmt for fmt, pattern in FORMAT_PATTERNS if pattern.search(message_text)
        ]
        default_format = (self.valves.DEFAULT_OUTPUT_FORMAT or "txt").strip().lower()
        effective = requested or [
            default_format if default_format in FORMAT_MIME else "txt"
        ]
        return requested, effective

    def _apply_kebatori_strength(self, operations: list[str], zone: str) -> list[str]:
        """ケバ取りの強弱を依頼文の語(高速/しっかり等)と既定設定で決める。"""
        if "kebatori" not in operations:
            return operations
        if self.valves.KEBATORI_DEFAULT_STRONG:
            use_strong = not FAST_KEBATORI_PATTERN.search(zone)
        else:
            use_strong = bool(STRONG_KEBATORI_PATTERN.search(zone))
        if not use_strong:
            return operations
        return [
            "kebatori_plus" if op == "kebatori" else op for op in operations
        ]

    @staticmethod
    def _extract_max_chars(zone: str) -> int:
        """「800字以内で」のような文字数指定を取り出す。無ければ0。"""
        match = MAX_CHARS_PATTERN.search(zone)
        if not match:
            return 0
        try:
            return int(match.group(1))
        except ValueError:
            return 0

    @staticmethod
    def _resolve_follow_up(
        operations: list[str],
        instruction_text: str,
        state: dict,
        has_new_files: bool,
    ) -> tuple[list[str], str]:
        """
        処理名が無いメッセージの扱いと、追加指示の決定。

        一度処理を実行した会話では、以降の入力は原則すべて「前回への追加指示」
        とみなす。「稼働率の話を厚めに」のような言い回しを取りこぼさないため、
        指示語を探すのではなく、挨拶や相槌だけを除外する方式にしている。

        返り値は (実行する処理, 追加指示)。処理が空ならヘルプを出す。
        """
        if operations:
            # 処理名があり、新しい添付も無い(=会話の続き)場合は、依頼文を
            # 指示としても渡す。「議事録を決定事項中心で」のような限定に追随できる。
            # 新しいファイルが添付されたときは仕切り直しなので渡さない。
            if state.get("base_text") and not has_new_files:
                return operations, instruction_text
            return operations, ""

        previous = state.get("operations") or []
        is_instruction = (
            len(instruction_text) >= MIN_INSTRUCTION_CHARS
            and not NOT_INSTRUCTION_PATTERN.match(instruction_text)
        )
        if previous and is_instruction:
            # 直前が複数処理でも、やり直すのは最後の処理だけにする。
            # 「要約をもっと詳細に」のように処理名が書かれていれば
            # そちらがOPERATION_PATTERNSで拾われ、ここには来ない。
            return previous[-1:], instruction_text
        return [], ""

    async def _prepare_sources(
        self, message_text: str, state: dict, files: Optional[list]
    ) -> tuple[list, str, str, str, str]:
        """
        本文の決定: 添付ファイル優先(IDから全文取得)、
        次に会話の土台(前回までの処理結果)、最後に貼り付け本文。

        返り値は (次第以外の添付, 次第, 名簿, 本文の名前, 本文)。
        """
        file_contents = await self._load_full_file_contents(files)
        # 名簿を先に抜く。名簿は「出席者名簿.xlsx」のように名前で確実に見分けられる
        # のに対し、次第は構造の採点なので取り違える。実際、名簿が次第として
        # 持って行かれ、残り1件になって名簿が使われないままだった
        # (2026-08-18の実運用ログ: agenda_chars=809 / roster_chars=0)。
        # 名簿と見分けられなかった複数の添付は、今までどおり
        # 「複数会議の文字起こし」として1本ずつ処理する。
        roster_text, file_contents = self._split_roster(file_contents)
        agenda_text, file_contents = self._split_agenda(file_contents)

        if file_contents:
            if len(file_contents) == 1:
                source_name, source_text = file_contents[0]
            else:
                source_name = f"添付{len(file_contents)}ファイル"
                source_text = "\n\n".join(
                    f"===== {name} =====\n{content}"
                    for name, content in file_contents
                )
        else:
            source_text = self._strip_command_lines(message_text)
            source_name = "貼り付け本文"
            if not source_text.strip() and state.get("base_text"):
                source_text = state["base_text"]
                source_name = state.get("base_name") or "前回の本文"
        return file_contents, agenda_text, roster_text, source_name, source_text

    # ------------------------------------------------------------------
    # 処理ごとのハンドラー
    # ------------------------------------------------------------------
    async def _handle_structured_minutes(
        self,
        *,
        source_name: str,
        source_text: str,
        agenda_text: str,
        roster_text: str,
        follow_up_instruction: str,
        state: dict,
        chat_id: str,
        user_info: Optional[dict],
        request: Any,
        event_emitter: Optional[EventEmitter],
        attached_files: list,
    ) -> str:
        """次第の骨格に沿った構造化議事録を作り、テンプレ書式のdocxを添付する。"""
        agenda_source = agenda_text or state.get("agenda_text", "")
        await self._emit_status(
            event_emitter,
            f"議事録を作成しています({len(source_text):,}字)",
            False,
        )
        try:
            result = await self._run_structured_minutes(
                {
                    "text": source_text,
                    "agenda_text": agenda_source,
                    "roster_text": roster_text or state.get("roster_text", ""),
                    "instruction": follow_up_instruction,
                }
            )
        except httpx.TimeoutException:
            return f"❌ 議事録: タイムアウトしました(上限{self.valves.TIMEOUT_SECONDS}秒)"
        except Exception as exc:
            return f"❌ 議事録: 失敗しました({exc})"

        document = result.get("document") or {}
        self._remember(
            chat_id,
            agenda_text=agenda_source,
            roster_text=roster_text or state.get("roster_text", ""),
            base_text=source_text,
            base_name=source_name,
            operations=["minutes"],
            last_label="議事録",
            minutes_doc=document,
        )

        attach_note = await self._export_minutes_docx(
            document,
            user_info,
            request,
            event_emitter,
            registry=attached_files,
        )
        sections = [
            "🟩 文字起こしから特定 / 🟨 次第から補完・推定 / 🟥 不明(要記入)",
            "修正したい点があれば、そのまま指示してください"
            "（例:「議題3の救急の話は議題2に移して」）。"
            "修正後は作り直したファイルを再添付します。",
        ]
        warnings = [w for w in (result.get("warnings") or []) if str(w).strip()]
        if warnings:
            sections.append("⚠ " + " / ".join(warnings))
        sections.append(attach_note)
        await self._emit_status(
            event_emitter,
            f"議事録ができました(発言{result.get('assigned_remarks', 0)}件)",
            True,
        )
        return "\n\n".join(sections)

    async def _run_operation_chain(
        self,
        *,
        operations: list[str],
        source_name: str,
        source_text: str,
        output_formats: list[str],
        max_chars: int,
        follow_up_instruction: str,
        chat_id: str,
        user_info: Optional[dict],
        request: Any,
        event_emitter: Optional[EventEmitter],
        attached_files: list,
    ) -> str:
        """ケバ取り・整形・要約・通常議事録を順に実行し、結果を書き出す。"""
        report_lines: list[str] = []
        inline_sections: list[str] = []
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

        # 「ケバ取りしたうえで要約と議事録」のような依頼では、
        # ケバ取り・整形の結果を後続処理の入力へ引き継ぐ。
        # 要約・議事録は情報を落とすため、後続へは引き継がない。
        working_text = source_text
        working_name = source_name

        for operation in operations:
            label = OPERATION_LABELS[operation]
            await self._emit_status(
                event_emitter,
                f"{label}を実行しています({len(working_text):,}字)",
                False,
            )

            payload: dict[str, Any] = {"text": working_text}
            if operation in ("kebatori", "kebatori_plus"):
                payload["profile"] = self.valves.KEBATORI_PROFILE
            if operation in ("summarize", "minutes") and max_chars > 0:
                payload["max_chars"] = max_chars
            if follow_up_instruction:
                payload["instruction"] = (
                    "これは前回の処理結果に対する追加指示です。"
                    "同じ原文を対象に、次の指示を反映してやり直してください: "
                    + follow_up_instruction
                )
            if operation == "minutes":
                payload["instruction"] = "\n\n".join(
                    part
                    for part in (
                        PLAIN_MINUTES_STYLE,
                        payload.get("instruction", ""),
                    )
                    if part
                )

            try:
                result = await self._run_bridge(operation, payload)
            except httpx.TimeoutException:
                report_lines.append(
                    f"❌ {label}: タイムアウトしました"
                    f"(上限{self.valves.TIMEOUT_SECONDS}秒)"
                )
                continue
            except Exception as exc:
                report_lines.append(f"❌ {label}: 失敗しました({exc})")
                continue

            result_text = result["text"]
            source_chars = int(result.get("source_chars") or len(working_text))
            result_chars = int(result.get("result_chars") or len(result_text))
            warnings = [
                str(w) for w in (result.get("warnings") or []) if str(w).strip()
            ]

            # 依頼文で指定された形式すべてで書き出す。指定が無ければ既定形式。
            attached: list[str] = []
            failures: list[str] = []
            for output_format in output_formats:
                filename = self._safe_filename(
                    f"{label}_{timestamp}", output_format
                )
                try:
                    file_bytes, mime_type = await self._render_output(
                        output_format, result_text, f"{label} {timestamp}"
                    )
                    stored = await self._store_file(
                        request=request,
                        user_info=user_info,
                        filename=filename,
                        file_bytes=file_bytes,
                        mime_type=mime_type,
                    )
                    await self._attach_file(
                        event_emitter,
                        str(stored["id"]),
                        str(stored.get("filename") or filename),
                        attached_files,
                    )
                    attached.append(str(stored.get("filename") or filename))
                except Exception as exc:
                    failures.append(f"{FORMAT_LABELS.get(output_format, output_format)}: {exc}")

            if attached:
                file_note = " → " + "、".join(f"**{n}**" for n in attached) + " を添付しました"
            else:
                file_note = ""
            if failures:
                file_note += f"(出力に失敗: {' / '.join(failures)})"
                if f"### {label}の結果" not in "".join(inline_sections):
                    inline_sections.append(f"### {label}の結果\n{result_text}")

            chain_note = "" if working_name == source_name else f"({working_name}から)"
            line = (
                f"✅ **{label}**{chain_note} 完了: "
                f"{source_chars:,}字 → {result_chars:,}字{file_note}"
            )
            if warnings:
                line += f"\n  - " + " / ".join(warnings)
            report_lines.append(line)

            if (
                result_chars <= self.valves.INLINE_RESULT_MAX_CHARS
                and f"### {label}の結果" not in "".join(inline_sections)
            ):
                inline_sections.append(f"### {label}の結果\n{result_text}")

            # ケバ取り・整形は情報を落とさないため、同じ依頼内の後続処理と
            # 次回以降の土台を、その結果で更新する。
            # 要約・議事録は圧縮するため土台を変えず、原文を保ったままにする。
            if operation in ("kebatori", "kebatori_plus", "rewrite"):
                working_text = result_text
                working_name = f"{label}済みの本文"
                self._remember(
                    chat_id,
                    base_text=result_text,
                    base_name=working_name,
                )
            self._remember(
                chat_id,
                operations=operations,
                result=result_text,
                last_label=label,
            )
            if not self._recall(chat_id).get("base_text"):
                self._remember(
                    chat_id, base_text=source_text, base_name=source_name
                )

            await self._emit_status(
                event_emitter,
                f"{label}が完了しました({source_chars:,}字 → {result_chars:,}字)",
                True,
            )

        header = f"処理対象: {source_name}({len(source_text):,}字)"
        if follow_up_instruction:
            header += "\n追加指示を反映して前回と同じ処理をやり直しました。"
        sections = [header, "\n".join(report_lines)]
        if inline_sections:
            sections.append("\n\n".join(inline_sections))
        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # 本体(依頼の解釈と各ハンドラーへの振り分けだけを行う)
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
        # タイトル生成などのバックグラウンドタスクには定型で応答し、
        # ブリッジ処理を走らせない。
        if __task__:
            return "文章処理"

        message_text = self._latest_user_message(body)
        if not message_text.strip():
            return HELP_MESSAGE

        # 指示は先頭・末尾に書かれることが多いため、その範囲だけで判定する。
        zone = message_text[:200] + "\n" + message_text[-200:]
        operations = self._detect_operations(zone)
        requested_formats, output_formats = self._resolve_output_formats(message_text)

        chat_id = __chat_id__ or ""
        state = self._recall(chat_id)

        # この応答で添付したファイルの累積。画面表示イベントの再送に使う。
        attached_files: list[dict] = []

        # 議事録の下書きがある状態で形式だけ指定されたら、テンプレート書式で出す。
        if not operations and "docx" in requested_formats and state.get("minutes_doc"):
            note = await self._export_minutes_docx(
                state["minutes_doc"],
                __user__,
                __request__,
                __event_emitter__,
                registry=attached_files,
            )
            return "前回の議事録をテンプレート書式で書き出しました。\n\n" + note

        # 形式だけを言われた場合(「Wordで出して」)は、再処理せず前回結果を出し直す。
        if not operations and requested_formats and state.get("result"):
            return await self._export_only(
                result_text=state["result"],
                label=state.get("last_label") or "処理結果",
                output_formats=requested_formats,
                user_info=__user__,
                request=__request__,
                event_emitter=__event_emitter__,
                registry=attached_files,
            )

        # 前のメッセージの添付を巻き込まないよう、新しいものだけへ絞る
        new_files = self._pending_files(chat_id, __files__)

        operations, follow_up_instruction = self._resolve_follow_up(
            operations, message_text.strip(), state, bool(new_files)
        )
        if not operations:
            return HELP_MESSAGE

        operations = self._apply_kebatori_strength(operations, zone)
        max_chars = self._extract_max_chars(zone)

        (
            file_contents,
            agenda_text,
            roster_text,
            source_name,
            source_text,
        ) = await self._prepare_sources(message_text, state, new_files)
        self._remember_handled(chat_id, new_files)

        # 次第なしの文字起こしが複数ある議事録依頼は、1ファイルずつ
        # 個別に処理して、それぞれの結果ファイルを添付する。
        if (
            operations == ["minutes"]
            and not agenda_text
            and not state.get("agenda_text")
            and len(file_contents) > 1
        ):
            return await self._minutes_for_each_file(
                file_contents,
                output_formats,
                __user__,
                __request__,
                __event_emitter__,
                registry=attached_files,
            )

        if not source_text.strip():
            return (
                "処理対象の文章がありません。"
                "ファイルを添付するか、指示と一緒に本文を貼り付けてください。"
            )

        # 次第が添付されていれば、テンプレート書式の議事録を作る。
        if "minutes" in operations:
            return await self._handle_structured_minutes(
                source_name=source_name,
                source_text=source_text,
                agenda_text=agenda_text,
                roster_text=roster_text,
                follow_up_instruction=follow_up_instruction,
                state=state,
                chat_id=chat_id,
                user_info=__user__,
                request=__request__,
                event_emitter=__event_emitter__,
                attached_files=attached_files,
            )

        return await self._run_operation_chain(
            operations=operations,
            source_name=source_name,
            source_text=source_text,
            output_formats=output_formats,
            max_chars=max_chars,
            follow_up_instruction=follow_up_instruction,
            chat_id=chat_id,
            user_info=__user__,
            request=__request__,
            event_emitter=__event_emitter__,
            attached_files=attached_files,
        )
