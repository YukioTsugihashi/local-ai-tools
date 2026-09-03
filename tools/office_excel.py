"""
Excelファイル(.xlsx / .xlsm)を編集するツール。

使い方(リポジトリ直下で。対象ファイルは input/ に置いてもらう):
  シート一覧:   ...python.exe tools\\office_excel.py list "input\\帳票.xlsm"
  セルの表示:   ...python.exe tools\\office_excel.py show "input\\帳票.xlsm" --sheet 集計 --range A1:D10
  セルの変更:   ...python.exe tools\\office_excel.py set "input\\帳票.xlsm" --sheet 集計 --cell B2 --value 123
  ファイル内容: ...python.exe tools\\office_excel.py set "input\\帳票.xlsm" --sheet 集計 --cell B2 --value-file "output\\コメント.txt"
  複数セル:     ...python.exe tools\\office_excel.py set-batch "input\\帳票.xlsm" --updates-json '[{"sheet":"集計","cell":"B2","value":"本文"}]'
  複数セル(長文): ...python.exe tools\\office_excel.py set-batch "input\\帳票.xlsm" --updates-file "work\\updates.json"

(...python.exe = text-processing-bridge\\.venv\\Scripts\\python.exe)

守っている約束:
- input/ のファイルは絶対に上書きしない。変更結果は output/ へ
  「元名_updated.xlsx(.xlsm)」として保存する
- .xlsx/.xlsm内部の対象セルXMLだけを変更し、数式キャッシュ・マクロ・書式を保持する

新しい書込操作を追加するときも、ブック全体をopenpyxlで再保存しない。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from html import escape
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
from xml.etree import ElementTree

from openpyxl import load_workbook

# PowerShell経由(既定cp932)だと日本語出力が化けるため、常にUTF-8で出す
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

def _load(path: Path):
    """マクロ付き(.xlsm)はマクロを保持したまま開く。"""
    return load_workbook(path, keep_vba=path.suffix.lower() == ".xlsm")


def _output_path(source: Path) -> Path:
    """入力ファイルを守るための別名出力先を返す。"""
    output_dir = Path("output")
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / f"{source.stem}_updated{source.suffix}"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _sheet_part(archive: ZipFile, sheet_name: str) -> str:
    """シート名に対応するxl/worksheets/*.xmlのパスを解決する。"""
    workbook_root = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    relationship_id = None
    for element in workbook_root.iter():
        if _local_name(element.tag) == "sheet" and element.attrib.get("name") == sheet_name:
            relationship_id = next(
                (value for key, value in element.attrib.items() if _local_name(key) == "id"),
                None,
            )
            break
    if relationship_id is None:
        raise RuntimeError(f"シートXMLが見つかりません: {sheet_name}")

    relationships = ElementTree.fromstring(
        archive.read("xl/_rels/workbook.xml.rels")
    )
    target = None
    for element in relationships:
        if element.attrib.get("Id") == relationship_id:
            target = element.attrib.get("Target")
            break
    if not target:
        raise RuntimeError(f"シートの関連付けが見つかりません: {sheet_name}")
    if target.startswith("/"):
        return target.lstrip("/")
    return str(Path("xl", target).as_posix())


def _column_number(coordinate: str) -> int:
    letters = re.match(r"[A-Z]+", coordinate.upper())
    if letters is None:
        raise ValueError(f"セル座標が不正です: {coordinate}")
    result = 0
    for character in letters.group(0):
        result = result * 26 + ord(character) - 64
    return result


def _cell_xml(
    coordinate: str,
    value: str | int | float,
    opening: str | None = None,
    prefix: str = "",
    style_index: int | None = None,
    style_spec: dict | None = None,
) -> str:
    """既存属性を保ったセルXMLを組み立てる。"""
    if opening is None:
        opening = f'<{prefix}c r="{coordinate}">'
    tag = re.match(r"<([^\s>/]+)", opening).group(1)
    prefix = tag.removesuffix("c")
    opening = re.sub(r"\s+t=(?:\"[^\"]*\"|'[^']*')", "", opening)
    opening = opening.rstrip().removesuffix("/").rstrip().removesuffix(">").rstrip()
    if style_index is not None:
        # 書式テンプレートの移植先。styles.xmlへ追記した書式番号へ差し替える
        if re.search(r'\bs="\d+"', opening):
            opening = re.sub(r'\bs="\d+"', f's="{style_index}"', opening, count=1)
        else:
            opening += f' s="{style_index}"'
    if isinstance(value, (int, float)):
        return f"{opening}><{prefix}v>{value}</{prefix}v></{tag}>"
    if style_spec and style_spec.get("markers"):
        # ひな型で見出しだけ太字にしてある場合、その部分書式を出力にも移す
        body = _rich_text_body(value, style_spec, prefix)
        return f'{opening} t="inlineStr"><{prefix}is>{body}</{prefix}is></{tag}>'
    text = escape(value, quote=False)
    return (
        f'{opening} t="inlineStr"><{prefix}is><{prefix}t xml:space="preserve">'
        f"{text}</{prefix}t></{prefix}is></{tag}>"
    )


def _replace_cell_xml(
    sheet_xml: bytes,
    coordinate: str,
    value: str | int | float,
    style_index: int | None = None,
    style_spec: dict | None = None,
) -> bytes:
    """ワークシートXML内の対象セルだけを書き換える。"""
    text = sheet_xml.decode("utf-8")
    escaped_coordinate = re.escape(coordinate.upper())
    cell_pattern = re.compile(
        rf'(?P<opening><(?P<tag>(?:[A-Za-z_][\w.-]*:)?c)\b'
        rf'(?=[^>]*\br="{escaped_coordinate}")[^>]*?)'
        rf'(?:\s*/>|>(?P<body>.*?)</(?P=tag)>)',
        re.DOTALL,
    )
    existing = cell_pattern.search(text)
    if existing:
        replacement = _cell_xml(
            coordinate.upper(),
            value,
            existing.group("opening"),
            style_index=style_index,
            style_spec=style_spec,
        )
        return (text[: existing.start()] + replacement + text[existing.end() :]).encode("utf-8")

    row_number = int(re.search(r"\d+", coordinate).group(0))
    row_pattern = re.compile(
        rf'(?P<opening><(?P<tag>(?:[A-Za-z_][\w.-]*:)?row)\b'
        rf'(?=[^>]*\br="{row_number}")[^>]*>)'
        rf'(?P<body>.*?)</(?P=tag)>',
        re.DOTALL,
    )
    row = row_pattern.search(text)
    if row:
        row_prefix = row.group("tag").removesuffix("row")
        new_cell = _cell_xml(
            coordinate.upper(),
            value,
            prefix=row_prefix,
            style_index=style_index,
            style_spec=style_spec,
        )
        body = row.group("body")
        new_column = _column_number(coordinate)
        insertion = len(body)
        for candidate in re.finditer(
            r'<(?:[A-Za-z_][\w.-]*:)?c\b[^>]*\br="([A-Z]+\d+)"[^>]*',
            body,
        ):
            if _column_number(candidate.group(1)) > new_column:
                insertion = candidate.start()
                break
        new_body = body[:insertion] + new_cell + body[insertion:]
        replacement = f'{row.group("opening")}{new_body}</{row.group("tag")}>'
        return (text[: row.start()] + replacement + text[row.end() :]).encode("utf-8")

    sheet_data = re.search(
        r"<(?P<tag>(?:[A-Za-z_][\w.-]*:)?sheetData)\b[^>]*>"
        r"(?P<body>.*?)</(?P=tag)>",
        text,
        re.DOTALL,
    )
    if sheet_data is None:
        raise RuntimeError("sheetDataが見つかりません")
    sheet_prefix = sheet_data.group("tag").removesuffix("sheetData")
    new_cell = _cell_xml(
        coordinate.upper(),
        value,
        prefix=sheet_prefix,
        style_index=style_index,
        style_spec=style_spec,
    )
    new_row = f'<{sheet_prefix}row r="{row_number}">{new_cell}</{sheet_prefix}row>'
    body = sheet_data.group("body")
    insertion = len(body)
    for candidate in re.finditer(
        r'<(?:[A-Za-z_][\w.-]*:)?row\b[^>]*\br="(\d+)"[^>]*',
        body,
    ):
        if int(candidate.group(1)) > row_number:
            insertion = candidate.start()
            break
    new_body = body[:insertion] + new_row + body[insertion:]
    start, end = sheet_data.span("body")
    return (text[:start] + new_body + text[end:]).encode("utf-8")


