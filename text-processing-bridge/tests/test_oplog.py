from __future__ import annotations

import json

from app.local_workflow import WorkflowRunResult
from app.main import app, get_workflow_client


class FakeWorkflowClient:
    def __init__(self, results: list[WorkflowRunResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def run(
        self, workflow_name: str, inputs: dict[str, object]
    ) -> WorkflowRunResult:
        self.calls.append((workflow_name, inputs))
        return self.results[len(self.calls) - 1]


# 架空データ。ログへ漏れてはいけない内容の代表として使う
SECRET_TEXT = "極秘の会議本文です。丁野部長が発言しました。"
SECRET_RESULT = "極秘の要約結果です。"


def test_operation_log_keeps_metadata_only(api_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BRIDGE_LOG_DIR", str(tmp_path))
    fake = FakeWorkflowClient(
        [WorkflowRunResult(text=SECRET_RESULT, workflow_run_id="run-log-1")]
    )
    app.dependency_overrides[get_workflow_client] = lambda: fake

    response = api_client.post(
        "/v1/text/summarize", json={"text": SECRET_TEXT, "max_chars": 100}
    )
    assert response.status_code == 200

    raw = (tmp_path / "operations.jsonl").read_text(encoding="utf-8")
    entry = json.loads(raw.splitlines()[-1])
    assert entry["path"] == "/v1/text/summarize"
    assert entry["method"] == "POST"
    assert entry["status"] == 200
    assert entry["workflow_run_id"] == "run-log-1"
    assert entry["source_chars"] == len(SECRET_TEXT)
    assert entry["request_id"]
    assert entry["duration_ms"] >= 0

    # 本文・結果・氏名は絶対に残さない(PIIレスの核)
    assert SECRET_TEXT not in raw
    assert SECRET_RESULT not in raw
    assert "丁野" not in raw


def test_error_responses_are_logged_without_detail_text(
    api_client, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("BRIDGE_LOG_DIR", str(tmp_path))

    response = api_client.post("/v1/text/custom_comment", json={"input_data": []})
    assert response.status_code == 501

    raw = (tmp_path / "operations.jsonl").read_text(encoding="utf-8")
    entry = json.loads(raw.splitlines()[-1])
    assert entry["path"] == "/v1/text/custom_comment"
    assert entry["status"] == 501
    # エラー詳細文はログへ書かない(状態コードだけで追える)
    assert "未実装" not in raw


def test_health_requests_are_not_logged(api_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BRIDGE_LOG_DIR", str(tmp_path))

    response = api_client.get("/health")
    assert response.status_code == 200

    # /v1/ 以外は記録しない(doctor等の定期監視でログが埋まらないように)
    assert not (tmp_path / "operations.jsonl").exists()
