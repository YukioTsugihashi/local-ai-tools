"""
Ollamaへ直接プロンプトを投げるクライアント。

2026-08-14 に Dify を外した際、dify_client.py を置き換えたもの。
Difyは分岐1つとプロンプト数本のために14コンテナを要していたうえ、
プロンプトがGUI側にあってKiloから編集できなかった。
"""

from __future__ import annotations

import os

import httpx

from app.config import llm_timeout_seconds, ollama_base_url


class WorkflowError(Exception):
    """文章処理の実行に関する安全なエラー。"""


class WorkflowConfigurationError(WorkflowError):
    pass


class WorkflowTimeoutError(WorkflowError):
    pass


class WorkflowResponseError(WorkflowError):
    pass


class OllamaChatClient:
    """`/api/chat` を1往復するだけの薄いクライアント。"""

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = (base_url or ollama_base_url()).rstrip("/")

    async def complete(
        self, prompt: str, *, model: str, num_predict: int, temperature: float
    ) -> str:
        if not self._base_url:
            raise WorkflowConfigurationError("OLLAMA_BASE_URLを設定してください。")
        body = {
            "model": model,
            "stream": False,
            "think": False,
            "options": {"num_predict": num_predict, "temperature": temperature},
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            async with httpx.AsyncClient(timeout=llm_timeout_seconds()) as client:
                response = await client.post(f"{self._base_url}/api/chat", json=body)
        except httpx.TimeoutException as error:
            raise WorkflowTimeoutError("ローカルLLMがタイムアウトしました。") from error
        except httpx.HTTPError as error:
            raise WorkflowResponseError("ローカルLLMへ接続できませんでした。") from error

        if response.status_code >= 400:
            raise WorkflowResponseError(
                f"ローカルLLMがHTTP {response.status_code}を返しました。"
            )
        try:
            payload = response.json()
            text = payload["message"]["content"]
        except (KeyError, TypeError, ValueError) as error:
            raise WorkflowResponseError("ローカルLLMの応答形式が不正です。") from error
        if not isinstance(text, str) or not text.strip():
            raise WorkflowResponseError("ローカルLLMが空の応答を返しました。")
        return text.strip()


def configured_model() -> str:
    """使用する生成モデル。環境変数で差し替えられる。"""
    from app.prompts import DEFAULT_MODEL

    return os.getenv("BRIDGE_LLM_MODEL", "").strip() or DEFAULT_MODEL
