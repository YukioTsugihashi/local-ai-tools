from __future__ import annotations

from app.document import build_document_data
from app.agenda_parse import parse_agenda

# 架空の次第(test_agenda_parse.pyと同内容。テストは自己完結させる)
SAMPLE_AGENDA = """
令和8年度 第1回 経営企画会議 次第
日 時：令和8年6月9日（火）17:00～
場 所：大会議室
議 題
1 病院の経営状況について
① 月次収支の報告 ・・・・ 資料1
経営企画課 山田 課長補佐
2 その他
次回開催予定：令和8年7月14日（月）
"""


def test_transcript_sources_are_marked_green() -> None:
    agenda = parse_agenda(SAMPLE_AGENDA)
    assignments = {
        "1-1": {
            "報告者": "山田課長補佐",
            "資料": "資料1",
            "発言": [{"発言者": "乙野先生", "内容": "予算は足りるのか。"}],
        }
    }

    document = build_document_data(
        agenda, assignments, roster=["乙野教授", "山田課長補佐"]
    )

    item = document["議題"][0]["項目"][0]
    marks = {part["text"]: part["mark"] for part in item["説明"]}
    assert marks["山田課長補佐"] == "green"
    assert marks["資料1"] == "green"
    # 発言者の呼称は名簿の表記(乙野教授)へ寄る
    assert item["質疑応答"] == [{"発言者": "乙野教授", "内容": "予算は足りるのか。"}]
    assert document["ヘッダ"]["日時"] == "令和8年6月9日（火）17:00～"
    assert document["次回開催"] == "令和8年7月14日（月）"


def test_agenda_fallbacks_are_marked_yellow() -> None:
    agenda = parse_agenda(SAMPLE_AGENDA)

    # 文字起こしから何も取れなかった場合、次第由来の値を黄マーカーで補う
    document = build_document_data(agenda, {})

    item = document["議題"][0]["項目"][0]
    marks = {part["text"]: part["mark"] for part in item["説明"]}
    assert marks["山田課長補佐"] == "yellow"
    assert marks["資料1"] == "yellow"


def test_roster_categories_fill_header() -> None:
    agenda = parse_agenda(SAMPLE_AGENDA)

    document = build_document_data(
        agenda, {}, roster_categories={"出席者": ["乙野教授", "山田課長補佐"]}
    )

    assert document["ヘッダ"]["出席者"] == "乙野教授、山田課長補佐"
