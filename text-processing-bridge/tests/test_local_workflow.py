"""
Dify撤去後の操作分岐のテスト。

旧ワークフローの operation-router と同じ振り分けになっていること、
ケバ取りがLLMを使わないこと、LLM候補の検証が効いていることを見る。
"""

from __future__ import annotations

import json

import pytest

from app.kebatori import (
    KebatoriProfile,
    apply_kebatori_plus,
    profile_from_rules,
    select_filler_candidates,
)
from app.local_workflow import LocalWorkflowClient
from app.prompts import LLM_PROMPTS


class FakeChatClient:
    """呼ばれた回数とプロンプトを記録するだけのLLM。"""

    def __init__(self, reply: str = "生成結果") -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def complete(self, prompt, *, model, num_predict, temperature) -> str:
        self.calls.append(
            {
                "prompt": prompt,
                "model": model,
                "num_predict": num_predict,
                "temperature": temperature,
            }
        )
        return self.reply


RULES = json.dumps(
    {
        "filler_phrases": ["えー", "あのー"],
        "remove_adjacent_duplicate_sentences": True,
        "normalize_whitespace": True,
        "normalize_repeated_punctuation": True,
    },
    ensure_ascii=False,
)


@pytest.mark.anyio
@pytest.mark.parametrize("operation", sorted(LLM_PROMPTS))
async def test_llm_operations_call_the_model_once(operation) -> None:
    chat = FakeChatClient()
    client = LocalWorkflowClient(chat)

    result = await client.run(
        "text_processing",
        {"operation": operation, "text": "本文", "max_chars": 120, "retry_instruction": "簡潔に"},
    )

    assert result.text == "生成結果"
    assert result.workflow_run_id
    assert len(chat.calls) == 1
    prompt = chat.calls[0]["prompt"]
    # プロンプトの差し込み口が埋まっていること(未置換の波括弧が残らない)
    assert "本文" in prompt
    assert "{text}" not in prompt and "{retry_instruction}" not in prompt
    assert chat.calls[0]["num_predict"] == LLM_PROMPTS[operation].num_predict


@pytest.mark.anyio
async def test_kebatori_does_not_use_the_model() -> None:
    chat = FakeChatClient()
    client = LocalWorkflowClient(chat)

    result = await client.run(
        "text_processing",
        {
            "operation": "kebatori",
            # フィラー削除は語頭か区切りの直後だけを対象にする(意味語を巻き込まないため)
            "text": "えー、本日は、あのー資料の件です。",
            "kebatori_rules_json": RULES,
        },
    )

    # ルールだけで済む処理でLLMを呼ぶと、遅くなるうえ結果が揺れる
    assert chat.calls == []
    assert "えー" not in result.text and "あのー" not in result.text
    assert "資料の件です。" in result.text


@pytest.mark.anyio
async def test_kebatori_plus_detects_then_deletes_in_code() -> None:
    chat = FakeChatClient(reply='["まあ", "なんか"]')
    client = LocalWorkflowClient(chat)

    result = await client.run(
        "text_processing",
        {
            "operation": "kebatori_plus",
            # 短すぎる文だと削除量が3割を超えて候補が丸ごと捨てられるため、
            # 実際の文字起こしに近い長さで確かめる
            "text": "まあ、本日の会議では なんか 新しい資料の取り扱いについて説明します。",
            "kebatori_rules_json": RULES,
        },
    )

    assert len(chat.calls) == 1
    assert "まあ" not in result.text
    assert "なんか" not in result.text
    assert "新しい資料の取り扱いについて説明します。" in result.text


@pytest.mark.anyio
async def test_unknown_workflow_name_is_rejected() -> None:
    client = LocalWorkflowClient(FakeChatClient())

    with pytest.raises(Exception, match="未登録の処理"):
        await client.run("別の処理", {"operation": "rewrite", "text": "x"})


def test_candidate_validation_rejects_meaningful_words() -> None:
    source = "まあ、その資料は3件あります。うーん、はい。"
    output = json.dumps(
        [
            "まあ",              # 採用
            "うーん",            # 採用
            "その資料",          # 漢字を含むので却下
            "3件",               # 数字を含むので却下
            "あります。",        # 句点を含むので却下
            "ここには無い語",    # 原文に無いので却下
            "まあ",              # 重複
            "あ" * 20,           # 長すぎるので却下
        ],
        ensure_ascii=False,
    )

    assert select_filler_candidates(output, source) == ["まあ", "うーん"]


def test_candidate_deletion_is_abandoned_when_it_removes_too_much() -> None:
    profile = KebatoriProfile("t", (), False, False, False)
    source = "あああ"

    # 3割を超えて削れる場合はLLM候補を丸ごと捨てる
    assert apply_kebatori_plus(source, profile, ["あああ"]) == source


def test_broken_llm_output_is_ignored() -> None:
    assert select_filler_candidates("JSONではない返答", "本文") == []
    assert select_filler_candidates("[壊れた", "本文") == []


def test_profile_from_rules_ignores_malformed_entries() -> None:
    profile = profile_from_rules(
        {"filler_phrases": ["えー", "", 123, None], "normalize_whitespace": True}
    )

    assert profile.filler_phrases == ("えー",)
    assert profile.normalize_whitespace is True
    assert profile.remove_adjacent_duplicate_sentences is False
