from __future__ import annotations

from app.roster import normalize_person, parse_roster, parse_roster_categories


def test_line_format_roster_is_parsed_by_category() -> None:
    categories = parse_roster_categories(
        "出席者：乙野教授、山田課長補佐\n事務局：佐藤主幹"
    )

    assert categories == {
        "出席者": ["乙野教授", "山田課長補佐"],
        "事務局": ["佐藤主幹"],
    }


def test_token_format_roster_survives_lost_newlines() -> None:
    # Excel抽出で改行が失われ、1行のトークン列になった場合
    categories = parse_roster_categories(
        "出席者 丙野病院長 出席者 乙野教授 事務局 佐藤主幹"
    )

    assert categories == {
        "出席者": ["丙野病院長", "乙野教授"],
        "事務局": ["佐藤主幹"],
    }


def test_parse_roster_flattens_without_duplicates() -> None:
    names = parse_roster("出席者:乙野教授、山田課長補佐\n書記:乙野教授")

    assert names == ["乙野教授", "山田課長補佐"]


def test_normalize_person_prefers_roster_notation() -> None:
    roster = ["乙野教授", "山田課長補佐"]

    # 呼称違い(先生→教授)は名簿の表記へ寄せる
    assert normalize_person("乙野先生", roster) == "乙野教授"
    # 名簿に無い姓はそのまま
    assert normalize_person("戊野部長", roster) == "戊野部長"
    # 名簿が無ければそのまま
    assert normalize_person("乙野先生", None) == "乙野先生"


def test_normalize_person_keeps_ambiguous_names() -> None:
    # 同姓が複数いる場合は置き換えない
    roster = ["乙野教授", "乙野技師長"]

    assert normalize_person("乙野先生", roster) == "乙野先生"
