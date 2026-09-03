from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


# 追加指示。会話の続きで「もっと詳細に」などと言われたとき、
# 元の本文に対して同じ処理を指示付きで再実行するために使う。
_INSTRUCTION = Field(default="", max_length=10_000)


class SummarizeRequest(StrictRequest):
    text: str = Field(min_length=1, max_length=200_000)
    max_chars: int | None = Field(default=None, ge=1, le=100_000)
    instruction: str = _INSTRUCTION


class RewriteRequest(StrictRequest):
    text: str = Field(min_length=1, max_length=200_000)
    instruction: str = _INSTRUCTION


class MinutesRequest(StrictRequest):
    text: str = Field(min_length=1, max_length=200_000)
    max_chars: int | None = Field(default=None, ge=1, le=100_000)
    instruction: str = _INSTRUCTION


class KebatoriRequest(StrictRequest):
    text: str = Field(min_length=1, max_length=200_000)
    profile: str = Field(default="default", min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    instruction: str = _INSTRUCTION


class RefineRequest(StrictRequest):
    text: str = Field(min_length=1, max_length=200_000)
    instruction: str = Field(min_length=1, max_length=10_000)


class StructuredMinutesRequest(StrictRequest):
    """次第の骨格に沿って発言を振り分ける議事録。"""

    text: str = Field(min_length=1, max_length=400_000)
    # 次第の抽出テキスト。空なら議題構成を文字起こしから推定する。
    agenda_text: str = Field(default="", max_length=100_000)
    # 出席者名簿。発言者名の表記を揃えるために使う(任意)。
    roster_text: str = Field(default="", max_length=100_000)
    instruction: str = _INSTRUCTION


class StructuredMinutesResponse(BaseModel):
    document: dict[str, Any]
    source_chars: int
    blocks: int
    assigned_remarks: int
    unmatched_keys: list[str] = Field(default_factory=list)
    # 議題構成を推定で作ったかどうか(次第なしの場合True)。
    agenda_inferred: bool = False
    # 次第・名簿が実際に届いたかを運用ログへ残すための規模(氏名は残さない)。
    # 「次第と名簿を渡したのに議事録へ乗らない」を、推測ではなくログで切り分ける
    # ためにある(2026-08-18に切り分けできず追加)。
    agenda_chars: int = 0
    roster_chars: int = 0
    roster_names: int = 0
    warnings: list[str] = Field(default_factory=list)
    workflow_run_id: str = ""


class ExportRequest(StrictRequest):
    """処理結果を配布用ファイル(txt/md/csv/docx/xlsx)へ変換する依頼。"""

    content: str = Field(default="", max_length=2_000_000)
    format: str = Field(default="txt", pattern=r"^(txt|md|csv|docx|xlsx)$")
    # docxの見出し・xlsxのシート名に使う。ファイル名はPipe側で決める。
    title: str = Field(default="", max_length=200)


class ExportResponse(BaseModel):
    mime: str
    data_b64: str


class MinutesDocxRequest(StrictRequest):
    """構造化議事録データをテンプレート書式のdocxへ流し込む依頼。

    テンプレートはOpen WebUI側のデータ領域に置かれ差し替え可能なため、
    ブリッジには持たせず毎回base64で受け取る。
    """

    document: dict[str, Any]
    template_b64: str = Field(min_length=1, max_length=30_000_000)


class ResultPageRequest(StrictRequest):
    """Toolの結果埋め込みページ(HTML)の生成依頼。"""

    operation_label: str = Field(default="", max_length=100)
    result: str = Field(default="", max_length=2_000_000)
    source_chars: int = 0
    result_chars: int = 0
    result_chars_crlf: int | None = None
    source_type: str = Field(default="", max_length=300)
    workflow_run_id: str = Field(default="", max_length=200)
    warnings: list[str] = Field(default_factory=list)
    attached_filename: str = Field(default="", max_length=300)


class ResultPageResponse(BaseModel):
    html: str


class TextProcessingResponse(BaseModel):
    operation: str
    text: str
    source_chars: int
    result_chars: int
    # 改行をCRLF(2文字)として数えた場合の文字数。エディタや文書ソフトの
    # 表示とPythonのlen()がずれるため、上限判定は必ず多いこちらで行う。
    result_chars_crlf: int = 0
    within_limit: bool
    workflow_run_id: str
    profile: str | None = None
    rules: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
