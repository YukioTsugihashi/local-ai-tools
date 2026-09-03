from __future__ import annotations

from app.llm_lines import parse_structured_lines, parse_topic_lines


def test_structured_lines_are_grouped_by_item() -> None:
    result = parse_structured_lines(
        "##項目 1-1\n"
        "##報告 山田課長補佐\n"
        "##資料 資料1\n"
        "##発言 乙野教授 | 予算は足りるのか。\n"
        "##発言  | 来月に補正を予定している。\n"
    )

    assert result["1-1"]["報告者"] == "山田課長補佐"
    assert result["1-1"]["資料"] == "資料1"
    assert result["1-1"]["発言"] == [
        {"発言者": "乙野教授", "内容": "予算は足りるのか。"},
        {"発言者": "", "内容": "来月に補正を予定している。"},
    ]


def test_item_symbol_is_normalized_and_trailing_text_dropped() -> None:
    # LLMが記号の後ろへ議題名を書き足しても、先頭トークンだけを採用する
    result = parse_structured_lines(
        "##項目 1-① 議題1 病院の経営状況について\n##発言 A | 進めてよいか。\n"
    )

    assert list(result.keys()) == ["1-1"]


def test_role_placeholder_speaker_is_blanked() -> None:
    # 「議長」のような役割名は名前ではないので空へ戻す(推測を残さない)
    result = parse_structured_lines("##項目 1-1\n##発言 議長 | 進めてよいか。\n")

    assert result["1-1"]["発言"] == [{"発言者": "", "内容": "進めてよいか。"}]


def test_reported_lines_are_counted_but_discarded() -> None:
    result = parse_structured_lines(
        "##項目 1-1\n##報告内容 稼働率は79.1%であった。\n"
    )

    assert result["1-1"]["発言"] == []
    assert result["1-1"]["報告数"] == 1


def test_abnormal_reporter_names_are_rejected() -> None:
    # 説明文の混入(長文・句点入り)は報告者として採用しない
    result = parse_structured_lines(
        "##項目 1-1\n##報告 山田課長補佐が資料1に基づき説明を行った。続いて質疑があった。\n"
    )

    assert result["1-1"]["報告者"] == ""


def test_topic_lines_are_extracted_with_length_limits() -> None:
    titles = parse_topic_lines(
        "#議題 病床稼働率の向上について\n"
        "議題: 次年度予算の編成方針\n"
        "#議題 あ\n"
        "これは本文なので無視される\n"
    )

    assert titles == ["病床稼働率の向上について", "次年度予算の編成方針"]
