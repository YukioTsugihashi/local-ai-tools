"""
出席者名簿の解析と、呼称の表記揺れ補正。

名簿を「正」の表記として扱い、LLMが返す呼称(乙野先生など)を
名簿記載(乙野教授)へ寄せる。推測での置き換えはしない。
"""

from __future__ import annotations

import re

from app.agenda_parse import ROLE_WORDS

ROSTER_LABELS = ("出席者", "陪席者", "欠席者", "事務局", "書記")
ROSTER_LINE = re.compile(
    r"^[\s　]*(" + "|".join(ROSTER_LABELS) + r")[\s　]*[:：](.*)$"
)


def parse_roster_categories(text: str) -> dict[str, list[str]]:
    """
    名簿から区分ごとの氏名一覧を取り出す。

    「出席者：A、B」の行形式(docx等)と、Excel抽出で1行に潰れた
    「出席者 丙野病院長 出席者 …」のトークン列形式の両方に対応する。
    """
    categories: dict[str, list[str]] = {}

    def add(category: str, name: str) -> None:
        name = name.strip(" 　・、,")
        if not name or name in ("区分", "氏名", "役職", "名簿"):
            return
        if not (1 <= len(name) <= 20):
            return
        bucket = categories.setdefault(category, [])
        if name not in bucket:
            bucket.append(name)

    normalized = (text or "").replace("\r\n", "\n")

    # 行形式: 出席者：A、B、C (折り返し行も拾う)
    current: str | None = None
    for raw in normalized.split("\n"):
        line = raw.strip()
        if not line:
            current = None
            continue
        match = ROSTER_LINE.match(line)
        if match:
            current = match.group(1)
            for part in re.split(r"[、,]", match.group(2)):
                add(current, part)
        elif current and "：" not in line and ":" not in line and " " not in line:
            for part in re.split(r"[、,]", line):
                add(current, part)
        else:
            current = None

    # トークン列形式: Excel抽出で改行が失われた場合
    if not categories:
        category: str | None = None
        for token in re.split(r"[\s　]+", normalized.strip()):
            if token in ROSTER_LABELS:
                category = token
            elif category:
                add(category, token)
    return categories


def parse_roster(text: str) -> list[str]:
    """名簿から氏名の一覧(区分なし)を取り出す。呼称の照合表に使う。"""
    names: list[str] = []
    for bucket in parse_roster_categories(text).values():
        for name in bucket:
            if name not in names:
                names.append(name)
    return names


def _strip_role(name: str) -> str:
    """呼称から役職語を落として姓を取り出す(乙野教授→乙野)。"""
    for word in sorted(ROLE_WORDS, key=len, reverse=True):
        if name.endswith(word) and len(name) > len(word):
            return name[: -len(word)]
    return name


def normalize_person(name: str, roster: list[str] | None) -> str:
    """
    呼称を名簿の表記へ寄せる(乙野先生→乙野教授)。

    姓が一致する名簿記載が1人だけのときに限り置き換える。
    同姓が複数いる場合や名簿に無い場合は元の呼称を保つ。
    """
    name = (name or "").strip()
    if not name or not roster:
        return name
    family = _strip_role(name)
    if not family:
        return name
    hits = [entry for entry in roster if _strip_role(entry) == family]
    return hits[0] if len(hits) == 1 else name