# ---------------------------------------------------------------------------
# 書式テンプレートの移植。
# 利用者が templates/gui のひな型Excelで整えたフォント・サイズ・色・太さ・配置・
# 罫線・塗りつぶしを、書き込み先セルへ適用する。
# 罫線と塗りつぶしは「ひな型で指定されているときだけ」移す。指定が無ければ
# 書き込み先のものを保つ(ひな型で何も引いていない既存の使い方で、分析表の枠が
# 消えないようにするため)。
# 列幅・行高はセルの書式ではなく列・行の属性なので移さない。移すとコメント欄
# だけでなく、同じ列・行に並ぶ分析表のセルまで動いてしまう。
# ---------------------------------------------------------------------------

# 書式を読み取る名前付きセル。ひな型Excel側でこの名前を付けておく
STYLE_TEMPLATE_CELL_NAME = "comment_template"


def _color_xml(color, tag: str = "color") -> str:
    """
    openpyxlの色を、styles.xmlへ書ける要素にする。指定が無ければ空文字。

    フォント・罫線・塗りつぶしで書き方が同じなので、ここに1つだけ置く。
    """
    if color is None:
        return ""
    kind = getattr(color, "type", None)
    if kind == "rgb" and isinstance(color.rgb, str):
        return f'<{tag} rgb="{color.rgb}"/>'
    if kind == "theme":
        tint = f' tint="{color.tint}"' if color.tint else ""
        return f'<{tag} theme="{color.theme}"{tint}/>'
    if kind == "indexed":
        return f'<{tag} indexed="{color.indexed}"/>'
    return ""


