"""文字起こし健康診断の単体テスト。

実データは使わず、その場で作った架空の文章だけで確かめる。
「見つけるべきものを見つける」だけでなく、
**「見つけてはいけないものを見つけない」**ことも確かめる
(誤検出が多い診断は、直す必要のない箇所まで直させてしまうため)。
"""

from __future__ import annotations

import main


def lines_of(text: str):
    return [
        (number, line)
        for number, line in enumerate(text.splitlines(), start=1)
        if line.strip()
    ]


def test_発言者が分からない行を見つける() -> None:
    text = "甲野: おはようございます。\nはい、その件は確認しております。\n"
    found = main.find_unknown_speaker(lines_of(text))
    assert [number for number, _ in found] == [2]


def test_発言者つきの行は誤検出しない() -> None:
    text = (
        "甲野: 本日はよろしくお願いします。\n"
        "乙野さん 資料を配ります。\n"
        "丙野主任: 数字を確認しました。\n"
    )
    assert main.find_unknown_speaker(lines_of(text)) == []


def test_短い相槌は発言者不明として数えない() -> None:
    # 「はい。」程度の行まで拾うと指摘だらけになって使えない
    text = "甲野: どうぞ。\nはい。\nええ。\n"
    assert main.find_unknown_speaker(lines_of(text)) == []


def test_見出し行を発言者不明として数えない() -> None:
    # タイトルや議題の見出しは発言ではない。指摘するとノイズになる
    text = (
        "第5回 運営会議(架空)\n"
        "議題1 来年度の体制について\n"
        "はい、その件は確認しております。\n"
    )
    found = main.find_unknown_speaker(lines_of(text))
    assert [number for number, _ in found] == [3]


def test_序数は数値として拾わない() -> None:
    # 「第5回」は回数の情報ではない。拾うと目視すべき箇所が埋もれる
    assert main.find_numberish(lines_of("甲野: 第5回の会議です。\n")) == []
    # 「5回」単独は拾う
    assert main.find_numberish(lines_of("甲野: 5回実施しました。\n"))


def test_同じ内容の繰り返しを見つける() -> None:
    text = (
        "甲野: 来週までに資料をまとめてください。\n"
        "乙野: 承知しました。\n"
        "甲野: 来週までに資料をまとめてください\n"  # 句点と空白の違いは同じ扱い
    )
    found = main.find_duplicates(lines_of(text))
    assert len(found) == 1
    assert found[0][0] == 1 and found[0][1] == 3


def test_全角半角の違いは同じ扱いにする() -> None:
    # 同じ人が同じことを言っている、が全角半角だけ違う場合
    text = "甲野: 対象は１２０名です。\n乙野: 了解しました。\n甲野: 対象は120名です。\n"
    assert len(main.find_duplicates(lines_of(text))) == 1


def test_表記の変種までは拾わない() -> None:
    # 「ください/下さい」のような変種は**別物として扱う**。
    # 推測で寄せると、直す必要のない箇所まで指摘してしまうため(このツールの方針)
    text = (
        "甲野: 来週までに資料をまとめてください。\n"
        "乙野: 来週までに資料をまとめて下さい。\n"
    )
    assert main.find_duplicates(lines_of(text)) == []


def test_短い相槌は重複として数えない() -> None:
    text = "甲野: はい。\n乙野: はい。\n丙野: はい。\n"
    assert main.find_duplicates(lines_of(text)) == []


def test_長すぎるかたまりを見つける() -> None:
    text = "甲野: " + "あ" * (main.LONG_CHUNK_CHARS + 10) + "\n短い行\n"
    found = main.find_long_chunks(lines_of(text))
    assert [number for number, _ in found] == [1]


def test_数値と日時と担当を拾う() -> None:
    text = (
        "甲野: 4月10日までにお願いします。\n"
        "乙野: 対象は120名です。\n"
        "丙野: 開始は14:30です。\n"
        "丁野: 担当は乙野さんです。\n"
        "戊野: それでは始めます。\n"
    )
    found = main.find_numberish(lines_of(text))
    assert sorted(number for number, _ in found) == [1, 2, 3, 4]


def test_文字化けと記号の乱れを見つける() -> None:
    text = "甲野: これは�化けています。\n乙野: そうですね、、、、\n"
    found = main.find_broken_text(lines_of(text))
    assert [number for number, _ in found] == [1, 2]


def test_診断結果は対処が要るものと補足を分ける() -> None:
    text = (
        "甲野: 4月10日までにお願いします。\n"
        "はい、その件は確認しております。\n"
    )
    message, skipped, notes = main.diagnose(text)
    assert "発言者が分からない行" in message
    # 対処が要るもの(誰の発言か補う)と、補足(目視してほしい)を混ぜない
    assert any("発言者" in item for item in skipped)
    assert any("数値" in item for item in notes)
    assert not any("数値" in item for item in skipped)


def test_問題が無ければそう伝える() -> None:
    text = "甲野: おはようございます。\n乙野: よろしくお願いします。\n"
    message, skipped, _ = main.diagnose(text)
    assert skipped == []
    assert "問題は見つかりませんでした" in message


def test_中身が空なら対処を促す() -> None:
    message, skipped, _ = main.diagnose("\n \n")
    assert "空" in message and skipped


def test_抜粋は長すぎない() -> None:
    # 報告へ載せる本文は必要最小限にする
    assert len(main._excerpt("あ" * 100)) <= 31
