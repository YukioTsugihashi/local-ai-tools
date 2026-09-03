from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from app.config import CONFIG_DIR

# LLMが挙げた候補のうち、実際に削ってよいものの条件。
# 文まるごとや意味語を消さないための柵であり、緩めると本文が壊れる。
CANDIDATE_MAX_CHARS = 12
# 削除量が原文の3割を超えたらLLM候補を丸ごと捨てる
CANDIDATE_MIN_REMAINING_RATIO = 0.7


@dataclass(frozen=True)
class KebatoriProfile:
    name: str
    filler_phrases: tuple[str, ...]
    remove_adjacent_duplicate_sentences: bool
    normalize_whitespace: bool
    normalize_repeated_punctuation: bool

    def response_rules(self) -> dict[str, object]:
        rules = asdict(self)
        rules.pop("name")
        rules["filler_phrases"] = list(self.filler_phrases)
        return rules

    def workflow_rules(self) -> dict[str, object]:
        return {"profile": self.name, **self.response_rules()}


class KebatoriProfiles:
    def __init__(self, profiles: dict[str, KebatoriProfile]) -> None:
        self._profiles = profiles

    @classmethod
    def from_yaml(cls, path: Path | None = None) -> KebatoriProfiles:
        config_path = path or CONFIG_DIR / "kebatori.yaml"
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        raw_profiles = payload.get("profiles")
        if not isinstance(raw_profiles, dict) or not raw_profiles:
            raise ValueError("kebatori.yamlにprofiles設定がありません。")

        profiles: dict[str, KebatoriProfile] = {}
        for name, raw_profile in raw_profiles.items():
            if not isinstance(raw_profile, dict):
                raise ValueError(f"毛羽取りプロファイルが不正です: {name}")
            raw_fillers = raw_profile.get("filler_phrases", [])
            if not isinstance(raw_fillers, list) or not all(
                isinstance(item, str) and item for item in raw_fillers
            ):
                raise ValueError(f"filler_phrasesが不正です: {name}")
            profiles[str(name)] = KebatoriProfile(
                name=str(name),
                filler_phrases=tuple(raw_fillers),
                remove_adjacent_duplicate_sentences=bool(
                    raw_profile.get("remove_adjacent_duplicate_sentences", False)
                ),
                normalize_whitespace=bool(
                    raw_profile.get("normalize_whitespace", False)
                ),
                normalize_repeated_punctuation=bool(
                    raw_profile.get("normalize_repeated_punctuation", False)
                ),
            )
        return cls(profiles)

    def get(self, name: str) -> KebatoriProfile:
        try:
            return self._profiles[name]
        except KeyError as error:
            raise KeyError(f"毛羽取りプロファイルがありません: {name}") from error


# 「1行おきに空行」とみなす空行の割合。きれいに交互なら約0.5になる
BLANK_PADDING_MIN_RATIO = 0.35


