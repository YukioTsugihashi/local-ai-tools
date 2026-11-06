"""Excel全行の構造集計と、型・重複関係を保つダミー化。通信なし。"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import json
import io
import math
from pathlib import Path
import random
import re
from zipfile import BadZipFile, ZipFile, ZIP_DEFLATED

from openpyxl import Workbook, load_workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter
from openpyxl.utils.exceptions import InvalidFileException

KANA = "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをん"
MAX_TEXT = 20
OUTPUTS = ["data_overview.xlsx", "dummy_data.xlsx", "data_overview_csv.zip", "dummy_data_csv.zip"]


class UserError(Exception):
    """元データを含めない、利用者向けエラー。"""


def kind(value):
    if value is None or value == "":
        return "空欄"
    if isinstance(value, bool):
        return "真偽値"
    if isinstance(value, datetime):
        return "日付時刻"
    if isinstance(value, date):
        return "日付"
    if isinstance(value, time):
        return "時刻"
    if isinstance(value, timedelta):
        return "経過時間"
    if isinstance(value, (int, float)):
        return "数値"
    if isinstance(value, str):
        return "文字列"
    raise UserError("未対応のセル形式があります。通常の文字・数値・日付・時刻に直してください。")


class Mapper:
    """対応表はメモリ内だけ。実行をまたぐ再識別用キーは保存しない。"""
    def __init__(self, seed=None, alphabet=KANA, max_text=MAX_TEXT):
        self.rng = random.Random(seed)
        self.alphabet = alphabet
        self.max_text = max_text
        self.mapping = {}
        self.used_text = set()
        self.offsets = {}
        self.text_counters = {}
        self.extended = 0
        self.truncated = 0

    def rotate(self, group, rank, size):
        if size < 2:
            raise UserError("元の形式で異なるダミー値を作れません。形式条件を見直してください。")
        if group not in self.offsets:
            self.offsets[group] = self.rng.randrange(1, size)
        return (rank + self.offsets[group]) % size

    def text(self, value):
        # 数字と区切りだけの文字列は、各数字の桁数・区切り・先頭0を維持。
        # 日付やIDの意味を推測して型変換しない。
        if re.fullmatch(r"[0-9 +:./TtZz_-]+", value) and re.search(r"[0-9]", value):
            pieces = re.split(r"([0-9]+)", value)
            shape, ranks, sizes = [], [], []
            for item in pieces:
                if item.isascii() and item.isdigit():
                    zeros = min(len(item) - len(item.lstrip("0")), len(item) - 1)
                    width = len(item) - zeros
                    lower = 10 ** (width - 1) if width > 1 else 0
                    shape.append(("0" * zeros, width))
                    ranks.append(int(item) - lower)
                    sizes.append(10 ** width - lower)
                else:
                    shape.append(item)
            total, rank = 1, 0
            for size, part in zip(sizes, ranks):
                rank = rank * size + part
                total *= size
            rank = self.rotate(("digit_text", tuple(shape)), rank, total)
            newparts = []
            for size in reversed(sizes):
                newparts.append(rank % size)
                rank //= size
            newparts.reverse()
            result, index = [], 0
            for item in shape:
                if isinstance(item, str):
                    result.append(item)
                else:
                    prefix, width = item
                    part = newparts[index]
                    index += 1
                    lower = 10 ** (width - 1) if width > 1 else 0
                    result.append(prefix + str(lower + part))
            return "".join(result)
        # 仮名の空間を重複なく順に使う。各文字数でランダムな開始点を使う。
        start = min(len(value), self.max_text)
        for length in range(max(1, start), self.max_text + 1):
            size = len(self.alphabet) ** length
            if length not in self.text_counters:
                self.text_counters[length] = [self.rng.randrange(size), 0]
            state = self.text_counters[length]
            while state[1] < size:
                index = (state[0] + state[1]) % size
                state[1] += 1
                letters = []
                for _ in range(length):
                    letters.append(self.alphabet[index % len(self.alphabet)])
                    index //= len(self.alphabet)
                candidate = "".join(letters)
                if candidate == value or candidate in self.used_text:
                    continue
                self.used_text.add(candidate)
                self.extended += length > len(value)
                self.truncated += len(value) > self.max_text
                return candidate
        raise UserError("20文字以内で重複しないダミー文字列を作れないため、処理を打ち切りました。")

    def number(self, value):
        if not math.isfinite(value):
            raise UserError("有限でない数値があります。通常の数値に直してください。")
        decimal = Decimal(str(value))
        precision = max(0, -decimal.as_tuple().exponent)
        sign = -1 if decimal < 0 else 1
        absolute = abs(decimal)
        digits = max(1, len(str(int(absolute))))
        if digits + precision > 15:
            raise UserError("15桁を超える数値があります。正確な桁を保つには元Excelで文字列として保存してください。")
        scale = 10 ** precision
        lower = (10 ** (digits - 1) if digits > 1 else 0) * scale
        upper = 10 ** digits * scale
        if sign < 0 and lower == 0:
            lower = 1
        scaled = int(absolute * scale)
        # 小数末尾の非0を保ち、再読込時に小数の桁数が減らないようにする。
        if precision:
            def count_before(n):
                return n - (n + 9) // 10
            base = count_before(lower)
            rank = count_before(scaled) - base
            size = count_before(upper) - base
            target = self.rotate(("num", sign, digits, precision), rank, size) + base
            scaled_out = (target // 9) * 10 + target % 9 + 1
            return float(Decimal(sign * scaled_out) / scale)
        rank = scaled - lower
        return sign * (lower + self.rotate(("num", sign, digits, 0), rank, upper - lower))

    def convert(self, value):
        category = kind(value)
        if category == "空欄":
            return value
        # Excelの同じ数値はint/floatの内部読取差にかかわらず同じ対応。
        key = (category, value)
        if key in self.mapping:
            return self.mapping[key]
        if category == "文字列":
            result = self.text(value)
        elif category == "数値":
            result = self.number(value)
        elif category == "真偽値":
            result = not value
        elif category in {"日付", "日付時刻"}:
            minimum, maximum = date(1900, 1, 1).toordinal(), date(9999, 12, 31).toordinal()
            ordinal = value.toordinal()
            if not minimum <= ordinal <= maximum:
                raise UserError("1900年より前の日付があります。対応する日付範囲を確認してください。")
            # datetimeのミリ秒・時刻部分も維持可能な精度のまま置換。
            result = date.fromordinal(minimum + self.rotate("day", ordinal - minimum, maximum - minimum + 1))
            if category == "日付時刻":
                result = datetime.combine(result, self.convert(value.time()))
        elif category == "時刻":
            second = value.hour * 3600 + value.minute * 60 + value.second
            new = self.rotate("clock", second, 86400)
            micro = value.microsecond
            if micro:
                trailing = len(str(micro)) - len(str(micro).rstrip("0"))
                factor = 10 ** trailing
                magnitude = micro // factor
                digits = len(str(magnitude))
                lower = 10 ** (digits - 1)
                base = lower - (lower + 9) // 10
                rank = magnitude - (magnitude + 9) // 10 - base
                upper = 10 ** digits
                size = upper - (upper + 9) // 10 - base
                mapped = self.rotate(("micro", factor, digits), rank, size) + base
                micro = ((mapped // 9) * 10 + mapped % 9 + 1) * factor
            result = time(new // 3600, new // 60 % 60, new % 60, micro)
        elif category == "経過時間":
            # timedeltaの全マイクロ秒を整数として対応固定。
            ticks = value.days * 86400000000 + value.seconds * 1000000 + value.microseconds
            result = timedelta(microseconds=self.number(ticks))
        else:
            raise UserError("対応していない形式があります。")
        self.mapping[key] = result
        return result


def put(sheet, row, value, number_format="General", data_type=None):
    cell = WriteOnlyCell(sheet, value=value)
    if isinstance(value, str):
        cell.data_type = "s"  # '='で始まるカラム名も数式として実行しない。
    elif data_type == "e":
        cell.data_type = "e"
    cell.number_format = number_format or "General"
    row.append(cell)


def styled_sheet(book, name, headers, widths):
    sheet = book.create_sheet(name)
    sheet.freeze_panes = "A2"
    for i, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(i)].width = width
    cells = []
    for value in headers:
        cell = WriteOnlyCell(sheet, value=value)
        cell.font = Font(name="Yu Gothic", bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="214E70")
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        cells.append(cell)
    sheet.append(cells)
    sheet.row_dimensions[1].height = 32
    return sheet


def csv_value(value):
    if value is None:
        return ""
    if isinstance(value, (datetime, date, time)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, timedelta):
        # Excel経過時間の数値表現(日単位)。表示書式は概要の形式内訳に記録。
        return str(Decimal(value.days) + Decimal(value.seconds * 1000000 + value.microseconds) / Decimal(86400000000))
    return str(value)


def csv_archive(source, destination):
    book = load_workbook(source, read_only=True, data_only=True)
    try:
        with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
            index = io.StringIO(newline="")
            writer = csv.writer(index)
            writer.writerow(["CSVファイル名", "元のシート名"])
            for i, sheet in enumerate(book.worksheets, 1):
                filename = f"sheet_{i:03d}.csv"
                writer.writerow([filename, sheet.title])
                # 大きなCSVもメモリに丸ごと保持しない。
                with archive.open(filename, "w") as raw_csv:
                    with io.TextIOWrapper(raw_csv, encoding="utf-8-sig", newline="") as text_csv:
                        output = csv.writer(text_csv)
                        for row in sheet.iter_rows(values_only=True):
                            output.writerow([csv_value(value) for value in row])
            archive.writestr("sheet_index.csv", index.getvalue().encode("utf-8-sig"))
    finally:
        book.close()


def run(request, job_dir):
    job_dir = Path(job_dir).resolve()
    items = request.get("inputs") or []
    if len(items) != 1:
        raise UserError("Excelファイルを1つ添付してください。")
    relative = Path(items[0].get("path", ""))
    source = (job_dir / relative).resolve()
    if relative.is_absolute() or not source.is_relative_to(job_dir) or not source.is_file():
        raise UserError("入力ファイルを確認し、Excelを添付し直してください。")
    if source.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise UserError(".xlsx または .xlsm のExcelを添付してください。.xlsは.xlsxで保存し直してください。")
    out_relative = Path(request.get("outdir", "out"))
    outdir = (job_dir / out_relative).resolve()
    if out_relative.is_absolute() or not outdir.is_relative_to(job_dir) or source.is_relative_to(outdir):
        raise UserError("出力先の指定を確認してください。")
    outdir.mkdir(parents=True, exist_ok=True)
    try:
        raw = load_workbook(source, read_only=True, data_only=False, keep_links=False)
        cached = load_workbook(source, read_only=True, data_only=True, keep_links=False)
    except (BadZipFile, InvalidFileException, KeyError, ValueError):
        raise UserError("Excelを読み取れません。暗号化を解除し、Excelで開いて.xlsxとして保存し直してください。") from None
    mapper = Mapper()
    dummy = Workbook(write_only=True)
    dummy.epoch = raw.epoch
    overview = Workbook(write_only=True)
    summary = styled_sheet(overview, "シート概要", ["シート名", "列数", "データ行数", "有効セル数", "空欄セル数"], [28, 14, 18, 18, 18])
    columns = styled_sheet(overview, "カラム概要", ["シート名", "列番号", "カラム名", "データ行数", "有効データ行数", "空欄数", "データ形式と件数"], [28, 12, 30, 18, 20, 16, 65])
    formats = styled_sheet(overview, "形式内訳", ["シート名", "列番号", "カラム名", "データ形式", "表示形式", "セル数"], [28, 12, 30, 18, 35, 16])
    info = styled_sheet(overview, "処理条件", ["項目", "内容"], [30, 110])
    totals, formula_count = 0, 0
    try:
        # 1周目で実際の値のある範囲を全走査。書式だけの末尾は行数に含めない。
        bounds = []
        for si, sheet in enumerate(raw.worksheets, 1):
            sheet.reset_dimensions()
            last_row = last_col = 0
            for ri, row in enumerate(sheet.iter_rows(), 1):
                for ci, cell in enumerate(row, 1):
                    if cell.value is not None:
                        last_row, last_col = max(last_row, ri), max(last_col, ci)
                        if cell.data_type == "f" and ri == 1:
                            raise UserError(f"シート{si}の1行目に数式があります。カラム名を文字または値に直してください。")
            bounds.append((last_row, last_col))
        if not any(r >= 2 for r, _ in bounds):
            raise UserError("2行目以降にデータがありません。1行目をカラム名としてデータを入力してください。")
        for si, (sheet, cached_sheet, bound) in enumerate(zip(raw.worksheets, cached.worksheets, bounds), 1):
            rows, cols = bound
            data_rows = max(0, rows - 1)
            target = dummy.create_sheet(sheet.title)
            target.freeze_panes = "A2"
            counts = [Counter() for _ in range(cols)]
            format_counts = [Counter() for _ in range(cols)]
            headers = [None] * cols
            if rows:
                cached_sheet.reset_dimensions()
                for ri, (row, cache_row) in enumerate(zip(
                    sheet.iter_rows(min_row=1, max_row=rows, max_col=cols),
                    cached_sheet.iter_rows(min_row=1, max_row=rows, max_col=cols)), 1):
                    output = []
                    for ci, (cell, cache_cell) in enumerate(zip(row, cache_row), 1):
                        value = cell.value
                        if cell.data_type == "f":
                            formula_count += 1
                            value = cache_cell.value
                            if value is None:
                                raise UserError(f"シート{si}の{ri}行{ci}列に計算結果のない数式があります。Excelで再計算して保存してください。")
                        if cell.data_type == "e" or cache_cell.data_type == "e":
                            raise UserError(f"シート{si}の{ri}行{ci}列にExcelエラー値があります。修正して保存してください。")
                        if ri == 1:
                            headers[ci - 1] = value
                            converted = value
                        else:
                            category = kind(value)
                            counts[ci - 1][category] += 1
                            format_counts[ci - 1][(category, cell.number_format or "General")] += 1
                            converted = mapper.convert(value)
                        put(target, output, converted, cell.number_format)
                    target.append(output)
            valid = sum(sum(c.values()) - c["空欄"] for c in counts)
            summary.append([sheet.title, cols, data_rows, valid, data_rows * cols - valid])
            totals += data_rows
            for ci, counter in enumerate(counts, 1):
                header = headers[ci - 1]
                label = "、".join(f"{key}: {value}" for key, value in sorted(counter.items()) if key != "空欄") or "空欄のみ"
                result_row = []
                for value in [sheet.title, ci, header, data_rows, data_rows - counter["空欄"], counter["空欄"], label]:
                    put(columns, result_row, value)
                columns.append(result_row)
                for (category, fmt), count in sorted(format_counts[ci - 1].items()):
                    result_row = []
                    for value in [sheet.title, ci, header, category, fmt, count]:
                        put(formats, result_row, value)
                    formats.append(result_row)
        rules = [
            ("カラム名", "1行目を保持。2行目以降の全行を処理。"),
            ("行・列の範囲", "値のある最終行・最終列まで。範囲内の空行・空列・空欄位置を保持。書式だけの末尾は除外。"),
            ("有効データ", "空欄・空文字以外。0、False、空白だけの文字列も有効。"),
            ("型と表示形式", "セルごとの保存型とExcel表示形式を保持。文字列を日付や数値に変換しない。"),
            ("文字列", "文字列全体で対応固定。原則同じ文字数。候補不足時は最大20文字。元が20文字超なら20文字に短縮。"),
            ("数字を含む文字列", "数字と区切りだけなら桁数・区切り・先頭0を保持。文字列の日付らしさや暦上の妥当性は推定しない。"),
            ("対応固定", "同じ型の同じ値は全シートで共通。異なる値を同一ダミー値に統合しない。対応は今回の処理内で有効。"),
            ("数値", "符号・整数部桁数・保存値の小数桁数・表示形式を保持。元の分布や大小関係は保証しない。"),
            ("日付と時刻", "Excelの保存型・表示形式を保持。同じ値の対応を固定。前後関係や時間差は保証しない。"),
            ("数式", f"計算済みの値をダミー化。数式自体は出力しない。処理数: {formula_count}"),
            ("出力対象", "セル値・表示形式・シート名。コメント・リンク・画像・マクロ・埋込オブジェクト・元文書属性は引き継がない。"),
            ("CSV", "Excelと同じダミー値をUTF-8 BOM付きCSVで出力。1シート1CSVをZIP化。型・表示書式はExcel版と形式内訳を参照。日付時刻はISO表記。"),
            ("文字数を増やした種類数", mapper.extended),
            ("20文字に短縮した種類数", mapper.truncated),
        ]
        for rule in rules:
            info.append(rule)
        # 両方が完成するまで利用者へファイルを返さない。
        dummy.save(outdir / OUTPUTS[1])
        overview.save(outdir / OUTPUTS[0])
        csv_archive(outdir / OUTPUTS[0], outdir / OUTPUTS[2])
        csv_archive(outdir / OUTPUTS[1], outdir / OUTPUTS[3])
    except Exception:
        for book in (dummy, overview):
            for sheet in book.worksheets:
                if not sheet.closed:
                    sheet.close()
        for name in OUTPUTS:
            path = outdir / name
            if path.is_file():
                path.unlink()
        raise
    finally:
        raw.close()
        cached.close()
    return {"status": "ok", "message": f"{len(bounds)}シート・データ{totals}行を処理しました。データ概要とダミーデータをExcelとCSV（ZIP）で作成しました。",
            "files": OUTPUTS, "skipped": [], "notes": ["対応表は出力しません。カラム名・シート名は元のままです。",
            f"文字数拡張: {mapper.extended}種類、20文字への短縮: {mapper.truncated}種類。"]}


def main():
    parser = argparse.ArgumentParser(description="データ構造学習")
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    request_path = Path(args.request).resolve()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    try:
        result = run(request, request_path.parent)
    except UserError as error:
        result = {"status": "user_error", "message": str(error), "files": [], "skipped": [], "notes": []}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
