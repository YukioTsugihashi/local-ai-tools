"""
議事録テンプレート(.docx)へ構造化データを流し込むレンダラ。

書式はテンプレートの行XMLを複製して引き継ぐ。フォント・罫線・網掛け・
インデントを一切コード側で再現しないため、原本と完全に一致する。
"""

from __future__ import annotations

import copy
import io
import re
from typing import Any

from docx import Document
from docx.oxml.ns import qn
from docx.enum.text import WD_COLOR_INDEX
from docx.shared import RGBColor
from docx.text.run import Run


# テンプレート本文表の行番号。行の役割ごとにプロトタイプとして使う。
PROTO_HEAD_NEW_TOPIC = 0  # ＜議題N＞行＋項目行(左2段落) / 担当
PROTO_BODY = 1  # ・説明＋質疑応答(横結合)
PROTO_HEAD_ITEM = 2  # 項目行のみ(左1段落) / 担当
PROTO_OTHER_HEAD = 18  # ＜その他＞(横結合・網掛け)
PROTO_OTHER_BODY = 19  # その他の本文(横結合)
PROTO_NEXT = 20  # ＜次回の開催予定＞(横結合・網掛け・2段落)

# タイトルの既定値。空にすると段落ごと消えてフォントが失われるため、
# 仮の文字列を置き、赤マーカーで書き換え対象であることを示す。
# 次第や文字起こしのヘッダから会議名が取れた場合はそれで上書きされる。
DEFAULT_TITLE = "タイトル"


# 値の出所をマーカーで示す。校正時にどこを確認すべきか一目で分かるようにする。
# 文字色ではなく蛍光ペンにすることで、印刷・画面のどちらでも見落としにくい。
#   green  : 文字起こしから特定できた
#   yellow : 文字起こしでは不明だが次第から補った
#   red    : どちらからも特定できず空欄
MARK_HIGHLIGHTS = {
    "green": WD_COLOR_INDEX.BRIGHT_GREEN,
    "yellow": WD_COLOR_INDEX.YELLOW,
    "red": WD_COLOR_INDEX.RED,
}


def _apply_mark(run, mark: str) -> None:
    highlight = MARK_HIGHLIGHTS.get(mark)
    if highlight is not None:
        run.font.highlight_color = highlight


def _set_paragraph_text(paragraph, text, mark: str = "") -> None:
    """
    段落の書式(インデント・フォント)を保ったまま本文だけ差し替える。

    text には文字列のほか、[{"text":..., "mark":...}, ...] を渡せる。
    1つの文の中で出所ごとに色を変えるために使う。
    """
    runs = paragraph.runs
    template_run = runs[0] if runs else None

    segments = (
        text if isinstance(text, list) else [{"text": text, "mark": mark}]
    )

    if template_run is None:
        for segment in segments:
            _apply_mark(paragraph.add_run(segment.get("text", "")), segment.get("mark", ""))
        return

    # 1つ目のランを雛形として使い回し、残りはその複製にする。
    for extra in runs[1:]:
        extra._element.getparent().remove(extra._element)

    # 色を付ける前の状態を複製元として確保する。
    # 付けた後に複製すると、色が後続のランへ伝播してしまう。
    pristine = copy.deepcopy(template_run._element)

    first = segments[0]
    template_run.text = first.get("text", "")
    _apply_mark(template_run, first.get("mark", ""))

    anchor_element = template_run._element
    for segment in segments[1:]:
        new_element = copy.deepcopy(pristine)
        anchor_element.addnext(new_element)
        run = Run(new_element, paragraph)
        run.text = segment.get("text", "")
        _apply_mark(run, segment.get("mark", ""))
        anchor_element = new_element


def _fill_cell(cell, items: list) -> None:
    """
    セルへ複数段落を書き込む。

    items は文字列、または (雛形段落のXML, 文字列) のタプル。
    本文セルは「・説明」「＜質疑応答・意見等＞」「発言」で
    インデントが異なるため、行ごとに雛形を指定できるようにしている。
    """
    items = items or [""]
    normalized = []
    for entry in items:
        if isinstance(entry, tuple):
            proto, text = entry[0], entry[1]
            mark = entry[2] if len(entry) > 2 else ""
        else:
            proto, text, mark = None, entry, ""
        normalized.append((proto, text, mark))

    default_proto = copy.deepcopy(cell.paragraphs[0]._element)

    # 既存段落を全部消してから、雛形を複製して積み直す。
    tc = cell._tc
    for paragraph in list(cell.paragraphs):
        tc.remove(paragraph._element)

    for proto, text, mark in normalized:
        new_p = copy.deepcopy(proto if proto is not None else default_proto)
        tc.append(new_p)
        _set_paragraph_text(cell.paragraphs[-1], text, mark=mark)