def normalize_line_endings(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def collapse_blank_line_padding(text: str) -> str:
    """
    1行おきに空行が入っている文書から、その空行だけを落とす。

    Open WebUIのdocx抽出は段落を空行で連結するため、文字起こしdocxを
    渡すと全行が空行で挟まれた状態で届く。ケバ取りは本文をそのまま通すので、
    出力のWordが1行おきに空いた見た目になる(2026-08-18に利用者から指摘。
    実測で1,420段落中707が空、しかも空行の連続は1つも無い)。
    要約や議事録はLLMが文章を書き直すため、この症状は出ない。

    落とすのは**1行だけの空き**。2行以上続く空行は書き手が入れた区切りと
    みなしてそのまま残す。空行がまばらな文書(割合が低い)には手を出さない。

    「連続が1か所でもあれば全部あきらめる」作りにしていたが、長文は
    分割して処理するため、かたまりの端に空行が並ぶだけで効かなくなっていた
    (ケバ取り強で700行中300行が空のまま。2026-08-18に実測)。
    """
    lines = text.split("\n")
    if len(lines) < 6:
        return text
    blank_total = sum(1 for line in lines if not line.strip())
    if blank_total < len(lines) * BLANK_PADDING_MIN_RATIO:
        return text
    kept: list[str] = []
    run = 0
    for line in lines:
        if not line.strip():
            run += 1
            continue
        if run >= 2:
            kept.extend([""] * run)
        kept.append(line)
        run = 0
    if run >= 2:
        kept.extend([""] * run)
    return "\n".join(kept)


def remove_leading_sentence_punctuation(text: str) -> str:
    return re.sub(
        r"(?m)^([ \t\u3000]*)[、。，．｡､]+[ \t\u3000]*",
        r"\1",
        text,
    )


def apply_kebatori(text: str, profile: KebatoriProfile) -> str:
    source = collapse_blank_line_padding(normalize_line_endings(text))
    result = source

    if profile.filler_phrases:
        phrases = sorted(profile.filler_phrases, key=len, reverse=True)
        alternatives = "|".join(re.escape(phrase) for phrase in phrases)
        result = re.sub(
            rf"(^|[\s、。,.!?！？])(?:{alternatives})[、，,]?",
            lambda match: match.group(1),
            result,
        )

    if profile.remove_adjacent_duplicate_sentences:
        result = remove_adjacent_duplicate_sentences(result)

    if profile.normalize_repeated_punctuation:
        result = re.sub(r"([。！？!?])\1+", r"\1", result)

    if profile.normalize_whitespace:
        result = re.sub(r"[ \t\u3000]+", " ", result)
        result = re.sub(r" *\n *", "\n", result)
        result = re.sub(r"\n{3,}", "\n\n", result)
        result = result.strip()

    result = remove_leading_sentence_punctuation(result)
    return source if len(result) > len(source) else result


def _remove_phrases(text: str, phrases: list[str]) -> str:
    """語頭・句読点の直後に現れる指定語を落とす。"""
    if not phrases:
        return text
    ordered = sorted(set(phrases), key=len, reverse=True)
    alternatives = "|".join(re.escape(phrase) for phrase in ordered)
    return re.sub(
        rf"(^|[\s、。,.!?！？…])(?:{alternatives})[、，,]?",
        lambda match: match.group(1),
        text,
    )


def _remove_candidates(text: str, phrases: list[str]) -> str:
    """
    LLMが挙げた候補を落とす。

    「こう」「その」など語頭と紛らわしい語を含むため、
    前後どちらも語の境界であるときだけ削除する。
    """
    if not phrases:
        return text
    ordered = sorted(set(phrases), key=len, reverse=True)
    alternatives = "|".join(re.escape(phrase) for phrase in ordered)
    return re.sub(
        rf"(^|[\s、。,.!?！？…])(?:{alternatives})"
        rf"(?:[、，,]|(?=[\s。！？!?…])|$)",
        lambda match: match.group(1),
        text,
    )


def select_filler_candidates(llm_output: str, source: str) -> list[str]:
    """
    LLMのJSON配列から、実際に削ってよい短いつなぎ語だけを選ぶ。

    LLMは文まるごとや意味語を挙げてくることがあるため、
    ここを通ったものだけを削除対象にする。
    """
    array_match = re.search(r"\[.*\]", llm_output or "", re.S)
    if not array_match:
        return []
    try:
        parsed = json.loads(array_match.group(0))
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []

    validated: list[str] = []
    seen: set[str] = set()
    for candidate in parsed:
        if not isinstance(candidate, str):
            continue
        phrase = candidate.strip()
        if not phrase or len(phrase) > CANDIDATE_MAX_CHARS:
            continue
        if re.search(r"[0-9０-９]", phrase):
            continue
        if re.search(r"[。．！？!?]", phrase):
            continue
        if re.search(r"[一-鿿]", phrase):  # 漢字を含む語は意味語とみなす
            continue
        if phrase in seen or phrase not in source:
            continue
        seen.add(phrase)
        validated.append(phrase)
    return validated


def apply_kebatori_plus(
    text: str, profile: KebatoriProfile, candidates: list[str]
) -> str:
    """ルール由来のフィラーに加え、検証済みのLLM候補も落とす。"""
    source = collapse_blank_line_padding(normalize_line_endings(text))
    result = _remove_phrases(source, list(profile.filler_phrases))
    with_candidates = _remove_candidates(result, candidates)
    if len(with_candidates) >= len(source) * CANDIDATE_MIN_REMAINING_RATIO:
        result = with_candidates

    if profile.remove_adjacent_duplicate_sentences:
        result = remove_adjacent_duplicate_sentences(result)
    if profile.normalize_repeated_punctuation:
        result = re.sub(r"([。！？!?])\1+", r"\1", result)
    if profile.normalize_whitespace:
        result = re.sub(r"[ \t　]+", " ", result)
        result = re.sub(r" *\n *", "\n", result)
        result = re.sub(r"\n{3,}", "\n\n", result)
        result = result.strip()

    result = remove_leading_sentence_punctuation(result)
    return source if len(result) > len(source) else result


def profile_from_rules(rules: dict[str, object]) -> KebatoriProfile:
    """呼び出し側が渡すルール辞書からプロファイルを組む。"""
    raw_fillers = rules.get("filler_phrases", [])
    fillers = tuple(
        phrase
        for phrase in (raw_fillers if isinstance(raw_fillers, list) else [])
        if isinstance(phrase, str) and phrase
    )
    return KebatoriProfile(
        name=str(rules.get("profile", "runtime")),
        filler_phrases=fillers,
        remove_adjacent_duplicate_sentences=rules.get("remove_adjacent_duplicate_sentences") is True,
        normalize_whitespace=rules.get("normalize_whitespace") is True,
        normalize_repeated_punctuation=rules.get("normalize_repeated_punctuation") is True,
    )


def remove_adjacent_duplicate_sentences(text: str) -> str:
    tokens = re.findall(r"[^。！？!?\n]+[。！？!?]*|\n+", text)
    output: list[str] = []
    previous_sentence: str | None = None
    for token in tokens:
        if token.isspace():
            output.append(token)
            continue
        sentence = re.sub(r"[\s。！？!?]+", "", token)
        if sentence and sentence == previous_sentence:
            continue
        output.append(token)
        if sentence:
            previous_sentence = sentence
    return "".join(output)
