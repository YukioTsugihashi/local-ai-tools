from __future__ import annotations

import json

import pytest

from app.local_workflow import (
    WorkflowConfigurationError,
    WorkflowResponseError,
    WorkflowRunResult,
    WorkflowTimeoutError,
)
from app.main import app, get_workflow_client


class FakeWorkflowClient:
    def __init__(
        self,
        results: list[WorkflowRunResult] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.results = results or []
        self.error = error
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def run(
        self, workflow_name: str, inputs: dict[str, object]
    ) -> WorkflowRunResult:
        self.calls.append((workflow_name, inputs))
        if self.error:
            raise self.error
        return self.results[len(self.calls) - 1]


def test_health_succeeds(api_client) -> None:
    response = api_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_custom_comment_is_disabled(api_client) -> None:
    # ブリッジ版が正式実装されるまで501で止まることを保証する
    response = api_client.post("/v1/text/custom_comment", json={"input_data": []})

    assert response.status_code == 501
    assert "未実装" in response.json()["detail"]


def test_version_succeeds(api_client) -> None:
    response = api_client.get("/version")

    assert response.status_code == 200
    assert response.json() == {"version": "1.0.0"}


def test_text_tools_page_contains_fixed_controls(api_client) -> None:
    response = api_client.get("/text-tools")

    assert response.status_code == 200
    assert "ケバ取り" in response.text
    assert "選択した処理を実行" in response.text
    assert 'type="file"' in response.text
    assert "/v1/text/refine" not in response.text


def test_summarize_sends_expected_workflow_inputs(api_client) -> None:
    fake = FakeWorkflowClient([WorkflowRunResult(text="短い要約", workflow_run_id="run-1")])
    app.dependency_overrides[get_workflow_client] = lambda: fake

    response = api_client.post(
        "/v1/text/summarize",
        json={"text": "一行目\r\n二行目", "max_chars": 20},
    )

    assert response.status_code == 200
    workflow_name, inputs = fake.calls[0]
    assert workflow_name == "text_processing"
    assert inputs == {
        "operation": "summarize",
        "text": "一行目\n二行目",
        "max_chars": 20,
        "kebatori_rules_json": "{}",
        "retry_instruction": "",
    }
    assert response.json()["source_chars"] == len("一行目\n二行目")


@pytest.mark.parametrize("operation", ["summarize", "minutes"])
def test_character_limit_can_be_omitted(api_client, operation: str) -> None:
    fake = FakeWorkflowClient(
        [WorkflowRunResult(text="文字数制限なしの結果", workflow_run_id=f"run-{operation}")]
    )
    app.dependency_overrides[get_workflow_client] = lambda: fake

    response = api_client.post(f"/v1/text/{operation}", json={"text": "原文"})

    assert response.status_code == 200
    assert len(fake.calls) == 1
    assert fake.calls[0][1]["max_chars"] == 0
    assert response.json()["within_limit"] is True


def test_over_limit_retries_only_twice(api_client) -> None:
    fake = FakeWorkflowClient(
        [
            WorkflowRunResult(text="ABCDEF", workflow_run_id="run-1"),
            WorkflowRunResult(text="ABCDE", workflow_run_id="run-2"),
            WorkflowRunResult(text="ABCD", workflow_run_id="run-3"),
        ]
    )
    app.dependency_overrides[get_workflow_client] = lambda: fake

    response = api_client.post(
        "/v1/text/summarize",
        json={"text": "abcdef", "max_chars": 3},
    )

    assert response.status_code == 200
    assert len(fake.calls) == 3
    assert fake.calls[0][1]["retry_instruction"] == ""
    assert "6文字" in str(fake.calls[1][1]["retry_instruction"])
    assert "5文字" in str(fake.calls[2][1]["retry_instruction"])
    assert response.json()["within_limit"] is False
    assert response.json()["workflow_run_id"] == "run-3"


def test_character_count_uses_lf_normalization(api_client) -> None:
    fake = FakeWorkflowClient([WorkflowRunResult(text="x\r\ny", workflow_run_id="run-lf")])
    app.dependency_overrides[get_workflow_client] = lambda: fake

    response = api_client.post(
        "/v1/text/rewrite",
        json={"text": "a\r\nb\rc"},
    )

    assert response.status_code == 200
    assert response.json()["source_chars"] == 5
    assert response.json()["result_chars"] == 3
    assert response.json()["text"] == "x\ny"


def test_leading_sentence_punctuation_is_removed_from_api_output(api_client) -> None:
    fake = FakeWorkflowClient(
        [
            WorkflowRunResult(
                text="、本日の会議では公開日を決定しました。\n。担当者は丁野さんです。",
                workflow_run_id="run-leading-punctuation",
            )
        ]
    )
    app.dependency_overrides[get_workflow_client] = lambda: fake

    response = api_client.post("/v1/text/rewrite", json={"text": "原文"})

    assert response.status_code == 200
    assert response.json()["text"] == (
        "本日の会議では公開日を決定しました。\n担当者は丁野さんです。"
    )


@pytest.mark.parametrize("operation", ["summarize", "rewrite"])
def test_summary_and_rewrite_remove_adjacent_duplicate_sentences(
    api_client, operation: str
) -> None:
    fake = FakeWorkflowClient(
        [
            WorkflowRunResult(
                text="本日の会議を開始します。確認します。確認します！！",
                workflow_run_id=f"run-{operation}",
            )
        ]
    )
    app.dependency_overrides[get_workflow_client] = lambda: fake
    payload: dict[str, object] = {
        "text": "えー、本日の会議を開始します。確認します。確認します！！"
    }
    if operation == "summarize":
        payload["max_chars"] = 100

    response = api_client.post(f"/v1/text/{operation}", json=payload)

    assert response.status_code == 200
    assert response.json()["text"] == "本日の会議を開始します。確認します。"
    assert response.json()["result_chars"] == len(
        "本日の会議を開始します。確認します。"
    )


def test_long_rewrite_is_chunked_and_joined(api_client, monkeypatch) -> None:
    monkeypatch.setenv("LONG_TEXT_THRESHOLD_CHARS", "100")
    monkeypatch.setenv("LONG_TEXT_CHUNK_CHARS", "60")
    fake = FakeWorkflowClient(
        [
            WorkflowRunResult(text=f"整形{index}", workflow_run_id=f"run-{index}")
            for index in range(1, 4)
        ]
    )
    app.dependency_overrides[get_workflow_client] = lambda: fake

    response = api_client.post(
        "/v1/text/rewrite", json={"text": "文です。" * 40}
    )

    assert response.status_code == 200
    assert len(fake.calls) == 3
    assert all(call[1]["max_chars"] == 0 for call in fake.calls)
    assert "".join(str(call[1]["text"]) for call in fake.calls) == "文です。" * 40
    assert response.json()["text"] == "整形1\n整形2\n整形3"
    assert any("分割" in warning for warning in response.json()["warnings"])


def test_long_summarize_runs_map_then_merge(api_client, monkeypatch) -> None:
    monkeypatch.setenv("LONG_TEXT_THRESHOLD_CHARS", "100")
    monkeypatch.setenv("LONG_TEXT_CHUNK_CHARS", "60")
    fake = FakeWorkflowClient(
        [
            WorkflowRunResult(text="部分A", workflow_run_id="run-1"),
            WorkflowRunResult(text="部分B", workflow_run_id="run-2"),
            WorkflowRunResult(text="最終要約", workflow_run_id="run-final"),
        ]
    )
    app.dependency_overrides[get_workflow_client] = lambda: fake

    response = api_client.post(
        "/v1/text/summarize",
        json={"text": "文です。" * 30, "max_chars": 50},
    )

    assert response.status_code == 200
    assert len(fake.calls) == 3
    merge_call = fake.calls[2][1]
    assert merge_call["text"] == "部分A\n\n部分B"
    assert "統合" in str(merge_call["retry_instruction"])
    assert merge_call["max_chars"] == 50
    assert response.json()["text"] == "最終要約"
    assert response.json()["workflow_run_id"] == "run-final"


def test_long_summarize_merges_hierarchically(api_client, monkeypatch) -> None:
    monkeypatch.setenv("LONG_TEXT_THRESHOLD_CHARS", "100")
    monkeypatch.setenv("LONG_TEXT_CHUNK_CHARS", "60")

    class GrowingFake:
        """最初の分割結果が長く、まだ統合パスへ入り切らないケースを再現する。"""

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def run(
            self, workflow_name: str, inputs: dict[str, object]
        ) -> WorkflowRunResult:
            self.calls.append((workflow_name, inputs))
            if len(self.calls) <= 2:
                return WorkflowRunResult(
                    text="長" * 80, workflow_run_id=f"run-{len(self.calls)}"
                )
            return WorkflowRunResult(
                text="統合済み要約", workflow_run_id=f"run-{len(self.calls)}"
            )

    fake = GrowingFake()
    app.dependency_overrides[get_workflow_client] = lambda: fake

    response = api_client.post(
        "/v1/text/summarize", json={"text": "文です。" * 30}
    )

    assert response.status_code == 200
    # 1段目2ブロック + 2段目3ブロック + 最終統合1回
    assert len(fake.calls) == 6
    assert "統合" in str(fake.calls[-1][1]["retry_instruction"])
    assert response.json()["text"] == "統合済み要約"
    assert any("分割" in warning for warning in response.json()["warnings"])


def test_long_refine_applies_instruction_to_every_chunk(
    api_client, monkeypatch
) -> None:
    monkeypatch.setenv("LONG_TEXT_THRESHOLD_CHARS", "100")
    monkeypatch.setenv("LONG_TEXT_CHUNK_CHARS", "60")
    fake = FakeWorkflowClient(
        [
            WorkflowRunResult(text="修正1", workflow_run_id="run-1"),
            WorkflowRunResult(text="修正2", workflow_run_id="run-2"),
        ]
    )
    app.dependency_overrides[get_workflow_client] = lambda: fake

    response = api_client.post(
        "/v1/text/refine",
        json={"text": "文です。" * 30, "instruction": "敬体にしてください。"},
    )

    assert response.status_code == 200
    assert len(fake.calls) == 2
    assert all(
        call[1]["retry_instruction"] == "敬体にしてください。"
        for call in fake.calls
    )
    assert response.json()["text"] == "修正1\n修正2"


def test_short_text_is_not_chunked(api_client, monkeypatch) -> None:
    monkeypatch.setenv("LONG_TEXT_THRESHOLD_CHARS", "100")
    fake = FakeWorkflowClient(
        [WorkflowRunResult(text="整形結果", workflow_run_id="run-1")]
    )
    app.dependency_overrides[get_workflow_client] = lambda: fake

    response = api_client.post("/v1/text/rewrite", json={"text": "短い原文です。"})

    assert response.status_code == 200
    assert len(fake.calls) == 1
    assert response.json()["warnings"] == []


def test_kebatori_sends_selected_yaml_rules(api_client) -> None:
    fake = FakeWorkflowClient(
        [WorkflowRunResult(text="本題です。", workflow_run_id="run-kebatori")]
    )
    app.dependency_overrides[get_workflow_client] = lambda: fake

    response = api_client.post(
        "/v1/text/kebatori",
        json={"text": "えー 本題です。", "profile": "default"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["profile"] == "default"
    assert payload["rules"]["filler_phrases"]
    sent_rules = json.loads(str(fake.calls[0][1]["kebatori_rules_json"]))
    assert sent_rules["profile"] == "default"
    assert sent_rules["filler_phrases"] == payload["rules"]["filler_phrases"]


def test_rewrite_retries_when_numbers_missing(api_client) -> None:
    fake = FakeWorkflowClient(
        [
            WorkflowRunResult(text="売上は好調でした。", workflow_run_id="run-1"),
            WorkflowRunResult(
                text="売上は8億4,200万円でした。", workflow_run_id="run-2"
            ),
        ]
    )
    app.dependency_overrides[get_workflow_client] = lambda: fake

    response = api_client.post(
        "/v1/text/rewrite",
        json={"text": "えー、売上は8億4,200万円でした。"},
    )

    assert response.status_code == 200
    assert len(fake.calls) == 2
    assert "欠落" in str(fake.calls[1][1]["retry_instruction"])
    assert response.json()["warnings"] == []
    assert response.json()["text"] == "売上は8億4,200万円でした。"


def test_rewrite_warns_when_numbers_stay_missing(api_client) -> None:
    fake = FakeWorkflowClient(
        [
            WorkflowRunResult(text="売上は好調でした。", workflow_run_id="run-1"),
            WorkflowRunResult(text="売上は好調でした。", workflow_run_id="run-2"),
        ]
    )
    app.dependency_overrides[get_workflow_client] = lambda: fake

    response = api_client.post(
        "/v1/text/rewrite",
        json={"text": "売上は8億4,200万円でした。"},
    )

    assert response.status_code == 200
    assert len(fake.calls) == 2
    warnings = response.json()["warnings"]
    assert any("欠落" in warning for warning in warnings)


def test_minutes_rebuilds_when_numbers_are_invented(api_client) -> None:
    fake = FakeWorkflowClient(
        [
            WorkflowRunResult(
                text="概要:予算は500万円と決定。", workflow_run_id="run-1"
            ),
            WorkflowRunResult(
                text="概要:予算の増額を決定。", workflow_run_id="run-2"
            ),
        ]
    )
    app.dependency_overrides[get_workflow_client] = lambda: fake

    response = api_client.post(
        "/v1/text/minutes",
        json={"text": "会議で予算の増額を決定しました。"},
    )

    assert response.status_code == 200
    assert len(fake.calls) == 2
    assert "存在しない数値" in str(fake.calls[1][1]["retry_instruction"])
    assert response.json()["text"] == "概要:予算の増額を決定。"
    assert response.json()["warnings"] == []


def test_kebatori_plus_sends_rules_and_maps_operation(api_client) -> None:
    fake = FakeWorkflowClient(
        [WorkflowRunResult(text="本題です。", workflow_run_id="run-kebatori-plus")]
    )
    app.dependency_overrides[get_workflow_client] = lambda: fake

    response = api_client.post(
        "/v1/text/kebatori_plus",
        json={"text": "えー 本題です。", "profile": "default"},
    )

    assert response.status_code == 200
    assert response.json()["operation"] == "kebatori_plus"
    assert fake.calls[0][1]["operation"] == "kebatori_plus"
    sent_rules = json.loads(str(fake.calls[0][1]["kebatori_rules_json"]))
    assert sent_rules["filler_phrases"]


def test_long_kebatori_plus_chunks_carry_rules(api_client, monkeypatch) -> None:
    monkeypatch.setenv("LONG_TEXT_THRESHOLD_CHARS", "100")
    monkeypatch.setenv("LONG_TEXT_CHUNK_CHARS", "60")
    fake = FakeWorkflowClient(
        [
            WorkflowRunResult(text=f"部分{index}", workflow_run_id=f"run-{index}")
            for index in range(1, 4)
        ]
    )
    app.dependency_overrides[get_workflow_client] = lambda: fake

    response = api_client.post(
        "/v1/text/kebatori_plus",
        json={"text": "文です。" * 40, "profile": "default"},
    )

    assert response.status_code == 200
    assert len(fake.calls) == 3
    for call in fake.calls:
        sent_rules = json.loads(str(call[1]["kebatori_rules_json"]))
        assert sent_rules["filler_phrases"]
    assert response.json()["text"] == "部分1\n部分2\n部分3"


def test_refine_sends_instruction_to_the_model(api_client) -> None:
    fake = FakeWorkflowClient(
        [WorkflowRunResult(text="修正後です。", workflow_run_id="run-refine")]
    )
    app.dependency_overrides[get_workflow_client] = lambda: fake

    response = api_client.post(
        "/v1/text/refine",
        json={"text": "修正前です。", "instruction": "敬体にしてください。"},
    )

    assert response.status_code == 200
    assert fake.calls[0][1]["operation"] == "refine"
    assert fake.calls[0][1]["retry_instruction"] == "敬体にしてください。"
    assert response.json()["text"] == "修正後です。"


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (WorkflowConfigurationError("missing"), 503),
        (WorkflowTimeoutError("timeout"), 504),
        (WorkflowResponseError("upstream"), 502),
    ],
)
def test_workflow_errors_map_to_http_errors(
    api_client, error: Exception, expected_status: int
) -> None:
    fake = FakeWorkflowClient(error=error)
    app.dependency_overrides[get_workflow_client] = lambda: fake

    response = api_client.post("/v1/text/rewrite", json={"text": "原文"})

    assert response.status_code == expected_status


def test_openapi_contains_four_text_endpoints(api_client) -> None:
    schema = api_client.get("/openapi.json").json()
    expected = {
        "/v1/text/summarize": "summarize_text",
        "/v1/text/rewrite": "rewrite_text",
        "/v1/text/minutes": "create_minutes",
        "/v1/text/kebatori": "remove_kebatori",
    }

    for path, operation_id in expected.items():
        assert schema["paths"][path]["post"]["operationId"] == operation_id


def test_議事録の応答に次第と名簿の規模が載る(api_client) -> None:
    """
    「次第と名簿を渡したのに議事録へ乗らない」を切り分けるための記録。
    どこで落ちたかを推測で追う羽目になったため、届いたかどうかを
    運用ログに残せるようにした(2026-08-18)。氏名は残さない。
    """
    response = api_client.post(
        "/v1/text/minutes_structured",
        json={
            "text": "山田: 開会します。戊野: 入院実績を報告します。",
            "agenda_text": "会議次第\n1．開会\n2．議題\n ①　入院実績について",
            "roster_text": "出席者：山田太郎、戊野一郎",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["agenda_chars"] > 0
    assert body["roster_chars"] > 0
    assert body["roster_names"] == 2
