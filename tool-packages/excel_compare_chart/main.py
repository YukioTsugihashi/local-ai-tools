"""Excelの2つの列を比べて、棒グラフと記録用のExcelを作る。

**計算はすべてPythonで行う。LLMは使わない。**
数値の集計をLLMに任せると、合っているかどうかを確かめられなくなるため。

実物の帳票を読むための作り(2026-08-31に実物で作り直した):

  - **見出しは1行目とは限らない。** 表題や注意書きが上に何行もあり、
    見出しはその下にある。数値の並びから見出し行を探す
  - **1枚のシートに表が複数ある。** 「(1)入院関係の実績」「(2)外来関係の実績」
    のように節が分かれている。節ごとに別の表として扱う
  - **項目名は1列とは限らない。** 大項目がB列、内訳がC列のように分かれ、
    結合セルで表示されている。数値列より左にある文字を全部つないで項目名にする

**分からないときは推測しない。** どの表・どの列を比べるのか決められなければ、
選べるものを並べて聞き返す。**利用者が指定した列は、数値でなくてもその列として扱い、
なぜ使えないかを具体的に答える**(同じ文言を繰り返さない)。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata
from datetime import datetime

# この個数以上の数値が横に並んでいたら「表の中身の行」とみなす
MIN_NUMERIC_IN_ROW = 2
# 同じ表の続きとみなす、数値列の重なり具合。空欄のある行で表が切れないようにする
SAME_BLOCK_OVERLAP = 0.6
# 数値の行がこれだけ離れていたら、別の表とみなす
MAX_ROW_GAP = 6
# 見出しは何段まで見るか(「R8年度 / 入院 / 入院収入」のような多段見出し)
HEADER_LEVELS = 3
# 比べる対象が多すぎると棒グラフが読めない
MAX_ITEMS = 40
# これを超えたら縦棒をやめて横棒にする(名前を真っ直ぐ読ませるため)
HORIZONTAL_FROM = 12
# エラー表示に載せる候補の数
MAX_CHOICES = 12


class NeedsUserInput(Exception):
    """利用者に決めてもらう必要がある。推測で進めない。"""


def _normalized(text: object) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or ""))).lower()


def _as_number(value: object) -> float | None:
    """数値として読めれば返す。Excelのエラー値や文字は None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = unicodedata.normalize("NFKC", str(value or "")).strip().replace(",", "")
    if not text or text.startswith("#"):  # #REF! などのエラー値
        return None
    text = re.sub(r"[%円件名人回日]+$", "", text)
    try:
        return float(text)
    except ValueError:
        return None


def _column_letter(index: int) -> str:
    from openpyxl.utils import get_column_letter

    return get_column_letter(index)


def _letter_to_index(letter: str) -> int:
    from openpyxl.utils import column_index_from_string

    return column_index_from_string(letter.upper())


class Block:
    """1つの表(節)。見出し行・数値の列・中身の行をまとめて持つ。"""

    def __init__(self, title: str, header_row: int, columns: list[int], rows: list[int]) -> None:
        self.title = title
        self.header_row = header_row
        self.columns = columns  # 数値が並んでいる列(1起点)
        self.rows = rows

    def header(self, sheet, column: int) -> str:
        value = sheet.cell(row=self.header_row, column=column).value
        return str(value).strip() if value is not None else ""

    def full_name(self, sheet, column: int) -> str:
        """多段見出しをつないだ列の名前。

        「R8年度 / 入院 / 入院収入」のように上の段が結合セルで束ねられている表では、
        いちばん下の段(入院収入)だけでは年度をまたいで同じ名前になってしまう。
        上の段を右へ引き継いで、区別できる名前にする。
        """
        parts: list[str] = []
        for level in range(HEADER_LEVELS - 1, 0, -1):
            row = self.header_row - level
            if row < 1:
                continue
            value = _filled_left(sheet, row, column, min(self.columns))
            if value and value not in parts:
                parts.append(value)
        head = self.header(sheet, column)
        if head and head not in parts:
            parts.append(head)
        return " ".join(parts)

    def choices(self, sheet) -> str:
        shown = []
        for column in self.columns[:MAX_CHOICES]:
            name = self.full_name(sheet, column) or "(見出しなし)"
            shown.append(f"{_column_letter(column)}列「{name}」")
        more = len(self.columns) - MAX_CHOICES
        text = "、".join(shown)
        return text + (f" ほか{more}列" if more > 0 else "")