# OOXMLの<border>は子要素の順序が決まっている。入れ替えるとExcelが開けなくなる
BORDER_SIDES = ("left", "right", "top", "bottom", "diagonal")


def _border_xml(border) -> str:
    """
    ひな型セルの罫線を<border>要素にする。1本も引かれていなければ空文字。

    空文字のときは書き込み先の罫線をそのまま残す。ひな型に罫線を引いていない
    既存の使い方で、分析表の枠が消えないようにするため。
    """
    if border is None:
        return ""
    sides = [getattr(border, name, None) for name in BORDER_SIDES]
    if not any(side is not None and side.style for side in sides):
        return ""
    parts: list[str] = []
    for name, side in zip(BORDER_SIDES, sides):
        if side is None or not side.style:
            parts.append(f"<{name}/>")
            continue
        parts.append(f'<{name} style="{side.style}">{_color_xml(side.color)}</{name}>')
    attributes = ""
    if getattr(border, "diagonalUp", None):
        attributes += ' diagonalUp="1"'
    if getattr(border, "diagonalDown", None):
        attributes += ' diagonalDown="1"'
    return f"<border{attributes}>" + "".join(parts) + "</border>"


def _fill_xml(fill) -> str:
    """
    ひな型セルの塗りつぶしを<fill>要素にする。塗っていなければ空文字。

    空文字のときは書き込み先の塗りをそのまま残す(罫線と同じ理由)。
    """
    pattern = getattr(fill, "patternType", None)
    if not pattern or pattern == "none":
        return ""
    foreground = _color_xml(getattr(fill, "fgColor", None), "fgColor")
    background = _color_xml(getattr(fill, "bgColor", None), "bgColor")
    return f'<fill><patternFill patternType="{pattern}">{foreground}{background}</patternFill></fill>'


def _font_xml(font) -> str:
    """openpyxlのFontを、styles.xmlへ追記できる<font>要素にする。"""
    parts: list[str] = []
    if font.bold:
        parts.append("<b/>")
    if font.italic:
        parts.append("<i/>")
    if font.underline and font.underline != "none":
        parts.append("<u/>" if font.underline == "single" else f'<u val="{font.underline}"/>')
    if font.strike:
        parts.append("<strike/>")
    if font.size:
        parts.append(f'<sz val="{float(font.size):g}"/>')
    parts.append(_color_xml(font.color))
    if font.name:
        parts.append(f'<name val="{escape(font.name)}"/>')
    if font.family is not None:
        parts.append(f'<family val="{int(font.family)}"/>')
    if font.charset is not None:
        parts.append(f'<charset val="{int(font.charset)}"/>')
    # scheme(テーマ既定)は付けない。付けるとテーマのフォントが優先され、
    # ひな型で選んだフォント名が無視されるため
    return "<font>" + "".join(parts) + "</font>"


def _alignment_xml(alignment) -> str:
    """openpyxlのAlignmentを<alignment>要素にする。指定が無ければ空文字。"""
    attributes: list[str] = []
    if alignment.horizontal:
        attributes.append(f'horizontal="{alignment.horizontal}"')
    if alignment.vertical:
        attributes.append(f'vertical="{alignment.vertical}"')
    if alignment.wrap_text:
        attributes.append('wrapText="1"')
    if alignment.shrink_to_fit:
        attributes.append('shrinkToFit="1"')
    if alignment.indent:
        attributes.append(f'indent="{int(alignment.indent)}"')
    return f"<alignment {' '.join(attributes)}/>" if attributes else ""


def _run_properties_xml(font) -> str:
    """
    セル内の一部だけに掛ける書式(<rPr>)を組み立てる。

    <font>と中身はほぼ同じだが、フォント名の要素だけ <rFont> になる。
    openpyxlのFont(セル全体)とInlineFont(セル内の一部)の両方を受ける。
    """
    def pick(*names):
        for name in names:
            value = getattr(font, name, None)
            if value is not None:
                return value
        return None

    parts: list[str] = []
    if pick("b", "bold"):
        parts.append("<b/>")
    if pick("i", "italic"):
        parts.append("<i/>")
    underline = pick("u", "underline")
    if underline and underline != "none":
        parts.append("<u/>" if underline == "single" else f'<u val="{underline}"/>')
    if pick("strike"):
        parts.append("<strike/>")
    size = pick("sz", "size")
    if size:
        parts.append(f'<sz val="{float(size):g}"/>')
    color = pick("color")
    if color is not None:
        if getattr(color, "type", None) == "rgb" and isinstance(color.rgb, str):
            parts.append(f'<color rgb="{color.rgb}"/>')
        elif getattr(color, "theme", None) is not None:
            tint = f' tint="{color.tint}"' if getattr(color, "tint", None) else ""
            parts.append(f'<color theme="{color.theme}"{tint}/>')
        elif getattr(color, "indexed", None) is not None:
            parts.append(f'<color indexed="{color.indexed}"/>')
    name = pick("rFont", "name")
    if name:
        parts.append(f'<rFont val="{escape(str(name))}"/>')
    family = pick("family")
    if family is not None:
        parts.append(f'<family val="{int(family)}"/>')
    charset = pick("charset")
    if charset is not None:
        parts.append(f'<charset val="{int(charset)}"/>')
    return "<rPr>" + "".join(parts) + "</rPr>"


