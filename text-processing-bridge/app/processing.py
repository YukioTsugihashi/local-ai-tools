"""
汎用文章処理(要約・整形・議事録・ケバ取り・修正)の実行制御。

長文の分割・統合、文字数上限の再試行、数値の欠落・捏造ガードなど、
「LLMの出力を信用せず、コードで検証して必要なら再実行する」層。
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import HTTPException

from app.chunking import split_text
from app.config import long_text_chunk_chars, long_text_threshold_chars
from app.kebatori import (
    normalize_line_endings,
    remove_adjacent_duplicate_sentences,
    remove_leading_sentence_punctuation,
)
from app.local_workflow import LocalWorkflowClient, WorkflowRunResult
from app.ollama_client import (
    WorkflowConfigurationError,
    WorkflowResponseError,
    WorkflowTimeoutError,
)
from app.models import TextProcessingResponse

# 数値トークン(単位1文字まで含む)。LLM出力の欠落・捏造検知に使う。
NUMBER_TOKEN_PATTERN = re.compile(
    r"[0-9０-９][0-9０-９,，\.]*[億万％%円件名日月時分社台年字]?"
)
_NUMBER_NORMALIZE_TABLE = str.maketrans("０１２３４５６７８９，", "0123456789,")


def _normalize_numbers(text: str) -> str:
    return text.translate(_NUMBER_NORMALIZE_TABLE).replace(",", "")


def _crlf_chars(text: str) -> int:
    """
    改行をCRLF(2文字)として数えた文字数を返す。

    Pythonのlen()は改行を1文字として数えるが、Windowsのエディタや文書ソフトは
    2文字として数えることがある。文字数上限はこの多い側で判定し、
    どちらで開いても指定文字数を超えないようにする。
    """
    return len(text) + text.count("\n")


def _number_tokens(text: str) -> set[str]:
    return {match.group(0) for match in NUMBER_TOKEN_PATTERN.finditer(text)}


def _missing_number_tokens(source: str, output: str) -> list[str]:
    """原文にあるのに出力へ残っていない数値トークンを返す。"""
    normalized_output = _normalize_numbers(output)
    return sorted(
        token
        for token in _number_tokens(source)
        if _normalize_numbers(token) not in normalized_output
    )


NUMBER_CORE_PATTERN = re.compile(r"[0-9０-９][0-9０-９,，\.]*")

# 日付・時刻のパターン。数値ガードの照合前に原文から取り除く。
DATETIME_STRIP_PATTERN = re.compile(
    r"[0-9０-９]{4}\s*[/年.\-]\s*[0-9０-９]{1,2}\s*[/月.\-]\s*[0-9０-９]{1,2}\s*日?"
    r"|[0-9０-９]{1,2}\s*[:：]\s*[0-9０-９]{2}(?:[:：][0-9０-９]{2})?"
    r"|[0-9０-９]{1,2}時(?:[0-9０-９]{1,2}分)?"
)


def _strip_datetime(text: str) -> str:
    """日付・時刻の表記を落とす。開催日時の断片を欠落数値と誤検知しないため。"""
    return DATETIME_STRIP_PATTERN.sub("", text)


def _invented_number_tokens(source: str, output: str) -> list[str]:
    """
    出力にあるのに原文に存在しない数値(捏造の疑い)を返す。

    「11パーセント」→「11%」のような単位の表記揺れを捏造と誤検知しないよう、
    照合は数字部分だけで行う。
    """
    normalized_source = _normalize_numbers(source)
    invented = set()
    for match in NUMBER_CORE_PATTERN.finditer(output):
        core = _normalize_numbers(match.group(0))
        if core and core not in normalized_source:
            invented.add(match.group(0))
    return sorted(invented)


# 分割処理の対象。ケバ取り(ルールのみ)は分割せず全文を一括で扱う。
CHUNKED_OPERATIONS = {"summarize", "rewrite", "minutes", "refine", "kebatori_plus"}
# 分割時に部分結果をそのまま連結する処理(統合パス不要)。
CONCAT_OPERATIONS = {"rewrite", "refine", "kebatori_plus"}
MERGE_INSTRUCTIONS = {
    "summarize": (
        "入力は長文を分割して作成した部分要約の連結です。"
        "全ブロックの情報を保ったまま、一つの要約へ統合してください。"
    ),
    "minutes": (
        "入力は長文を分割して作成した部分議事録の連結です。"
        "同じ項目の重複は一度だけ記載し、決定事項・担当者・数値・期限は"
        "省略せずに一つの議事録へ統合してください。"
    ),
}


async def _run_rewrite_with_number_guard(
    client: LocalWorkflowClient,
    text_block: str,
    rules_json: str,
    base_instruction: str = "",
) -> tuple[WorkflowRunResult, list[str]]:
    """
    整形は情報を落とさない処理のため、原文の数値が欠落したら
    欠落リスト付きで1回だけ再実行し、直らなければ警告を返す。
    """
    warnings: list[str] = []
    instruction = base_instruction
    result: WorkflowRunResult | None = None
    missing: list[str] = []
    for _ in range(2):
        result = await _run_workflow(
            client,
            {
                "operation": "rewrite",
                "text": text_block,
                "max_chars": 0,
                "kebatori_rules_json": rules_json,
                "retry_instruction": instruction,
            },
        )
        missing = _missing_number_tokens(text_block, result.text)
        if not missing:
            return result, warnings
        instruction = base_instruction + (
            "前回の整形結果では次の数値が欠落していました: "
            + "、".join(missing[:10])
            + "。原文の情報を一切省略せず、すべての数値を残したまま整形し直してください。"
        )
    warnings.append(
        "整形結果で数値が欠落した可能性があります: " + "、".join(missing[:10])
    )
    assert result is not None
    return result, warnings


async def _process(
    *,
    operation: str,
    text: str,
    client: LocalWorkflowClient,
    max_chars: int | None = None,
    profile_name: str | None = None,
    rules: dict[str, Any] | None = None,
    initial_retry_instruction: str = "",
) -> TextProcessingResponse:
    source = normalize_line_endings(text)
    warnings: list[str] = []
    work_source = source
    retry_instruction = initial_retry_instruction
    merge_instruction = ""
    result: WorkflowRunResult | None = None

    if (
        operation in CHUNKED_OPERATIONS
        and len(source) > long_text_threshold_chars()
    ):
        if operation in CONCAT_OPERATIONS:
            chunks = split_text(source, long_text_chunk_chars())
            rules_json = json.dumps(
                rules or {}, ensure_ascii=False, separators=(",", ":")
            )
            partials: list[str] = []
            last_run_id = ""
            for chunk in chunks:
                if operation == "rewrite":
                    chunk_result, guard_warnings = (
                        await _run_rewrite_with_number_guard(
                            client, chunk, rules_json, initial_retry_instruction
                        )
                    )
                    warnings.extend(guard_warnings)
                else:
                    chunk_result = await _run_workflow(
                        client,
                        {
                            "operation": operation,
                            "text": chunk,
                            "max_chars": 0,
                            "kebatori_rules_json": rules_json,
                            "retry_instruction": initial_retry_instruction,
                        },
                    )
                partials.append(
                    normalize_line_endings(chunk_result.text).strip("\n")
                )
                last_run_id = chunk_result.workflow_run_id
            warnings.append(
                f"長文のため{len(chunks)}ブロックに分割して処理しました。"
            )
            # 文境界で分割済みのため、整形結果はそのまま連結できる。
            result = WorkflowRunResult(
                text="\n".join(partials), workflow_run_id=last_run_id
            )
        else:
            # 要約・議事録は部分結果を作り、統合パスで一本化する。
            # 部分結果の連結がまだ長い場合は、収まるまで段階的に統合する。
            merge_instruction = MERGE_INSTRUCTIONS[operation]
            # 統合パス自体は出力が入力より短いため、少し長めの入力を許容できる。
            merge_input_limit = long_text_threshold_chars() * 3 // 2
            current = source
            pass_instruction = ""
            total_blocks = 0
            rounds = 0
            while rounds < 4:
                chunks = split_text(current, long_text_chunk_chars())
                partials = []
                for chunk in chunks:
                    chunk_result = await _run_workflow(
                        client,
                        {
                            "operation": operation,
                            "text": chunk,
                            "max_chars": 0,
                            "kebatori_rules_json": "{}",
                            "retry_instruction": (
                                pass_instruction or initial_retry_instruction
                            ),
                        },
                    )
                    partials.append(
                        normalize_line_endings(chunk_result.text).strip("\n")
                    )
                current = "\n\n".join(partials)
                pass_instruction = merge_instruction + initial_retry_instruction
                total_blocks += len(chunks)
                rounds += 1
                if len(current) <= merge_input_limit:
                    break
            warnings.append(
                f"長文のため{total_blocks}ブロックに分割して処理しました。"
            )
            work_source = current
            retry_instruction = merge_instruction + initial_retry_instruction

    if result is None and operation == "rewrite":
        result, guard_warnings = await _run_rewrite_with_number_guard(
            client,
            work_source,
            json.dumps(rules or {}, ensure_ascii=False, separators=(",", ":")),
            initial_retry_instruction,
        )
        warnings.extend(guard_warnings)

    if result is None:
        for attempt in range(3):
            inputs = {
                "operation": operation,
                "text": work_source,
                "max_chars": max_chars or 0,
                "kebatori_rules_json": json.dumps(
                    rules or {}, ensure_ascii=False, separators=(",", ":")
                ),
                "retry_instruction": retry_instruction,
            }
            result = await _run_workflow(client, inputs)
            normalized_result = remove_leading_sentence_punctuation(
                normalize_line_endings(result.text)
            )

            # 上限判定は改行をCRLF換算した多い側で行う。
            attempt_chars = _crlf_chars(normalized_result)
            if (
                operation not in {"summarize", "minutes"}
                or max_chars is None
                or attempt_chars <= max_chars
                or attempt == 2
            ):
                break
            retry_instruction = merge_instruction + (
                f"前回の出力は{attempt_chars}文字でした。"
                f"内容上の制約を守ったまま、必ず{max_chars}文字以内へ短縮してください。"
            )

    if result is None:
        raise HTTPException(status_code=502, detail="ローカルLLMから結果を取得できませんでした。")

    output = normalize_line_endings(result.text)
    if operation in {"summarize", "rewrite"}:
        output = remove_adjacent_duplicate_sentences(output)
    output = remove_leading_sentence_punctuation(output)
    if operation in {"kebatori", "kebatori_plus"} and len(output) > len(source):
        output = source
        warnings.append("毛羽取り結果が原文より長いため、原文を返しました。")

    if operation in {"summarize", "minutes"}:
        # 原文に存在しない数値(捏造の疑い)を検知したら1回だけ作り直す。
        invented = _invented_number_tokens(source, output)
        if invented:
            corrective = (
                merge_instruction
                + "前回の出力には原文に存在しない数値が含まれていました: "
                + "、".join(invented[:10])
                + "。原文にある情報だけを使って作り直してください。"
            )
            if max_chars:
                corrective += f"文字数上限は{max_chars}文字です。"
            retry_result = await _run_workflow(
                client,
                {
                    "operation": operation,
                    "text": work_source,
                    "max_chars": max_chars or 0,
                    "kebatori_rules_json": json.dumps(
                        rules or {}, ensure_ascii=False, separators=(",", ":")
                    ),
                    "retry_instruction": corrective,
                },
            )
            retry_output = remove_leading_sentence_punctuation(
                normalize_line_endings(retry_result.text)
            )
            if operation == "summarize":
                retry_output = remove_adjacent_duplicate_sentences(retry_output)
            still_invented = _invented_number_tokens(source, retry_output)
            if len(still_invented) < len(invented):
                output = retry_output
                result = retry_result
                invented = still_invented
            if invented:
                warnings.append(
                    "原文にない数値が含まれている可能性があります: "
                    + "、".join(invented[:10])
                )
        if operation == "minutes":
            missing = _missing_number_tokens(_strip_datetime(source), output)
            if missing:
                warnings.append(
                    f"原文の数値のうち{len(missing)}件が議事録に含まれていません"
                    "(例: " + "、".join(missing[:5]) + ")"
                )

    output_chars_crlf = _crlf_chars(output)
    within_limit = max_chars is None or output_chars_crlf <= max_chars
    if not within_limit:
        warnings.append(
            f"2回再実行しましたが、結果が{max_chars}文字を超えています"
            f"(改行CRLF換算で{output_chars_crlf}文字)。"
        )

    response_rules = dict(rules or {})
    response_rules.pop("profile", None)
    return TextProcessingResponse(
        operation=operation,
        text=output,
        source_chars=len(source),
        result_chars=len(output),
        result_chars_crlf=output_chars_crlf,
        within_limit=within_limit,
        workflow_run_id=result.workflow_run_id,
        profile=profile_name,
        rules=response_rules or None,
        warnings=warnings,
    )


async def _run_workflow(
    client: LocalWorkflowClient, inputs: dict[str, Any]
) -> WorkflowRunResult:
    try:
        return await client.run("text_processing", inputs)
    except WorkflowConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except WorkflowTimeoutError as error:
        raise HTTPException(status_code=504, detail=str(error)) from error
    except WorkflowResponseError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
