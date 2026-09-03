"""
会議次第の解析。

次第には議題番号・表題・枝番・資料番号・担当が載っている。ここから骨格を
確定させることで、LLMの仕事を「どの発言がどの項目に属するか」だけに絞る。
推測させないための土台。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.merge import _dedup_key


# 「１ 病院の経営状況について」「1 病院の経営状況について」
TOPIC_PATTERN = re.compile(r"^[\s　]*([0-9０-９]{1,2})[\s　.．、]+(\S.*?)[\s　]*$")
# 「① 令和8年…（速報）・・・・ 資料１」
ITEM_PATTERN = re.compile(
    r"^[\s　]*([①-⑳]|[0-9０-９]{1,2}[.．])[\s　]*(.*?)"
    r"[・･。\.\s　]*(資料[0-9０-９]{1,2})?[\s　]*$"
)
MATERIAL_PATTERN = re.compile(r"(資料[0-9０-９]{1,2})")
# 「経営企画課 甲野 課長補佐」のような担当行。役職語で終わる。
ROLE_WORDS = (
    "課長補佐", "課長", "部長", "係長", "主幹", "技師長", "教授", "准教授",
    "先生", "病院長", "副院長", "院長補佐", "主任調整員", "センター長", "師長",
)
DATE_LINE_PATTERN = re.compile(r"^[\s　]*(日\s*時|場\s*所)[\s　]*[:：](.+)$")
NEXT_PATTERN = re.compile(r"次回開催予定[\s　]*[:：]?[\s　]*(.+)$")
TITLE_PATTERN = re.compile(r"^[\s　]*(令和.*?(?:会議|委員会).*?)(?:次第|議事録)?[\s　]*$")

_ZEN2HAN = str.maketrans("０１２３４５６７８９", "0123456789")
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"

_KEY_NORMALIZE = {ch: str(i + 1) for i, ch in enumerate(CIRCLED)}
_KEY_NORMALIZE.update({chr(ord("０") + i): str(i) for i in range(10)})
_KEY_TRANS = str.maketrans(_KEY_NORMALIZE)


def _normalize_key(key: str) -> str:
    """照合キーの正規化。丸数字→半角、全角→半角、末尾ドット除去。"""
    return key.translate(_KEY_TRANS).rstrip(".．-")


def _item_key(number: str, branch: str) -> str:
    """項目の照合キー。枝番の表記揺れをすべて吸収して揃える。"""
    return _normalize_key(f"{number}-{branch}")


@dataclass
class AgendaItem:
    branch: str = ""
    title: str = ""
    material: str = ""
    owner: str = ""

    def key(self) -> str:
        return f"{self.branch}{self.title}"

    def speaker(self) -> str:
        """
        「経営企画課　甲野　課長補佐」から説明者名「甲野課長補佐」を作る。

        次第は 部署／姓／役職 を空白で区切るが、議事録では姓と役職を続けて
        書くため、末尾2要素を連結する。
        """
        parts = [p for p in re.split(r"[\s　]+", self.owner) if p]
        if len(parts) >= 2:
            return parts[-2] + parts[-1]
        return parts[-1] if parts else ""

    def owner_display(self) -> str:
        """担当欄の表記「経営企画課　甲野課長補佐」を作る。"""
        parts = [p for p in re.split(r"[\s　]+", self.owner) if p]
        if len(parts) >= 3:
            return "　".join(parts[:-2]) + "　" + parts[-2] + parts[-1]
        return "　".join(parts)


@dataclass
class AgendaTopic:
    number: str = ""
    title: str = ""
    items: list[AgendaItem] = field(default_factory=list)


@dataclass
class Agenda:
    title: str = ""
    datetime_text: str = ""
    place: str = ""
    next_meeting: str = ""
    topics: list[AgendaTopic] = field(default_factory=list)
    # 次第からではなく文字起こしから推定した骨格かどうか。
    # 推定の場合、見出しは黄マーカーで表示する。
    inferred: bool = False

    def is_empty(self) -> bool:
        return not self.topics

    def flat_items(self) -> list[tuple[AgendaTopic, AgendaItem]]:
        return [(t, i) for t in self.topics for i in t.items]


def _looks_like_owner(line: str) -> bool:
    stripped = line.strip().rstrip("・.。 　")
    if not stripped or len(stripped) > 40:
        return False
    return any(stripped.endswith(word) for word in ROLE_WORDS)


def _clean(text: str) -> str:
    """次第の点線(・・・・)や余分な空白を落とす。"""
    text = re.sub(r"[・･]{3,}", "", text)
    text = re.sub(r"[…]{1,}", "", text)
    return text.strip(" 　")


def _tighten(text: str) -> str:
    """
    PDF抽出で数字の前後に入る空白を詰める。

    「令和 8 年 6 月 9 日（火）17:00～」→「令和8年6月9日（火）17:00～」
    docx由来なら元から詰まっているため、この処理は無害。
    """
    text = re.sub(r"(?<=[0-9])\s+(?=[年月日時分回])", "", text)
    text = re.sub(r"(?<=[年月日])\s+(?=[0-9])", "", text)
    text = re.sub(r"(?<=[令和平成昭和第])\s+(?=[0-9])", "", text)
    text = re.sub(r"\s+(?=[（(])", "", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def parse_agenda(text: str) -> Agenda:
    """次第の抽出テキストから骨格を取り出す。"""
    agenda = Agenda()
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]

    current_topic: AgendaTopic | None = None
    current_item: AgendaItem | None = None
    seen_agenda_heading = False

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        next_match = NEXT_PATTERN.search(line)
        if next_match and not agenda.next_meeting:
            agenda.next_meeting = _tighten(_clean(next_match.group(1)))
            continue

        date_match = DATE_LINE_PATTERN.match(line)
        if date_match:
            label = re.sub(r"\s", "", date_match.group(1))
            value = _clean(date_match.group(2))
            if label == "日時" and not agenda.datetime_text:
                agenda.datetime_text = _tighten(value)
            elif label == "場所" and not agenda.place:
                agenda.place = _tighten(value)
            continue

        if not agenda.title:
            title_match = TITLE_PATTERN.match(line)
            if title_match and "議" in line:
                agenda.title = _tighten(_clean(title_match.group(1)))
                continue

        # 「議 題」の見出しより後を対象にする(あれば)。
        if re.fullmatch(r"議[\s　]*題", line):
            seen_agenda_heading = True
            continue

        item_match = ITEM_PATTERN.match(line)
        if item_match and current_topic is not None:
            branch = item_match.group(1)
            title = _clean(item_match.group(2))
            material = item_match.group(3) or ""
            if not material:
                found = MATERIAL_PATTERN.search(line)
                material = found.group(1) if found else ""
            if title:
                current_item = AgendaItem(
                    branch=branch, title=title, material=material
                )
                current_topic.items.append(current_item)
                continue

        topic_match = TOPIC_PATTERN.match(line)
        if topic_match and (seen_agenda_heading or current_topic is not None
                            or len(line) <= 40):
            number = topic_match.group(1).translate(_ZEN2HAN)
            title = _clean(topic_match.group(2))
            # 資料番号だけの行や日付行を議題と誤認しない。
            if title and not MATERIAL_PATTERN.fullmatch(title):
                current_topic = AgendaTopic(number=number, title=title)
                current_item = None
                agenda.topics.append(current_topic)
                continue

        if _looks_like_owner(line) and current_item is not None:
            if not current_item.owner:
                current_item.owner = re.sub(r"[\s　]+", "　", _clean(line))
                continue

        # 資料番号が単独行にある場合、直前の項目へ結び付ける。
        if current_item is not None and not current_item.material:
            found = MATERIAL_PATTERN.search(line)
            if found and len(line) <= 12:
                current_item.material = found.group(1)

    # 枝番が無い項目には丸数字を振る。
    for topic in agenda.topics:
        for index, item in enumerate(topic.items):
            if not item.branch and index < len(CIRCLED):
                item.branch = CIRCLED[index]
    return agenda


def build_inferred_agenda(titles: list[str], source_head: str = "") -> Agenda:
    """
    推定した議題表題から骨格を組み立てる(次第なし議事録のフェーズ1)。

    項目は議題ごとに1つの無題項目(枝番なし)。担当・資料は空のままにし、
    描画時に赤の空欄になる。日時・場所は文字起こしのヘッダから拾えれば使う。
    """
    meta = parse_agenda(source_head) if source_head else Agenda()
    agenda = Agenda(inferred=True)
    agenda.title = meta.title
    agenda.datetime_text = meta.datetime_text
    agenda.place = meta.place

    merged: list[str] = []
    for raw in titles:
        title = raw.strip()
        if not title:
            continue
        key = _dedup_key(title)
        duplicate = False
        for previous in merged:
            prev_key = _dedup_key(previous)
            if key == prev_key or (
                len(key) >= 6 and (key in prev_key or prev_key in key)
            ):
                duplicate = True
                break
        if not duplicate:
            merged.append(title)

    for index, title in enumerate(merged, start=1):
        agenda.topics.append(
            AgendaTopic(number=str(index), title=title, items=[AgendaItem()])
        )
    if not agenda.topics:
        agenda.topics.append(
            AgendaTopic(number="1", title="会議内容", items=[AgendaItem()])
        )
    return agenda