def _template_runs(cell_value) -> list[tuple[str, object | None]]:
    """ひな型セルを(文字列, 書式)の並びにする。書式なしの部分はNone。"""
    if type(cell_value).__name__ != "CellRichText":
        return [(str(cell_value or ""), None)]
    runs: list[tuple[str, object | None]] = []
    for block in cell_value:
        if hasattr(block, "font"):
            runs.append((str(block), block.font))
        else:
            runs.append((str(block), None))
    return runs


def _style_template_spec(path: Path) -> dict:
    """
    ひな型Excelの名前付きセルから、移植する書式を読む。

    セル全体の書式に加えて、**セル内の部分書式**(見出しだけ太字など)も拾う。
    利用者は《入院》《外来》《まとめ》を太字にして使っており、
    セル全体の書式しか見ないと、その太字が丸ごと落ちる(2026-08-17に判明)。
    部分書式は「その文字列そのもの」を目印にして出力側へ対応付ける。
    """
    if not path.exists():
        raise RuntimeError(f"書式テンプレートが見つかりません: {path}")
    workbook = load_workbook(path, rich_text=True)
    try:
        defined = workbook.defined_names.get(STYLE_TEMPLATE_CELL_NAME)
        if defined is None:
            raise RuntimeError(
                f"{STYLE_TEMPLATE_CELL_NAME} という名前のセルがありません。"
                "数式タブの「名前の管理」でコメント欄のセルへ付けてください"
            )
        destinations = list(defined.destinations)
        if not destinations:
            raise RuntimeError(f"{STYLE_TEMPLATE_CELL_NAME} の参照先を解決できません")
        sheet_name, reference = destinations[0]
        coordinate = reference.replace("$", "").split(":")[0]
        cell = workbook[sheet_name][coordinate]

        runs = _template_runs(cell.value)
        # 一番長い断片を「本文の書式」とみなし、それ以外を目印として扱う
        body = max(runs, key=lambda item: len(item[0]), default=("", None))
        base_font = body[1] if body[1] is not None else cell.font
        markers: list[tuple[str, str]] = []
        for text, font in runs:
            marker = text.strip()
            if not marker or font is None or font is body[1]:
                continue
            properties = _run_properties_xml(font)
            if properties != _run_properties_xml(base_font):
                markers.append((marker, properties))
        return {
            "font": _font_xml(cell.font),
            "alignment": _alignment_xml(cell.alignment),
            "border": _border_xml(cell.border),
            "fill": _fill_xml(cell.fill),
            "base_run": _run_properties_xml(base_font),
            "markers": markers,
        }
    finally:
        workbook.close()


# ひな型の既定位置。--style-template の指定が無くてもここにあれば使う。
# Kilo経由の書込み(excel_write)からも効かせるための既定値
DEFAULT_STYLE_TEMPLATE = (
    Path(__file__).resolve().parents[1] / "templates" / "gui" / "excel_comment_gui_template.xlsx"
)

# 生成コメントだけが持つ構造。任意のセル書込みへコメント書式を巻き込まないための目印
COMMENT_MARKERS = ("《入院》", "《外来》", "《まとめ》")


def resolve_style_template(style_template: str | None) -> dict | None:
    """
    書式テンプレートを読む。読めなくても書込み自体は止めない(理由は必ず出す)。

    指定が無ければ既定の場所を見る。指定も既定も無ければ書式は変えない。
    """
    path = Path(style_template) if style_template else DEFAULT_STYLE_TEMPLATE
    if not path.exists():
        if style_template:
            print(f"書式テンプレートを適用できません: 見つかりません: {path}")
        return None
    try:
        spec = _style_template_spec(path)
    except (RuntimeError, OSError, KeyError, ValueError) as error:
        print(f"書式テンプレートを適用できません: {error}(書式は変更せず本文だけ書き込みます)")
        return None
    detail = "フォント・サイズ・色・太さ・配置"
    # 罫線・塗りは指定があるときだけ移すので、当たったかどうかを必ず出す
    # (ひな型で引いたのに反映されない、を黙って起こさないため)
    if spec.get("border"):
        detail += "・罫線"
    if spec.get("fill"):
        detail += "・塗りつぶし"
    if spec.get("markers"):
        detail += f" / 部分書式{len(spec['markers'])}件({'・'.join(t for t, _ in spec['markers'][:3])})"
    print(f"書式テンプレートを適用します: {path.name}({detail})")
    return spec