def _row_cells(row) -> list:
    """横結合を1つにまとめたセル列を返す。"""
    unique = []
    for cell in row.cells:
        if not unique or cell._tc is not unique[-1]._tc:
            unique.append(cell)
    return unique


def _set_header_value(paragraph, label: str, value: str) -> None:
    """
    「日時：〇〇」のヘッダ行で、ラベル側の書式を壊さずに値だけ差し替える。

    ラベルは均等割り付け(fitText)が掛かっていることがあり、そこへ行全体を
    書き込むと行ごと圧縮されて潰れる。コロンまでのランはそのまま残す。
    """
    runs = paragraph.runs
    if not runs:
        paragraph.add_run(f"{label}：{value}")
        return

    colon_index = None
    for index, run in enumerate(runs):
        if "：" in run.text or ":" in run.text:
            colon_index = index
            break

    if colon_index is None:
        # コロンが無い(想定外)ときだけ従来どおり全体を書き換える。
        _set_paragraph_text(paragraph, f"{label}：{value}")
        return

    # コロンのランは、コロンまでを残す(値が同じランに入っている場合に備える)。
    head = runs[colon_index]
    text = head.text
    cut = text.find("：")
    if cut < 0:
        cut = text.find(":")
    head.text = text[: cut + 1]

    value_runs = runs[colon_index + 1 :]
    template = copy.deepcopy(value_runs[0]._element) if value_runs else None
    for run in value_runs:
        run._element.getparent().remove(run._element)

    if not value:
        return

    if template is not None:
        new_element = copy.deepcopy(template)
        head._element.addnext(new_element)
        from docx.text.run import Run

        Run(new_element, paragraph).text = value
    else:
        paragraph.add_run(value)


def _blank_header(document: Document, keep: dict[str, str] | None = None) -> None:
    """
    ヘッダの名簿・日時・場所・タイトルの可変部分を空にする。

    keep に値があればそれを入れる。指定が無い項目はラベルだけ残す。
    """
    keep = keep or {}
    labels = ("日時", "場所", "出席者", "陪席者", "欠席者", "事務局", "書記")
    # 複数行に折り返すのは名簿系だけ。書記や日時の次行を巻き込まないよう限定する。
    roster_labels = ("出席者", "陪席者", "欠席者", "事務局")
    in_roster = False

    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            in_roster = False
            continue

        # タイトル行が名簿の折り返しと誤判定されないよう、先に処理する。
        if text.endswith("議事録"):
            title_value = (keep.get("タイトル") or "").strip()
            if title_value:
                _set_paragraph_text(paragraph, title_value)
            else:
                # 会議名が不明な場合は仮タイトルを赤マーカーで置く。
                _set_paragraph_text(paragraph, DEFAULT_TITLE, mark="red")
            in_roster = False
            continue

        matched = None
        for label in labels:
            if text.startswith(label + "：") or text.startswith(label + ":"):
                matched = label
                break

        if matched:
            _set_header_value(paragraph, matched, keep.get(matched, ""))
            in_roster = matched in roster_labels
            continue

        # 名簿の折り返し行(ラベルなしの続き)だけを消す。
        if in_roster and "：" not in text and ":" not in text:
            _set_paragraph_text(paragraph, "")
            continue

        in_roster = False


_ZEN2HAN_DIGIT = str.maketrans("０１２３４５６７８９", "0123456789")


def _to_halfwidth_number(text: str) -> str:
    """
    議題見出しの数字は半角へ揃える。

    次第がPDF由来だと資料番号が全角(資料１)で入ってくることがあり、
    見出し内で半角と全角が混ざるため、ここで統一する。
    """
    return (text or "").translate(_ZEN2HAN_DIGIT)


def _tighten_spaces(text: str) -> str:
    """PDF抽出で数字の前後に入る空白を詰める(令和 8 年 → 令和8年)。"""
    text = re.sub(r"(?<=[0-9])\s+(?=[年月日時分回])", "", text)
    text = re.sub(r"(?<=[年月日])\s+(?=[0-9])", "", text)
    text = re.sub(r"(?<=[令和平成昭和第])\s+(?=[0-9])", "", text)
    return text


def _item_heading(item: dict[str, Any]) -> str:
    """「① 経営指標（速報）（資料１）」の形へ整える。"""
    branch = (item.get("枝番") or "").strip()
    title = _tighten_spaces(_to_halfwidth_number((item.get("表題") or "").strip()))
    material = _to_halfwidth_number((item.get("資料") or "").strip())
    head = f"{branch} {title}".strip()
    if material:
        head = f"{head}（{material}）"
    return head


