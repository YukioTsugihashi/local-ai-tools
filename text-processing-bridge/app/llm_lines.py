"""
LLM出力(##項目・##発言・#議題 の行形式)の解析。

指示文(instructions.py)が指定した行形式と対になっている。
LLMは形式を崩すことがあるため、崩れた行の救済もここで行う。
"""

from __future__ import annotations

import re

from app.agenda_parse import MATERIAL_PATTERN, _clean, _normalize_key

LINE_ITEM = re.compile(r"^###?項目[\s　]*[:：]?[\s　]*(.+)$")
LINE_SPEECH = re.compile(r"^#{2,3}発言[\s　]*[:：]?[\s　]*(.+)$")
LINE_REPORT = re.compile(r"^#{2,3}報告(?!内容)[\s　]*[:：]?[\s　]*(.+)$")
LINE_MATERIAL = re.compile(r"^#{2,3}資料[\s　]*[:：]?[\s　]*(.+)$")
LINE_REPORTED = re.compile(r"^#{2,3}報告内容[\s　]*[:：]?[\s　]*(.+)$")

# 名前ではなく役割を指す語。LLMがこれで埋めてきたら空に戻す。
ROLE_PLACEHOLDER = re.compile(
    r"(?:議長|担当者|担当|医師|質問者|回答者|発言者|参加者|司会|事務局|"
    r"出席者|委員|職員|スタッフ|不明|-|―|なし)[\s　]*"
)

TOPIC_LINE = re.compile(r"^#{0,2}[\s　]*議題[\s　]*[:：]?[\s　]*(.+)$")


def parse_structured_lines(text: str) -> dict[str, dict]:
    """LLM出力を {項目記号: {"報告者": str, "発言": [...]}} へ変換する。"""
    result: dict[str, dict] = {}
    current: str | None = None

    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue

        item_match = LINE_ITEM.match(line)
        if item_match:
            # LLMが「1-① 議題1 ○○ / △△」のように説明ごと返すことがあるため、
            # 先頭の記号トークンだけを取り出す。
            current = _normalize_key(
                item_match.group(1).strip().split()[0].strip("　")
            )
            result.setdefault(current, {"報告者": "", "資料": "", "発言": []})
            continue

        report_match = LINE_REPORT.match(line)
        if report_match and current is not None:
            name = report_match.group(1).strip().strip("|｜").strip()
            # 名前として異常な文字列(長文・句点入り)は、LLMが説明文を
            # 混入させたものなので採用しない。空のまま(赤の空欄)にする。
            if (
                name
                and name not in ("不明", "なし", "-", "―")
                and len(name) <= 20
                and "。" not in name
            ):
                result[current]["報告者"] = name
            continue

        reported_match = LINE_REPORTED.match(line)
        if reported_match and current is not None:
            # 報告内容はここで捨てる。LLMの分類を最終判断にしない。
            result[current]["報告数"] = result[current].get("報告数", 0) + 1
            continue

        material_match = LINE_MATERIAL.match(line)
        if material_match and current is not None:
            found = MATERIAL_PATTERN.search(material_match.group(1))
            if found:
                result[current]["資料"] = found.group(1)
            continue

        speech_match = LINE_SPEECH.match(line)
        if speech_match and current is not None:
            body = speech_match.group(1)
            if "|" in body or "｜" in body:
                separator = "|" if "|" in body else "｜"
                speaker, _, content = body.partition(separator)
            else:
                speaker, content = "", body
            speaker = speaker.strip()
            content = content.strip()

            # 役割名は名前ではないので空に戻す。推測を残さないため。
            if ROLE_PLACEHOLDER.fullmatch(speaker):
                speaker = ""
            if content:
                result[current]["発言"].append(
                    {"発言者": speaker, "内容": content}
                )
    return result


def parse_topic_lines(text: str) -> list[str]:
    """議題推定(フェーズ1)の出力から議題表題を取り出す。"""
    titles: list[str] = []
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        match = TOPIC_LINE.match(raw.strip())
        if match:
            title = _clean(match.group(1)).strip(" 　#・「」")
            if 2 <= len(title) <= 30:
                titles.append(title)
    return titles