def looks_like_comment(value: object) -> bool:
    """書式テンプレートを当ててよい値か(生成コメントか)を判定する。"""
    return isinstance(value, str) and any(mark in value for mark in COMMENT_MARKERS)


def _rich_text_body(value: str, spec: dict, prefix: str = "") -> str:
    """
    ひな型の部分書式を当てた <is> 本文を組み立てる。

    ひな型で「《入院》だけ太字」なら、出力の《入院》も太字になる。
    目印が無いひな型(セル全体が1書式)なら、単純な1断片として返す。
    """
    markers = spec.get("markers") or []
    base = spec.get("base_run") or ""
    segments: list[tuple[str, str]] = []
    if markers:
        pattern = re.compile("|".join(re.escape(text) for text, _ in sorted(
            markers, key=lambda item: len(item[0]), reverse=True
        )))
        properties = {text: prop for text, prop in markers}
        position = 0
        for match in pattern.finditer(value):
            if match.start() > position:
                segments.append((value[position:match.start()], base))
            segments.append((match.group(0), properties[match.group(0)]))
            position = match.end()
        if position < len(value):
            segments.append((value[position:], base))
    if not segments:
        segments = [(value, base)]
    return "".join(
        f"<{prefix}r>{prop}<{prefix}t xml:space=\"preserve\">"
        f"{escape(text, quote=False)}</{prefix}t></{prefix}r>"
        for text, prop in segments
        if text
    )


def _bump_count(opening: str, delta: int) -> str:
    match = re.search(r'count="(\d+)"', opening)
    if match is None:
        return opening
    return opening[: match.start(1)] + str(int(match.group(1)) + delta) + opening[match.end(1) :]


def _styled_xf(
    base_xf: str,
    font_id: int,
    alignment_xml: str,
    border_id: int | None = None,
    fill_id: int | None = None,
) -> str:
    """
    既存の書式(xf)を複製し、ひな型で指定されたものだけを差し替える。

    border_id / fill_id が None のときは書き込み先のものを残す。ひな型で
    罫線や塗りを引いていない場合に、分析表の見た目を変えないため。
    """
    opening = re.match(r"<xf\b([^>]*?)/?>", base_xf)
    attributes = dict(re.findall(r'([\w:]+)="([^"]*)"', opening.group(1)))
    attributes["fontId"] = str(font_id)
    attributes["applyFont"] = "1"
    if border_id is not None:
        attributes["borderId"] = str(border_id)
        attributes["applyBorder"] = "1"
    if fill_id is not None:
        attributes["fillId"] = str(fill_id)
        attributes["applyFill"] = "1"
    if alignment_xml:
        attributes["applyAlignment"] = "1"
    else:
        # ひな型に配置指定が無ければ、元セルの折返し等をそのまま残す
        base_alignment = re.search(r"<alignment\b[^>]*/>", base_xf)
        alignment_xml = base_alignment.group(0) if base_alignment else ""
    protection = re.search(r"<protection\b[^>]*/>", base_xf)
    children = alignment_xml + (protection.group(0) if protection else "")
    attr_text = " ".join(f'{key}="{value}"' for key, value in attributes.items())
    return f"<xf {attr_text}>{children}</xf>" if children else f"<xf {attr_text}/>"


def _append_to_block(text: str, block: str, element: str, xml: str) -> tuple[str, int]:
    """
    styles.xmlの<fonts>/<borders>/<fills>へ1件だけ足し、その番号を返す。

    既存の定義には触らない(他のセルの見た目を巻き込まないため)。
    """
    found = re.search(rf"(<{block}\b[^>]*>)(.*?)(</{block}>)", text, re.DOTALL)
    if found is None:
        raise RuntimeError(f"styles.xmlに{block}の定義が見つかりません")
    new_id = len(re.findall(rf"<{element}\b", found.group(2)))
    updated = (
        text[: found.start()]
        + _bump_count(found.group(1), 1)
        + found.group(2)
        + xml
        + f"</{block}>"
        + text[found.end() :]
    )
    return updated, new_id


