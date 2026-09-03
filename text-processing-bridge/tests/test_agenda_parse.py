from __future__ import annotations

from app.agenda_parse import _item_key, _normalize_key, build_inferred_agenda, parse_agenda

# 架空の次第。実在の会議データはテストに使わない(data/は触らない)。
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


def test_parse_agenda_extracts_skeleton() -> None:
    agenda = parse_agenda(SAMPLE_AGENDA)

    assert agenda.title == "令和8年度 第1回 経営企画会議"
    assert agenda.datetime_text == "令和8年6月9日（火）17:00～"
    assert agenda.place == "大会議室"
    assert agenda.next_meeting == "令和8年7月14日（月）"
    assert [topic.title for topic in agenda.topics] == ["病院の経営状況について", "その他"]

    item = agenda.topics[0].items[0]
    assert item.branch == "①"
    assert item.title == "月次収支の報告"
    assert item.material == "資料1"
    assert item.speaker() == "山田課長補佐"
    assert item.owner_display() == "経営企画課　山田課長補佐"


def test_item_key_absorbs_notation_variants() -> None:
    # 丸数字・全角数字・末尾ドットの揺れを同じキーへ揃える
    assert _item_key("1", "①") == "1-1"
    assert _item_key("１", "1.") == "1-1"
    assert _normalize_key("1-①") == "1-1"
    # 枝番なしの項目はハイフンごと落ちる
    assert _item_key("2", "") == "2"


def test_build_inferred_agenda_merges_similar_titles() -> None:
    agenda = build_inferred_agenda(
        ["病床稼働率の向上について", "病床稼働率の向上について（続き）"]
    )

    assert agenda.inferred is True
    assert len(agenda.topics) == 1
    assert agenda.topics[0].title == "病床稼働率の向上について"
    # 議題ごとに無題項目が1つ付く
    assert len(agenda.topics[0].items) == 1


def test_build_inferred_agenda_falls_back_to_single_topic() -> None:
    agenda = build_inferred_agenda([])

    assert agenda.inferred is True
    assert [topic.title for topic in agenda.topics] == ["会議内容"]
