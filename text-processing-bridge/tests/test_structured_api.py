from __future__ import annotations

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


# 架空の次第と文字起こし。実在の会議データはテストに使わない(data/は触らない)。
SAMPLE_AGENDA = """
令和8年度 第1回 経営企画会議 次第
日 時：令和8年6月9日（火）17:00～
場 所：大会議室
議 題
1 病院の経営状況について
① 月次収支の報告 ・・・・ 資料1
経営企画課 山田 課長補佐
次回開催予定：令和8年7月14日（月）
"""

# 話者を明記した文字起こし。スタブのLLM回答が話者を返すのは、
# ここに明記がある場合だけ(instructions.pyの「推測しない」契約)。
TRANSCRIPT = (
    "それでは山田課長補佐から資料1の説明をお願いします。\n"
    "乙野先生：月次収支の詳細はいつ共有されるのでしょうか。\n"
    "山田課長補佐：来週の経営会議までに共有します。"
)


def test_structured_minutes_assigns_remarks_to_agenda_items(api_client) -> None:
    fake = FakeWorkflowClient(
        [
            # 1周目: 発言の振り分け
            WorkflowRunResult(
                text=(
                    "##項目 1-1\n"
                    "##報告 山田課長補佐\n"
                    "##資料 資料1\n"
                    "##発言 乙野先生 | 月次収支の詳細はいつ共有されるのか。\n"
                    "##発言 山田課長補佐 | 来週の経営会議までに共有する。\n"
                ),
                workflow_run_id="run-1",
            ),
            # 2周目(強制チェック①): 追加なし
            WorkflowRunResult(text="", workflow_run_id="run-2"),
        ]
    )
    app.dependency_overrides[get_workflow_client] = lambda: fake

    response = api_client.post(
        "/v1/text/minutes_structured",
        json={
            "text": TRANSCRIPT,
            "agenda_text": SAMPLE_AGENDA,
            "roster_text": "出席者：乙野教授、山田課長補佐",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["blocks"] == 1
    assert body["agenda_inferred"] is False
    assert body["assigned_remarks"] == 2
    assert body["unmatched_keys"] == []
    assert body["workflow_run_id"] == "run-2"

    item = body["document"]["議題"][0]["項目"][0]
    # 文字起こし由来の報告者・資料は緑マーカー
    marks = {part["text"]: part["mark"] for part in item["説明"]}
    assert marks["山田課長補佐"] == "green"
    assert marks["資料1"] == "green"
    # 発言者の呼称は名簿の表記(乙野教授)へ寄る
    assert item["質疑応答"][0]["発言者"] == "乙野教授"

    # 2周とも項目一覧付きの振り分け指示が渡っている
    assert len(fake.calls) == 2
    for _, inputs in fake.calls:
        assert inputs["operation"] == "minutes"
        assert "【項目一覧】" in str(inputs["retry_instruction"])


def test_missing_agenda_is_inferred_from_transcript(api_client) -> None:
    fake = FakeWorkflowClient(
        [
            # フェーズ1: 議題構成の推定
            WorkflowRunResult(
                text="#議題 病床稼働率の向上について\n", workflow_run_id="run-1"
            ),
            # 1周目: 推定した骨格へ振り分け
            WorkflowRunResult(
                text="##項目 1\n##発言  | 稼働率の目標は何％なのか。\n",
                workflow_run_id="run-2",
            ),
            # 2周目: 追加なし
            WorkflowRunResult(text="", workflow_run_id="run-3"),
        ]
    )
    app.dependency_overrides[get_workflow_client] = lambda: fake

    response = api_client.post(
        "/v1/text/minutes_structured", json={"text": TRANSCRIPT}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["agenda_inferred"] is True
    assert body["assigned_remarks"] == 1

    topic = body["document"]["議題"][0]
    assert topic["表題"] == "病床稼働率の向上について"
    # 推定議題は黄マーカー表示になる
    assert topic["推定"] is True

    # 最初の呼び出しは議題推定の指示
    assert len(fake.calls) == 3
    assert "#議題" in str(fake.calls[0][1]["retry_instruction"])


def test_unreadable_agenda_returns_422(api_client) -> None:
    fake = FakeWorkflowClient([])
    app.dependency_overrides[get_workflow_client] = lambda: fake

    response = api_client.post(
        "/v1/text/minutes_structured",
        json={"text": "本文です。", "agenda_text": "こんにちは"},
    )

    assert response.status_code == 422
    # 次第が読めない時点で失敗し、LLMは一度も呼ばれない
    assert fake.calls == []
