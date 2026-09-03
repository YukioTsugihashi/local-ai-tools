from __future__ import annotations

from app.kebatori import (
    KebatoriProfiles,
    apply_kebatori,
    remove_adjacent_duplicate_sentences,
    remove_leading_sentence_punctuation,
)


def test_kebatori_yaml_loads_default_profile() -> None:
    profile = KebatoriProfiles.from_yaml().get("default")

    assert "えー" in profile.filler_phrases
    assert profile.remove_adjacent_duplicate_sentences is True


def test_only_configured_fillers_are_removed() -> None:
    profile = KebatoriProfiles.from_yaml().get("default")

    result = apply_kebatori("えー 本題です。未設定語 本文です。", profile)

    assert result == "本題です。未設定語 本文です。"


def test_comma_after_filler_is_removed() -> None:
    profile = KebatoriProfiles.from_yaml().get("default")

    result = apply_kebatori(
        "えー、本日の会議を開始します。あのー、次の議題です。",
        profile,
    )

    assert result == "本日の会議を開始します。次の議題です。"


def test_meaningful_ano_and_sono_are_not_removed() -> None:
    profile = KebatoriProfiles.from_yaml().get("default")
    source = "あの資料を確認し、その結果を報告します。"

    assert apply_kebatori(source, profile) == source


def test_demonstratives_must_not_be_registered_as_fillers() -> None:
    """
    指示語・副詞をフィラー一覧へ入れると本文が壊れる。

    削除は「語頭か句読点の直後」で発火し、後ろは何でもよい。そのため
    「あの」を登録すると「、あの件」→「、件」になる。実際に一度入れて壊した。
    """
    profile = KebatoriProfiles.from_yaml().get("default")

    dangerous = {"あの", "その", "こう", "なんか", "これ", "それ"}
    registered = dangerous & set(profile.filler_phrases)
    assert not registered, f"意味語がフィラーに登録されている: {sorted(registered)}"

    for source in (
        "資料を確認しました。その資料は来週使います。",
        "会議の件ですが、あの件はどうなりましたか。",
        "検討します。こう考えています。",
        "報告します。なんか問題がありました。",
    ):
        assert apply_kebatori(source, profile) == source, source


def test_added_fillers_are_removed_without_touching_numbers() -> None:
    profile = KebatoriProfiles.from_yaml().get("default")
    source = "えー、本日は、まあ第3回の会議です。あのー、患者数は1250人でした。"

    result = apply_kebatori(source, profile)

    assert result == "本日は、第3回の会議です。患者数は1250人でした。"


def test_only_adjacent_exact_duplicate_sentences_are_removed() -> None:
    profile = KebatoriProfiles.from_yaml().get("default")

    result = apply_kebatori(
        "確認します。確認します。確認しました。確認します。",
        profile,
    )

    assert result == "確認します。確認しました。確認します。"


def test_adjacent_duplicates_with_different_punctuation_are_removed() -> None:
    result = remove_adjacent_duplicate_sentences(
        "本日の会議を開始します。確認します。確認します！！"
    )

    assert result == "本日の会議を開始します。確認します。"


def test_leading_sentence_punctuation_is_removed_from_each_line() -> None:
    result = remove_leading_sentence_punctuation(
        "、本日の会議では公開日を決定しました。\n　。担当者は丁野さんです。"
    )

    assert result == "本日の会議では公開日を決定しました。\n　担当者は丁野さんです。"


def test_punctuation_inside_sentences_is_preserved() -> None:
    source = "本日の会議では、公開日を決定しました。"

    assert remove_leading_sentence_punctuation(source) == source


def test_result_never_exceeds_source_length() -> None:
    profile = KebatoriProfiles.from_yaml().get("default")
    source = "えー  本題です！！"

    result = apply_kebatori(source, profile)

    assert len(result) <= len(source)


def test_nan_to_iu_is_removed() -> None:
    profile = KebatoriProfiles.from_yaml().get("default")

    result = apply_kebatori(
        "なんというか、本日の会議を開始します。あのー、次の議題です。",
        profile,
    )

    assert result == "本日の会議を開始します。次の議題です。"


def test_new_filler_is_removed() -> None:
    profile = KebatoriProfiles.from_yaml().get("default")
    
    result = apply_kebatori("なんというか、本題です。", profile)
    
    assert result == "本題です。"


# ---------------------------------------------------------------------------
# 1行おきの空行(docx抽出が段落を空行で連結する)
# ---------------------------------------------------------------------------

from app.kebatori import collapse_blank_line_padding  # noqa: E402


def _double_spaced(lines: list[str]) -> str:
    """Open WebUIのdocx抽出と同じ、段落ごとに空行を挟んだ形。"""
    return "\n\n".join(lines)


def test_1行おきの空行を落とす() -> None:
    """出力Wordが1行おきに空く原因。実測で1,420段落中707が空だった。"""
    lines = [f"発言{i}です。よろしくお願いします。" for i in range(8)]
    result = collapse_blank_line_padding(_double_spaced(lines))
    assert result == "\n".join(lines)
    assert "\n\n" not in result


def test_意図した段落分けには手を出さない() -> None:
    """空行が2つ以上続く箇所があるなら、書き手が分けたものとみなす。"""
    text = "見出し\n\n\n本文1\n本文2\n\n\n見出し2\n本文3\n本文4\n本文5"
    assert collapse_blank_line_padding(text) == text


def test_空行がまばらな文書には手を出さない() -> None:
    lines = [f"行{i}" for i in range(20)]
    text = "\n".join(lines[:10] + [""] + lines[10:])
    assert collapse_blank_line_padding(text) == text


def test_短い文書には手を出さない() -> None:
    text = "あ\n\nい"
    assert collapse_blank_line_padding(text) == text


def test_ケバ取りの出力が1行おきに空かない() -> None:
    """入口(apply_kebatori)を通したときに効いていること。"""
    from app.kebatori import KebatoriProfile, apply_kebatori

    lines = [f"えーと、発言{i}です。" for i in range(8)]
    profile = KebatoriProfile(
        name="試験用",
        filler_phrases=("えーと、",),
        remove_adjacent_duplicate_sentences=False,
        normalize_whitespace=False,
        normalize_repeated_punctuation=False,
    )
    result = apply_kebatori(_double_spaced(lines), profile)

    assert "\n\n" not in result, "1行おきの空行が残っている"
    assert "えーと、" not in result, "ケバ取り自体が効かなくなっている"
    assert result.count("\n") == len(lines) - 1


def test_かたまりの端に空行が続いても他の1行空きは詰める() -> None:
    """長文は分割して処理するため、かたまりの端に空行が並ぶことがある。

    連続が1か所でもあると全部あきらめる作りだったので、ケバ取り強では
    ほとんど効いていなかった(700行中300行が空のまま。2026-08-18に実測)。
    """
    lines = [f"発言{i}です。よろしくお願いします。" for i in range(8)]
    text = "\n\n" + "\n\n".join(lines)  # 先頭に空行が2つ並ぶ

    result = collapse_blank_line_padding(text)
    body = result.lstrip("\n")
    assert "" not in body.split("\n"), "1行だけの空きが残っている"
    assert body.split("\n") == lines
    assert result.startswith("\n\n"), "続いた空行は書き手の区切りとして残す"


def test_2行以上続く空行はそのまま残す() -> None:
    text = "\n".join(["本文1", "", "", "本文2", "", "本文3", "", "本文4", "", "本文5"])
    result = collapse_blank_line_padding(text)
    assert result.split("\n") == ["本文1", "", "", "本文2", "本文3", "本文4", "本文5"]