def _text_of(value: object) -> str:
    """セルの文字。真偽値とエラー値は文字として扱わない。

    チェック用の行に True/False が並ぶ帳票があり、これを文字とみなすと
    「見出しの行」と誤認して1つの表が割れる(実物で発生)。
    """
    if value is None or isinstance(value, bool):
        return ""
    text = str(value).strip()
    return "" if text.startswith("#") else text


def _filled_left(sheet, row: int, column: int, left_limit: int) -> str:
    """その位置の値。空なら左へさかのぼって、直近の値を引き継ぐ(結合セル対策)。"""
    for c in range(column, max(left_limit - 1, 0), -1):
        value = sheet.cell(row=row, column=c).value
        text = str(value).strip() if value is not None else ""
        if text and not text.startswith("#"):
            return text
    return ""


def _header_between(sheet, previous: int, current: int, columns: set[int]) -> bool:
    """2つの数値行の間に、新しい見出し行があるか。

    見出しらしさは「数値が並ぶはずの列に、文字が2つ以上入っている行」で判定する。
    小計行や注記行はここに当たらないので、表が無用に分かれない。
    """
    for row in range(previous + 1, current):
        texts = 0
        for column in columns:
            value = sheet.cell(row=row, column=column).value
            if _text_of(value) and _as_number(value) is None:
                texts += 1
        if texts >= 2:
            return True
    return False


def _numeric_columns_in_row(sheet, row: int, max_column: int) -> list[int]:
    return [
        c for c in range(1, max_column + 1)
        if _as_number(sheet.cell(row=row, column=c).value) is not None
    ]


def find_blocks(sheet) -> list[Block]:
    """シートの中の表を、数値の並びから見つける。

    同じ列の組で数値が続く行のかたまりを1つの表とみなし、その直前の行を見出しとする。
    見出しの上にある文字を、その表の名前(「(1)入院関係の実績」など)として拾う。
    """
    max_row = min(sheet.max_row, 500)
    max_column = min(sheet.max_column, 80)

    runs: list[tuple[tuple[int, ...], list[int]]] = []
    for row in range(1, max_row + 1):
        columns = tuple(_numeric_columns_in_row(sheet, row, max_column))
        if len(columns) < MIN_NUMERIC_IN_ROW:
            continue
        if runs:
            known = set(runs[-1][0])
            previous = runs[-1][1][-1]
            overlap = len(known & set(columns)) / max(len(known), 1)
            # 表が変わったと判断するのは、**間に見出し行があるとき**だけにする。
            # 行間の空きだけで切ると、小計や空欄の行で1つの表が割れる。
            # 数値の入り方の一致で切っても同じことが起きる(どちらも実物で発生)
            if (
                row - previous <= MAX_ROW_GAP
                and overlap >= SAME_BLOCK_OVERLAP
                and not _header_between(sheet, previous, row, known)
            ):
                runs[-1][1].append(row)
                continue
        runs.append((columns, [row]))

    blocks: list[Block] = []
    for columns, rows in runs:
        if len(rows) < 2:
            continue  # 1行だけの数値並びは表とみなさない
        header_row = rows[0] - 1
        if header_row < 1:
            continue
        title = _find_title(sheet, header_row, max_column)
        blocks.append(Block(title, header_row, list(columns), rows))
    return blocks


def _find_title(sheet, header_row: int, max_column: int) -> str:
    """見出し行の上をさかのぼって、その表の名前らしい文字を拾う。

    見出しそのもの(「入院収入」など)を表題と取り違えないよう、見出し行は見ない。
    """
    for row in range(header_row - 1, max(header_row - 4, 0), -1):
        for column in range(1, min(max_column, 12) + 1):
            value = sheet.cell(row=row, column=column).value
            text = str(value).strip() if value is not None else ""
            if not text or _as_number(value) is not None:
                continue
            if re.match(r"^[((]?\d+[))]", text) or "実績" in text or "収入" in text:
                return text
    return sheet.title