def _apply_style_template(
    styles_xml: bytes,
    base_styles: list[int],
    spec: dict,
) -> tuple[bytes, dict[int, int]]:
    """
    styles.xmlへひな型の書式を追記し、元の書式番号→新しい番号の対応を返す。

    既存の定義は一切変更しない(他のセルの見た目を巻き込まないため)。
    """
    text = styles_xml.decode("utf-8")
    if re.search(r"<\w+:styleSheet\b", text):
        raise RuntimeError("対応していないstyles.xml形式です(名前空間接頭辞付き)")

    text, new_font_id = _append_to_block(text, "fonts", "font", spec["font"])
    # 罫線と塗りは、ひな型で指定されているときだけ足す。指定が無ければ番号を
    # 渡さず、書き込み先のものをそのまま使わせる(分析表の枠を消さないため)
    border_id: int | None = None
    if spec.get("border"):
        text, border_id = _append_to_block(text, "borders", "border", spec["border"])
    fill_id: int | None = None
    if spec.get("fill"):
        text, fill_id = _append_to_block(text, "fills", "fill", spec["fill"])

    xfs_block = re.search(r"(<cellXfs\b[^>]*>)(.*?)(</cellXfs>)", text, re.DOTALL)
    if xfs_block is None:
        raise RuntimeError("styles.xmlにセル書式(cellXfs)が見つかりません")
    xf_elements = re.findall(r"<xf\b[^>]*?(?:/>|>.*?</xf>)", xfs_block.group(2), re.DOTALL)
    if not xf_elements:
        raise RuntimeError("styles.xmlにセル書式(xf)が1件もありません")
    mapping: dict[int, int] = {}
    additions: list[str] = []
    next_index = len(xf_elements)
    for base in sorted(set(base_styles)):
        base_xf = xf_elements[base] if 0 <= base < len(xf_elements) else xf_elements[0]
        additions.append(
            _styled_xf(base_xf, new_font_id, spec["alignment"], border_id, fill_id)
        )
        mapping[base] = next_index
        next_index += 1
    text = (
        text[: xfs_block.start()]
        + _bump_count(xfs_block.group(1), len(additions))
        + xfs_block.group(2)
        + "".join(additions)
        + "</cellXfs>"
        + text[xfs_block.end() :]
    )
    return text.encode("utf-8"), mapping


def _save_surgical_copy(
    source: Path,
    sheet_name: str,
    coordinate: str,
    value: str | int | float,
) -> Path:
    """ZIP内の対象ワークシートだけを変更して別名保存する。"""
    return _save_surgical_copy_batch(source, [(sheet_name, coordinate, value)])


