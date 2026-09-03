"""Ollama直叩きクライアントのテスト。障害を安全なエラーへ変換できること。"""

from __future__ import annotations

import httpx
import pytest

from app.ollama_client import (
    OllamaChatClient,
    WorkflowResponseError,
    WorkflowTimeoutError,
    configured_model,
)


class FakeResponse:
    def __init__(self, status_code: int, payload) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _install(monkeypatch, behaviour) -> dict:
    """httpx.AsyncClient.post を差し替え、送った内容を記録する。"""
    sent: dict = {}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            sent["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json):
            sent["url"] = url
            sent["body"] = json
            if isinstance(behaviour, Exception):
                raise behaviour
            return behaviour

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    return sent


@pytest.mark.anyio
async def test_successful_call_sends_expected_request(monkeypatch) -> None:
    sent = _install(monkeypatch, FakeResponse(200, {"message": {"content": " 結果 "}}))
    client = OllamaChatClient("http://ollama:11434/")

    text = await client.complete("プロンプト", model="gemma4:26b", num_predict=100, temperature=0.2)

    assert text == "結果"
    assert sent["url"] == "http://ollama:11434/api/chat"
    assert sent["body"]["model"] == "gemma4:26b"
    assert sent["body"]["stream"] is False
    assert sent["body"]["options"] == {"num_predict": 100, "temperature": 0.2}
    assert sent["body"]["messages"] == [{"role": "user", "content": "プロンプト"}]


@pytest.mark.anyio
async def test_timeout_is_reported_as_timeout(monkeypatch) -> None:
    _install(monkeypatch, httpx.ReadTimeout("timed out"))
    client = OllamaChatClient("http://ollama:11434")

    with pytest.raises(WorkflowTimeoutError):
        await client.complete("x", model="m", num_predict=10, temperature=0)


@pytest.mark.anyio
async def test_connection_failure_is_reported_safely(monkeypatch) -> None:
    _install(monkeypatch, httpx.ConnectError("refused"))
    client = OllamaChatClient("http://ollama:11434")

    with pytest.raises(WorkflowResponseError, match="接続できません"):
        await client.complete("x", model="m", num_predict=10, temperature=0)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(500, {}),
        FakeResponse(200, {"message": {}}),
        FakeResponse(200, {"message": {"content": "   "}}),
        FakeResponse(200, ValueError("not json")),
    ],
)
async def test_bad_responses_are_reported_safely(monkeypatch, response) -> None:
    _install(monkeypatch, response)
    client = OllamaChatClient("http://ollama:11434")

    with pytest.raises(WorkflowResponseError):
        await client.complete("x", model="m", num_predict=10, temperature=0)


def test_model_can_be_overridden_by_env(monkeypatch) -> None:
    monkeypatch.delenv("BRIDGE_LLM_MODEL", raising=False)
    assert configured_model() == "gemma4:26b"

    monkeypatch.setenv("BRIDGE_LLM_MODEL", "別モデル:latest")
    assert configured_model() == "別モデル:latest"