def label_columns(sheet, block: Block) -> list[int]:
    """項目名が入っている列(文字が並ぶ列)。

    「数値列より左」では足りない。実物には、数値の列(入力番号)が先頭にあり
    項目名(科名)がその右に来る表もある。**中身が文字である列**を項目名として扱う。
    """
    found: list[int] = []
    limit = max(block.columns) if block.columns else 1
    for column in range(1, limit + 1):
        if column in block.columns:
            continue
        texts = 0
        for row in block.rows:
            value = sheet.cell(row=row, column=column).value
            if _text_of(value) and _as_number(value) is None:
                texts += 1
        if texts >= max(2, len(block.rows) // 3):
            found.append(column)
    return found


def row_label(sheet, row: int, columns: list[int]) -> str:
    """項目名の列をつないで1つの名前にする(大項目と内訳が分かれているため)。

    片方がもう片方に含まれるときは短いほうを落とす(「消内 消化器内科」を避ける)。
    """
    parts: list[str] = []
    for column in columns:
        value = sheet.cell(row=row, column=column).value
        text = _text_of(value)
        if not text or _as_number(value) is not None:
            continue
        text = re.sub(r"\s+", " ", text)
        if any(text in kept for kept in parts):
            continue
        parts = [kept for kept in parts if kept not in text]
        parts.append(text)
    return " ".join(parts)


def series_of(sheet, block: Block, column: int, labels: list[int] | None = None) -> list[tuple[str, float]]:
    columns = labels if labels is not None else label_columns(sheet, block)
    values: list[tuple[str, float]] = []
    for row in block.rows:
        label = row_label(sheet, row, columns)
        number = _as_number(sheet.cell(row=row, column=column).value)
        if not label or number is None:
            continue
        values.append((label, number))
    return values


def named_columns(instruction: str) -> list[int]:
    """依頼文で名指しされた列(「B列」「H列」)を、順番どおりに返す。"""
    text = unicodedata.normalize("NFKC", instruction)
    found: list[int] = []
    for match in re.finditer(r"([A-Za-z]{1,2})\s*列", text):
        index = _letter_to_index(match.group(1))
        if index not in found:
            found.append(index)
    return found


def check_named_columns(instruction: str, blocks: list[Block], sheet) -> None:
    """**名指しされた列がどの表にも数値として無いなら、真っ先にそれを答える。**

    表の選択を聞き返すより先に見る。利用者は「B列とC列」と具体的に聞いているのに
    「どの表ですか」と返されるのが、いちばん答えになっていない
    (2026-08-31に実際にそうなった)。
    """
    named = named_columns(instruction)
    if not named:
        return
    usable = {column for block in blocks for column in block.columns}
    unusable = [column for column in named if column not in usable]
    if not unusable:
        return
    letters = "、".join(f"{_column_letter(c)}列" for c in unusable)
    available = blocks[0].choices(sheet) if blocks else ""
    raise NeedsUserInput(
        f"{letters} には数値が入っていません(項目名などの列のようです)。"
        f"数値が入っているのは次の列です: {available}"
    )


def _example(blocks: list[Block]) -> str:
    """そのまま書き写せる依頼文の見本。聞き返しを行き止まりにしないため。"""
    block = blocks[0]
    if len(block.columns) >= 2:
        left, right = _column_letter(block.columns[0]), _column_letter(block.columns[1])
        return f"『{block.title}』の{left}列と{right}列を比較して"
    return f"『{block.title}』を比較して"


def pick_block(instruction: str, blocks: list[Block], sheet) -> Block:
    """どの表を使うか決める。決められなければ、見本つきで聞き返す。"""
    if not blocks:
        raise NeedsUserInput(
            "数値が並んだ表を見つけられませんでした。"
            "同じ列に数値が2行以上続く表を含むファイルを渡してください。"
        )
    if len(blocks) == 1:
        return blocks[0]

    key = _normalized(instruction)
    for block in blocks:
        if block.title and _normalized(block.title) and _normalized(block.title) in key:
            return block
    names = " / ".join(f"「{block.title}」" for block in blocks[:MAX_CHOICES])
    raise NeedsUserInput(
        f"このシートには表が {len(blocks)} つあります。どれを使うか指定してください。\n"
        f"表: {names}\n"
        f"書き方の例: 「{_example(blocks)}」"
    )


def pick_columns(instruction: str, block: Block, sheet) -> list[int]:
    """比べる2列を決める。**利用者が指定した列は、使えなくても理由を具体的に返す。**"""
    text = unicodedata.normalize("NFKC", instruction)

    # 「B列」「H列」のような指定。数値列でなくても、まずその列として受け止める
    named = named_columns(instruction)
    if named:
        unusable = [c for c in named if c not in block.columns]
        if unusable:
            letters = "、".join(f"{_column_letter(c)}列" for c in unusable)
            raise NeedsUserInput(
                f"{letters} には数値が入っていません(項目名の列のようです)。"
                f"数値が入っているのは次の列です: {block.choices(sheet)}"
            )
        if len(named) >= 2:
            return named[:2]

    # 見出し名での指定。多段見出しをつないだ名前(「R8年度 入院 入院収入」)の
    # 部品が依頼文にいくつ出てくるかで絞る。年度だけ違う列を選び分けるため
    key = _normalized(text)
    names = {column: block.full_name(sheet, column) for column in block.columns}
    parts = {part for name in names.values() for part in name.split() if part}
    mentioned = {part for part in parts if _normalized(part) and _normalized(part) in key}
    if mentioned:
        scores = {
            column: sum(1 for part in mentioned if _normalized(part) in _normalized(name))
            for column, name in names.items()
        }
        best = max(scores.values())
        if best:
            chosen = [column for column in block.columns if scores[column] == best]
            if len(chosen) == 2:
                return chosen
            if len(chosen) > 2:
                shown = "、".join(
                    f"{_column_letter(c)}列「{names[c]}」" for c in chosen[:MAX_CHOICES]
                )
                raise NeedsUserInput(
                    f"指定に当てはまる列が {len(chosen)} つありました。2つに絞ってください: {shown}"
                )

    if len(block.columns) == 2:
        return list(block.columns)  # 迷いようがない場合だけ自動で決める

    hint = f"「{block.title}」の" if block.title else ""
    raise NeedsUserInput(
        f"どの列を比べるか分かりませんでした。{hint}次から2つを指定してください: "
        + block.choices(sheet)
    )


TOTAL_SUFFIXES = ("合計", "総計", "小計", "全体")
# 合計行が他より何倍大きければ「並べると潰れる」とみなすか
TOTAL_RATIO = 2.5


def _looks_like_total(label: str) -> bool:
    """「病院全体」「合計」のような、明細の足し上げに見える項目名か。"""
    text = _normalized(label)
    return text == "計" or text.endswith(TOTAL_SUFFIXES)


def split_total_rows(rows: list[tuple[str, float, float]]):
    """合計に見えて他より飛び抜けて大きい行を、グラフから外して返す。

    診療科ごとの比較に「病院全体」を混ぜると、その1本だけが伸びて
    他の科の差が見えなくなる。**名前が合計らしい**ことと
    **他より飛び抜けて大きい**ことの両方が揃ったときだけ外す。
    数値は「比較データ.xlsx」に全件残す。
    """
    candidates = [row for row in rows if _looks_like_total(row[0])]
    if not candidates or len(candidates) >= len(rows):
        return rows, []

    others = [row for row in rows if row not in candidates]
    largest_other = max((max(abs(row[1]), abs(row[2])) for row in others), default=0.0)
    removed = [
        row for row in candidates
        if max(abs(row[1]), abs(row[2])) > largest_other * TOTAL_RATIO
    ]
    if not removed:
        return rows, []
    return [row for row in rows if row not in removed], removed


def align(
    left: list[tuple[str, float]], right: list[tuple[str, float]]
) -> tuple[list[str], list[float], list[float], list[str]]:
    """項目名で突き合わせる。**片方にしかない項目は勝手に対応づけない。**"""
    left_map = {_normalized(label): (label, value) for label, value in left}
    right_map = {_normalized(label): (label, value) for label, value in right}
    shared = [key for key in left_map if key in right_map]

    labels = [left_map[key][0] for key in shared]
    left_values = [left_map[key][1] for key in shared]
    right_values = [right_map[key][1] for key in shared]

    mismatches = []
    only_left = [left_map[key][0] for key in left_map if key not in right_map]
    only_right = [right_map[key][0] for key in right_map if key not in left_map]
    if only_left:
        mismatches.append(f"左にしかない項目 {len(only_left)} 件: {'、'.join(only_left[:5])}")
    if only_right:
        mismatches.append(f"右にしかない項目 {len(only_right)} 件: {'、'.join(only_right[:5])}")
    return labels, left_values, right_values, mismatches


def draw_chart(labels, left_values, right_values, left_name, right_name, target: str) -> None:
    """棒グラフを描く。日本語は同梱フォントで出す。

    診療科ごとの比較は 30 項目を超えることがある。縦棒のまま横に伸ばすと
    画面に収まらず名前も斜めで読めなくなるので、項目が多いときは横棒にして
    名前を左へ真っ直ぐ並べる(実物の科別比較で必要になった)。
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker
    import matplotlib_fontja  # noqa: F401  日本語フォントの登録に要る

    shown = [label if len(label) <= 24 else label[:24] + "…" for label in labels]
    positions = range(len(labels))
    width = 0.38
    horizontal = len(labels) > HORIZONTAL_FROM

    if horizontal:
        height = min(24.0, max(4.5, 0.32 * len(labels) + 1.6))
        figure, axes = plt.subplots(figsize=(11.0, height))
        # 上から順に読めるよう、1件目を一番上に置く
        axes.barh([p + width / 2 for p in positions], left_values, width, label=left_name)
        axes.barh([p - width / 2 for p in positions], right_values, width, label=right_name)
        axes.set_yticks(list(positions))
        axes.set_yticklabels(shown, fontsize=9)
        axes.invert_yaxis()
        axes.grid(axis="x", alpha=0.3)
    else:
        figure, axes = plt.subplots(
            figsize=(max(7.0, 0.85 * len(labels) + 3), max(4.5, 0.30 * len(labels) + 4.0))
        )
        axes.bar([p - width / 2 for p in positions], left_values, width, label=left_name)
        axes.bar([p + width / 2 for p in positions], right_values, width, label=right_name)
        axes.set_xticks(list(positions))
        axes.set_xticklabels(shown, rotation=45, ha="right", fontsize=9)
        axes.grid(axis="y", alpha=0.3)

    # 既定だと軸が「1e7」のような指数表記になり、金額として読めない
    value_axis = axes.xaxis if horizontal else axes.yaxis
    value_axis.set_major_formatter(
        ticker.FuncFormatter(lambda value, _: f"{value:,.0f}")
    )
    axes.legend()
    figure.tight_layout()
    figure.savefig(target, dpi=110)
    plt.close(figure)


def write_record(
    labels, left_values, right_values, left_name, right_name,
    source_name: str, instruction: str, block_title: str, target: str,
) -> None:
    """グラフの元になったデータと条件を残す(後から確かめられるように)。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    book = Workbook()
    data = book.active
    data.title = "比較データ"
    data.append(["項目", left_name, right_name, "差(右-左)"])
    for cell in data[1]:
        cell.font = Font(bold=True)
    for label, left, right in zip(labels, left_values, right_values):
        data.append([label, left, right, right - left])
    for column, width in zip("ABCD", (40, 18, 18, 16)):
        data.column_dimensions[column].width = width

    condition = book.create_sheet("条件")
    condition.append(["項目", "内容"])
    for cell in condition[1]:
        cell.font = Font(bold=True)
    for row in [
        ["元のファイル", source_name],
        ["依頼文", instruction],
        ["使った表", block_title],
        ["左の系列", left_name],
        ["右の系列", right_name],
        ["比べた項目数", len(labels)],
        ["作成日時", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
        ["計算方法", "Pythonで集計(LLMは使っていません)"],
    ]:
        condition.append(row)
    condition.column_dimensions["A"].width = 16
    condition.column_dimensions["B"].width = 60

    book.save(target)


def _reply(status: str, message: str, files=None, skipped=None, notes=None) -> None:
    print(json.dumps({
        "status": status, "message": message, "files": files or [],
        "skipped": skipped or [], "notes": notes or [],
    }, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Excelの2つの列を比べてグラフにする")
    parser.add_argument("--request", required=True)
    args = parser.parse_args()

    request = json.loads(open(args.request, encoding="utf-8").read())
    job_dir = os.path.dirname(args.request)
    entry = request["inputs"][0]
    source_path = os.path.join(job_dir, entry["path"])
    instruction = str(request.get("instruction") or "")

    from openpyxl import load_workbook

    book = load_workbook(source_path, data_only=True)  # 元のファイルは読むだけ

    # シートの指定があればそれを、無ければ表が見つかる最初のシートを使う
    key = _normalized(instruction)
    named = [name for name in book.sheetnames if _normalized(name) and _normalized(name) in key]
    candidates = named or book.sheetnames

    try:
        sheet = None
        blocks: list[Block] = []
        for name in candidates:
            found = find_blocks(book[name])
            if found:
                sheet, blocks = book[name], found
                break
        if sheet is None:
            raise NeedsUserInput(
                "数値が並んだ表を見つけられませんでした。"
                "同じ列に数値が2行以上続く表を含むファイルを渡してください。"
            )
        # 名指しの列が使えないなら、表を聞き返すより先にそれを答える
        check_named_columns(instruction, blocks, sheet)
        block = pick_block(instruction, blocks, sheet)
        columns = pick_columns(instruction, block, sheet)
    except NeedsUserInput as error:
        _reply("user_error", str(error))
        return 0

    left_name = block.full_name(sheet, columns[0]) or f"{_column_letter(columns[0])}列"
    right_name = block.full_name(sheet, columns[1]) or f"{_column_letter(columns[1])}列"
    label_cols = label_columns(sheet, block)
    labels, left_values, right_values, mismatches = align(
        series_of(sheet, block, columns[0], label_cols),
        series_of(sheet, block, columns[1], label_cols),
    )
    if not labels:
        _reply(
            "user_error",
            "比べられる項目がありませんでした。項目名の列に文字が入っているか確かめてください。",
            skipped=mismatches,
        )
        return 0

    notes: list[str] = []
    if len(blocks) > 1:
        others = "、".join(f"「{b.title}」" for b in blocks if b is not block)
        notes.append(f"「{block.title}」を使いました(ほかに {others} があります)")
    if len(labels) > MAX_ITEMS:
        notes.append(f"項目が多いため、先頭 {MAX_ITEMS} 件だけをグラフにしました")
        labels, left_values, right_values = (
            labels[:MAX_ITEMS], left_values[:MAX_ITEMS], right_values[:MAX_ITEMS]
        )

    # 両方とも0の項目はグラフでは空白の棒にしかならないので描かない。
    # ただし数値そのものは「比較データ.xlsx」に全件残す(勝手に消さない)
    drawn = [
        (label, left, right)
        for label, left, right in zip(labels, left_values, right_values)
        if left or right
    ]
    if not drawn:
        _reply(
            "user_error",
            f"{left_name} と {right_name} は、共通する {len(labels)} 項目すべてが0でした。\n"
            "比べる列が合っているか確かめてください。",
            skipped=mismatches,
        )
        return 0
    if len(drawn) < len(labels):
        empty = len(labels) - len(drawn)
        notes.append(
            f"どちらも0の項目 {empty} 件はグラフから外しました"
            "(数値は「比較データ.xlsx」に全件入っています)"
        )
    drawn, totals = split_total_rows(drawn)
    if totals:
        names = "、".join(f"「{row[0]}」" for row in totals)
        notes.append(
            f"{names} は他より桁が大きく、並べると他の項目が見えなくなるため"
            "グラフから外しました(数値は「比較データ.xlsx」にあります)"
        )
    chart_labels = [item[0] for item in drawn]
    chart_left = [item[1] for item in drawn]
    chart_right = [item[2] for item in drawn]

    chart_name, record_name = "比較グラフ.png", "比較データ.xlsx"
    out_dir = os.path.join(job_dir, request["outdir"])
    draw_chart(chart_labels, chart_left, chart_right, left_name, right_name,
               os.path.join(out_dir, chart_name))
    write_record(labels, left_values, right_values, left_name, right_name,
                 entry.get("filename", ""), instruction, block.title,
                 os.path.join(out_dir, record_name))

    total_left, total_right = sum(left_values), sum(right_values)
    message = (
        f"「{block.title}」の {left_name} と {right_name} を {len(labels)} 項目で比べました。\n"
        f"合計は {total_left:,.0f} と {total_right:,.0f} で、差は {total_right - total_left:+,.0f} です。\n"
        "グラフの元になった数値は「比較データ.xlsx」に入れてあります。"
    )
    notes.append("集計はすべてPythonで行っています(LLMは使っていません)")
    _reply("ok", message, files=[chart_name, record_name],
           skipped=mismatches, notes=notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