def _speech_segments(remark) -> list:
    """
    発言を「名前：内容」のセグメント列にする。

    名前が特定できた場合は緑マーカー。不明な場合は括弧を使わず、
    半角スペース2つに赤マーカーを塗って手入力の目印にする。
    """
    speaker = (remark.get("発言者") or "").strip()
    content = (remark.get("内容") or "").strip()
    if speaker:
        return [
            {"text": speaker, "mark": "green"},
            {"text": "：" + content, "mark": ""},
        ]
    return [
        {"text": "  ", "mark": "red"},
        {"text": "：" + content, "mark": ""},
    ]


def _body_texts(item: dict[str, Any], protos: dict) -> list:
    """本文セルの中身を(雛形, 文字列)の並びで返す。"""
    lines: list = []
    explanation = item.get("説明")
    if explanation:
        if isinstance(explanation, list):
            segments = [{"text": "・", "mark": ""}]
            for seg in explanation:
                segments.append(
                    {
                        "text": _to_halfwidth_number(seg.get("text", "")),
                        "mark": seg.get("mark", ""),
                    }
                )
        else:
            text = explanation if explanation.startswith("・") else f"・{explanation}"
            segments = text
        lines.append((protos["explain"], segments))

    remarks = item.get("質疑応答") or []
    if remarks:
        lines.append((protos["remarks_head"], "＜質疑応答・意見等＞"))
        for remark in remarks:
            lines.append((protos["speech"], _speech_segments(remark)))
    return lines or [(protos["explain"], "")]


def render(template_bytes: bytes, data: dict[str, Any]) -> bytes:
    """構造化データからテンプレート書式の議事録docxを生成する。"""
    document = Document(io.BytesIO(template_bytes))
    _blank_header(document, data.get("ヘッダ"))

    table = document.tables[1]
    rows = table.rows

    prototypes = {
        "head_new": copy.deepcopy(rows[PROTO_HEAD_NEW_TOPIC]._tr),
        "head_item": copy.deepcopy(rows[PROTO_HEAD_ITEM]._tr),
        "body": copy.deepcopy(rows[PROTO_BODY]._tr),
        "other_head": copy.deepcopy(rows[PROTO_OTHER_HEAD]._tr),
        "other_body": copy.deepcopy(rows[PROTO_OTHER_BODY]._tr),
        "next": copy.deepcopy(rows[PROTO_NEXT]._tr),
    }

    # 本文セル内の段落は役割ごとに書式が違うため、個別に雛形を控える。
    body_paragraphs = rows[PROTO_BODY].cells[0].paragraphs
    protos = {
        "explain": copy.deepcopy(body_paragraphs[0]._element),
        "remarks_head": copy.deepcopy(
            body_paragraphs[min(1, len(body_paragraphs) - 1)]._element
        ),
        "speech": copy.deepcopy(
            body_paragraphs[min(2, len(body_paragraphs) - 1)]._element
        ),
    }

    tbl = table._tbl
    for tr in tbl.tr_lst:
        tbl.remove(tr)

    def append(kind: str):
        tr = copy.deepcopy(prototypes[kind])
        tbl.append(tr)
        return table.rows[-1]

    for topic in data.get("議題") or []:
        number = (topic.get("番号") or "").strip()
        topic_title = (topic.get("表題") or "").strip()
        for index, item in enumerate(topic.get("項目") or []):
            first = index == 0
            row = append("head_new" if first else "head_item")
            cells = _row_cells(row)
            heading = _item_heading(item)
            # 推定した議題の見出しは黄マーカーで示す。
            topic_mark = "yellow" if topic.get("推定") else ""
            if first:
                topic_line = [
                    {
                        "text": f"＜議題{_to_halfwidth_number(number)}＞　",
                        "mark": "",
                    },
                    {"text": topic_title, "mark": topic_mark},
                ]
                head_lines: list = [topic_line]
                if heading.strip():
                    head_lines.append(f" {heading}")
                _fill_cell(cells[0], head_lines)
            else:
                _fill_cell(cells[0], [heading])
            if len(cells) > 1:
                _fill_cell(cells[1], [(item.get("担当") or "").strip()])

            body = append("body")
            _fill_cell(_row_cells(body)[0], _body_texts(item, protos))

    others = data.get("その他") or []
    if others:
        append("other_head")
        _fill_cell(
            _row_cells(append("other_body"))[0],
            [_speech_segments(remark) for remark in others],
        )

    next_meeting = (data.get("次回開催") or "").strip()
    if next_meeting:
        row = append("next")
        _fill_cell(_row_cells(row)[0], ["＜次回の開催予定＞", next_meeting])

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