def _save_surgical_copy_batch(
    source: Path,
    updates: list[tuple[str, str, str | int | float]],
    target: Path | None = None,
    style: dict[str, str] | None = None,
) -> Path:
    """複数シート・複数セルを1回のZIPコピーで変更して別名保存する。"""
    target = target or _output_path(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(source, "r") as source_archive:
        updates_by_part: dict[str, list[tuple[str, str | int | float]]] = {}
        for sheet_name, coordinate, value in updates:
            sheet_part = _sheet_part(source_archive, sheet_name)
            updates_by_part.setdefault(sheet_part, []).append((coordinate, value))

        # 書式テンプレートの移植。失敗しても本文の書込みは止めない。
        # 当てるのは生成コメントだけ。数値や短文への書込みまで
        # コメント用のフォントに変えてしまわないための線引き
        new_styles_xml: bytes | None = None
        style_map: dict[tuple[str, str], int] = {}
        commentish = {
            (part, coordinate)
            for part, cell_updates in updates_by_part.items()
            for coordinate, value in cell_updates
            if looks_like_comment(value)
        }
        if style and commentish:
            try:
                base_styles: dict[tuple[str, str], int] = {}
                for part, cell_updates in updates_by_part.items():
                    sheet_text = source_archive.read(part).decode("utf-8")
                    for coordinate, _ in cell_updates:
                        if (part, coordinate) not in commentish:
                            continue
                        opening = re.search(
                            rf'<(?:[A-Za-z_][\w.-]*:)?c\b(?=[^>]*\br="{re.escape(coordinate.upper())}")[^>]*',
                            sheet_text,
                        )
                        s_attr = (
                            re.search(r'\bs="(\d+)"', opening.group(0)) if opening else None
                        )
                        base_styles[(part, coordinate)] = int(s_attr.group(1)) if s_attr else 0
                new_styles_xml, mapping = _apply_style_template(
                    source_archive.read("xl/styles.xml"),
                    list(base_styles.values()),
                    style,
                )
                style_map = {key: mapping[base] for key, base in base_styles.items()}
            except (KeyError, RuntimeError) as error:
                print(f"書式テンプレートを適用できません: {error}(書式は変更せず本文だけ書き込みます)")
                new_styles_xml = None
                style_map = {}

        with tempfile.NamedTemporaryFile(
            dir=target.parent,
            suffix=source.suffix,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        try:
            with ZipFile(temporary_path, "w", compression=ZIP_DEFLATED) as target_archive:
                target_archive.comment = source_archive.comment
                for info in source_archive.infolist():
                    data = source_archive.read(info.filename)
                    if new_styles_xml is not None and info.filename == "xl/styles.xml":
                        data = new_styles_xml
                    for coordinate, value in updates_by_part.get(info.filename, []):
                        index = style_map.get((info.filename, coordinate))
                        data = _replace_cell_xml(
                            data,
                            coordinate,
                            value,
                            style_index=index,
                            style_spec=style if index is not None else None,
                        )
                    target_archive.writestr(info, data)
            temporary_path.replace(target)
        finally:
            temporary_path.unlink(missing_ok=True)
    return target


def _writable_cell(sheet, coordinate: str):
    """結合範囲内の座標を、値を書ける左上セルへ解決する。"""
    for merged_range in sheet.merged_cells.ranges:
        if coordinate in merged_range:
            return sheet.cell(merged_range.min_row, merged_range.min_col), str(merged_range)
    return sheet[coordinate], None


def _read_value(value: str | None, value_file: str | None) -> str:
    """直接指定またはwork/output内のUTF-8ファイルから設定値を読む。"""
    if value_file is None:
        assert value is not None
        return value

    path = Path(value_file).resolve()
    allowed_roots = (Path("work").resolve(), Path("output").resolve())
    if not any(path.is_relative_to(root) for root in allowed_roots):
        raise SystemExit("--value-file は work/ または output/ 内のファイルだけ指定できます")
    return path.read_text(encoding="utf-8").strip()


def _coerce_value(value: str | int | float) -> str | int | float:
    if not isinstance(value, str):
        return value
    try:
        return float(value) if "." in value else int(value)
    except ValueError:
        return value


def cmd_list(path: Path) -> None:
    workbook = _load(path)
    print(f"ファイル: {path}")
    for i, name in enumerate(workbook.sheetnames, 1):
        sheet = workbook[name]
        print(f"  {i}. {name} ({sheet.max_row} 行 × {sheet.max_column} 列)")


def cmd_show(path: Path, sheet_name: str, cell_range: str) -> None:
    workbook = _load(path)
    if sheet_name not in workbook.sheetnames:
        raise SystemExit(f"シートがありません: {sheet_name}(あるのは {workbook.sheetnames})")
    sheet = workbook[sheet_name]
    for row in sheet[cell_range]:
        print("\t".join("" if c.value is None else str(c.value) for c in row))


def cmd_set(
    path: Path,
    sheet_name: str,
    cell: str,
    value: str | None,
    value_file: str | None,
    style_template: str | None = None,
) -> None:
    workbook = _load(path)
    if sheet_name not in workbook.sheetnames:
        raise SystemExit(f"シートがありません: {sheet_name}(あるのは {workbook.sheetnames})")
    sheet = workbook[sheet_name]

    writable, merged_range = _writable_cell(sheet, cell)
    before = writable.value
    new_value = _read_value(value, value_file)
    # 数値らしい入力は数値として入れる。Excel上で文字列扱いになるのを防ぐ。
    expected = _coerce_value(new_value)

    coordinate = writable.coordinate
    workbook.close()
    target = _save_surgical_copy_batch(
        path,
        [(sheet_name, coordinate, expected)],
        style=resolve_style_template(style_template),
    )

    verification = _load(target)
    verified, _ = _writable_cell(verification[sheet_name], cell)
    if verified.value != expected:
        verification.close()
        raise RuntimeError(f"保存後の検証に失敗しました: {sheet_name}!{coordinate}")
    verification.close()

    if merged_range is not None and writable.coordinate != cell:
        print(f"{sheet_name}!{cell} は結合範囲 {merged_range} 内のため、左上 {writable.coordinate} へ書き込みます")
    print(f"{sheet_name}!{writable.coordinate}: {before!r} → {expected!r}")
    print(f"保存しました(元ファイルは無変更): {target}")
    print(f"再読込検証に成功しました: {sheet_name}!{writable.coordinate}")


def rejected_update_reason(item: object, sheetnames: list[str]) -> str:
    """更新1件を書き込めない理由。書ける場合は空文字を返す。"""
    if not isinstance(item, dict):
        return "sheet・cell・valueを持つオブジェクトではありません"
    sheet_name = item.get("sheet")
    cell = item.get("cell")
    value = item.get("value")
    if not isinstance(sheet_name, str) or sheet_name not in sheetnames:
        return f"シートがありません: {sheet_name}"
    if not isinstance(cell, str) or not re.fullmatch(r"[A-Za-z]+\d+", cell):
        return f"セル座標が不正です: {cell}"
    if not isinstance(value, (str, int, float)):
        return f"設定値が不正です: {sheet_name}!{cell}"
    return ""


def cmd_set_batch(
    path: Path,
    updates_json: str | None = None,
    updates_file: str | None = None,
    style_template: str | None = None,
) -> None:
    """JSONで指定された複数セルを一つの出力ファイルへまとめて書き込む。"""
    if bool(updates_json) == bool(updates_file):
        raise SystemExit("--updates-json または --updates-file のどちらか一方を指定してください")
    try:
        raw_updates = (
            Path(updates_file).read_text(encoding="utf-8")
            if updates_file
            else updates_json
        )
        requested = json.loads(raw_updates or "")
    except (OSError, json.JSONDecodeError) as error:
        option = "--updates-file" if updates_file else "--updates-json"
        raise SystemExit(f"{option} が不正です: {error}") from error
    if not isinstance(requested, list) or not requested:
        raise SystemExit("更新JSONには1件以上の配列を指定してください")

    workbook = _load(path)
    updates: list[tuple[str, str, str | int | float]] = []
    before_values: list[tuple[str, str, object, str | int | float]] = []
    resolved_targets: set[tuple[str, str]] = set()
    # 不正な1件で全部を捨てず、書ける分だけ書いて残りを理由つきで報告する
    rejected: list[str] = []
    try:
        for index, item in enumerate(requested, start=1):
            reason = rejected_update_reason(item, workbook.sheetnames)
            if reason:
                rejected.append(f"{index}件目 {reason}")
                continue
            sheet_name = item["sheet"]
            cell = item["cell"]
            writable, _ = _writable_cell(workbook[sheet_name], cell.upper())
            key = (sheet_name, writable.coordinate)
            if key in resolved_targets:
                rejected.append(
                    f"{index}件目 同じセルが複数回指定されています: "
                    f"{sheet_name}!{writable.coordinate}"
                )
                continue
            resolved_targets.add(key)
            expected = _coerce_value(item["value"])
            updates.append((sheet_name, writable.coordinate, expected))
            before_values.append((sheet_name, writable.coordinate, writable.value, expected))
    finally:
        workbook.close()

    if not updates:
        raise SystemExit("書き込める更新が1件もありません:\n  - " + "\n  - ".join(rejected))

    target = _save_surgical_copy_batch(
        path, updates, style=resolve_style_template(style_template)
    )
    verification = _load(target)
    try:
        unverified = [
            f"{sheet_name}!{coordinate}"
            for sheet_name, coordinate, _, expected in before_values
            if verification[sheet_name][coordinate].value != expected
        ]
    finally:
        verification.close()
    if len(unverified) == len(before_values):
        raise RuntimeError(f"保存後の検証に全件失敗しました: {'、'.join(unverified)}")
    if unverified:
        rejected.extend(f"保存後の検証に失敗: {location}" for location in unverified)

    for sheet_name, coordinate, before, expected in before_values:
        print(f"{sheet_name}!{coordinate}: {before!r} → {expected!r}")
    if rejected:
        print(f"書き込めなかった更新 ({len(rejected)}件):")
        for reason in rejected:
            print(f"  - {reason}")
    print(f"保存しました(元ファイルは無変更): {target}")
    print(f"再読込検証に成功しました: {len(before_values)}セル")


def main() -> None:
    parser = argparse.ArgumentParser(description="Excelの表示・編集(元ファイルは上書きしない)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="シート一覧を表示する")
    p_list.add_argument("path")

    p_show = sub.add_parser("show", help="セル範囲を表示する")
    p_show.add_argument("path")
    p_show.add_argument("--sheet", required=True, help="シート名")
    p_show.add_argument("--range", default="A1:F20", help="表示する範囲(例 A1:D10)")

    p_set = sub.add_parser("set", help="セルの値を変えて別名保存する")
    p_set.add_argument("path")
    p_set.add_argument("--sheet", required=True, help="シート名")
    p_set.add_argument("--cell", required=True, help="対象セル(例 B2)")
    p_set.add_argument(
        "--style-template",
        help="書式の正本にするひな型Excel(既定 templates/gui/excel_comment_gui_template.xlsx)",
    )
    value_group = p_set.add_mutually_exclusive_group(required=True)
    value_group.add_argument("--value", help="設定する値")
    value_group.add_argument(
        "--value-file",
        help="設定する本文を読むUTF-8ファイル(work/またはoutput/内)",
    )

    p_set_batch = sub.add_parser("set-batch", help="複数セルを一つの別名ファイルへまとめて書く")
    p_set_batch.add_argument("path")
    updates_group = p_set_batch.add_mutually_exclusive_group(required=True)
    updates_group.add_argument(
        "--updates-json",
        help='更新配列。例 [{"sheet":"集計","cell":"B2","value":"本文"}]',
    )
    updates_group.add_argument(
        "--updates-file",
        help="更新配列を読むUTF-8 JSONファイル(work/またはoutput/内)",
    )
    p_set_batch.add_argument(
        "--style-template",
        help=(
            "書式の正本にするひな型Excel。comment_template という名前のセルの"
            "フォント・サイズ・色・太さ・配置を書き込み先セルへ適用する"
        ),
    )

    args = parser.parse_args()
    path = Path(args.path)
    if not path.exists():
        raise SystemExit(f"ファイルが見つかりません: {path}")

    if args.command == "list":
        cmd_list(path)
    elif args.command == "show":
        cmd_show(path, args.sheet, args.range)
    elif args.command == "set":
        cmd_set(path, args.sheet, args.cell, args.value, args.value_file, args.style_template)
    elif args.command == "set-batch":
        cmd_set_batch(path, args.updates_json, args.updates_file, args.style_template)


if __name__ == "__main__":
    main()
