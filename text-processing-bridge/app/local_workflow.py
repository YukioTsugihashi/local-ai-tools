"""
文章処理の操作分岐。旧Difyワークフロー(text_processing)と同じ役割を持つ。

呼び出し側(processing.py・structured.py)の契約を変えないため、
`run(workflow_name, inputs)` の形と戻り値を旧クライアントに合わせてある。
分岐は旧ワークフローの operation-router と一対一で対応する。

  summarize / rewrite / minutes / refine → LLM 1回
  kebatori                               → LLMを使わずルールだけで処理
  kebatori_plus                          → LLMで候補検出 → コードで削除
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from app.kebatori import (
    apply_kebatori,
    apply_kebatori_plus,
    profile_from_rules,
    select_filler_candidates,
)
from app.ollama_client import (
    OllamaChatClient,
    WorkflowConfigurationError,
    WorkflowError,
    WorkflowResponseError,
    WorkflowTimeoutError,
    configured_model,
)
from app.prompts import KEBATORI_DETECT, LLM_PROMPTS

# 呼び出し側はこのモジュールだけを見れば済むよう、例外もここから公開する
__all__ = [
    "LocalWorkflowClient",
    "WorkflowConfigurationError",
    "WorkflowError",
    "WorkflowResponseError",
    "WorkflowRunResult",
    "WorkflowTimeoutError",
]


@dataclass(frozen=True)
class WorkflowRunResult:
    text: str
    workflow_run_id: str


def _rules_from(inputs: dict[str, Any]) -> dict[str, Any]:
    raw = inputs.get("kebatori_rules_json") or "{}"
    try:
        rules = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return rules if isinstance(rules, dict) else {}


class LocalWorkflowClient:
    """旧DifyWorkflowClientの置き換え。同じ呼び出し方で動く。"""

    def __init__(self, chat_client: OllamaChatClient | None = None) -> None:
        self._chat = chat_client or OllamaChatClient()

    async def run(self, workflow_name: str, inputs: dict[str, Any]) -> WorkflowRunResult:
        if workflow_name != "text_processing":
            raise WorkflowResponseError(f"未登録の処理です: {workflow_name}")

        operation = str(inputs.get("operation") or "")
        text = str(inputs.get("text") or "")
        run_id = uuid.uuid4().hex

        if operation in LLM_PROMPTS:
            spec = LLM_PROMPTS[operation]
            prompt = spec.template.format(
                text=text,
                max_chars=inputs.get("max_chars") or 0,
                retry_instruction=inputs.get("retry_instruction") or "",
            )
            output = await self._chat.complete(
                prompt,
                model=configured_model(),
                num_predict=spec.num_predict,
                temperature=spec.temperature,
            )
            return WorkflowRunResult(text=output, workflow_run_id=run_id)

        profile = profile_from_rules(_rules_from(inputs))

        if operation == "kebatori_plus":
            detected = await self._chat.complete(
                KEBATORI_DETECT.template.format(text=text),
                model=configured_model(),
                num_predict=KEBATORI_DETECT.num_predict,
                temperature=KEBATORI_DETECT.temperature,
            )
            candidates = select_filler_candidates(detected, text)
            return WorkflowRunResult(
                text=apply_kebatori_plus(text, profile, candidates),
                workflow_run_id=run_id,
            )

        # 旧ワークフローでも kebatori は分岐の既定値であり、LLMを使わない
        return WorkflowRunResult(
            text=apply_kebatori(text, profile), workflow_run_id=run_id
        )
