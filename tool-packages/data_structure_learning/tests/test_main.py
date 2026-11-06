"""合成データだけで型・件数・対応固定・失敗時の動作を検証。"""
import csv
from datetime import datetime, time, timedelta
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import re
from zipfile import ZipFile, ZIP_DEFLATED

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment

import main
from samples.make_sample import create_sample


def request():
    return {"contract": 1, "instruction": "ダミーデータを作成", "inputs": [{"path": "in/source.xlsx"}], "outdir": "out", "config": {}}


def fixture_file(tmp_path):
    source = tmp_path / "in" / "source.xlsx"
    source.parent.mkdir()
    create_sample(source)
    return source


def test_complete_roundtrip_counts_types_and_cross_sheet_mapping(tmp_path):
    source = fixture_file(tmp_path)
    original_bytes = source.read_bytes()
    result = main.run(request(), tmp_path)
    assert result["status"] == "ok"
    assert source.read_bytes() == original_bytes
    assert set(result["files"]) == set(main.OUTPUTS)
    original = load_workbook(source)
    dummy = load_workbook(tmp_path / "out" / "dummy_data.xlsx")
    mappings, reverse = {}, {}
    assert dummy.sheetnames == original.sheetnames
    for a, b in zip(original, dummy):
        assert (a.max_row, a.max_column) == (b.max_row, b.max_column)
        assert [c.value for c in a[1]] == [c.value for c in b[1]]
        for ar, br in zip(a.iter_rows(min_row=2), b.iter_rows(min_row=2)):
            for ac, bc in zip(ar, br):
                assert ac.number_format == bc.number_format
                assert ac.data_type == bc.data_type
                assert type(ac.value) is type(bc.value)
                if ac.value is None:
                    assert bc.value is None
                    continue
                key = (main.kind(ac.value), ac.value)
                output = (main.kind(bc.value), bc.value)
                if key in mappings:
                    assert mappings[key] == output
                if output in reverse:
                    assert reverse[output] == key
                mappings[key], reverse[output] = output, key
                assert bc.value != ac.value
                if isinstance(ac.value, str):
                    assert len(ac.value) == len(bc.value)
    assert dummy.worksheets[0]["A2"].value.startswith("00")
    overview = load_workbook(tmp_path / "out" / "data_overview.xlsx", data_only=True)
    summary = list(overview["シート概要"].values)
    assert summary[1][1:3] == (9, 150)
    assert summary[2][1:3] == (2, 1)
    columns = list(overview["カラム概要"].values)
    assert columns[8][3:6] == (150, 0, 150)
    assert columns[7][4] == 150  # ゼロを含む数値列もすべて有効
    assert "文字列: 75" in columns[9][6] and "数値: 75" in columns[9][6]
    with ZipFile(tmp_path / "out" / "dummy_data_csv.zip") as archive:
        assert archive.namelist() == ["sheet_001.csv", "sheet_002.csv", "sheet_index.csv"]
        payload = archive.read("sheet_001.csv")
        assert payload.startswith(b"\xef\xbb\xbf")
        rows = list(csv.reader(io.StringIO(payload.decode("utf-8-sig"))))
        assert len(rows) == 151 and len(rows[0]) == 9
        for row, cells in zip(rows, dummy.worksheets[0].iter_rows()):
            assert row == [main.csv_value(c.value) for c in cells]


@pytest.mark.parametrize("values", [list(range(10)), list(range(10, 100)), list(range(-99, 0)),
    [float(Decimal(i) / 100) for i in range(1, 1000) if i % 10],
    [-float(Decimal(i) / 100) for i in range(1, 1000) if i % 10]])
def test_numeric_permutation_no_collision_and_same_precision(values):
    mapper = main.Mapper(seed=42)
    converted = [mapper.convert(v) for v in values]
    assert len(set(converted)) == len(set(values))
    for before, after in zip(values, converted):
        assert before != after
        assert mapper.convert(before) == after
        assert len(str(abs(int(before)))) == len(str(abs(int(after))))
        assert Decimal(str(before)).as_tuple().exponent == Decimal(str(after)).as_tuple().exponent


def test_text_shortage_extends_and_limit_stops_without_collisions():
    mapper = main.Mapper(seed=3, alphabet="あい", max_text=3)
    outputs = [mapper.convert(v) for v in "甲乙丙丁戊己庚辛壬癸"]
    assert len(set(outputs)) == 10
    assert len(outputs[0]) == len(outputs[1]) == 1
    assert all(1 <= len(v) <= 3 for v in outputs)
    assert mapper.convert("甲") == outputs[0]
    for v in "子丑寅卯":
        mapper.convert(v)
    with pytest.raises(main.UserError, match="打ち切り"):
        mapper.convert("辰")
    mapper = main.Mapper(seed=3)
    assert len(mapper.convert("長" * 25)) == 20
    assert mapper.truncated == 1


def test_all_one_character_kana_values_are_injective():
    mapper = main.Mapper(seed=9)
    outputs = [mapper.convert(v) for v in main.KANA]
    assert len(set(outputs)) == len(outputs)
    assert all(v != result for v, result in zip(main.KANA, outputs))


def test_digit_strings_preserve_shape_leading_zero_and_type():
    mapper = main.Mapper(seed=4)
    for value in ["00123456", "20260816", "09:30", "2026/08/16", "09:00", "123.45"]:
        output = mapper.convert(value)
        assert isinstance(output, str) and len(output) == len(value)
        assert re.sub(r"[0-9]", "#", output) == re.sub(r"[0-9]", "#", value)
        assert output != value
        assert mapper.convert(value) == output
    assert mapper.convert("00123456").startswith("00")
    assert isinstance(mapper.convert(20260816), int)
    outputs = [mapper.convert(f"000{i}") for i in range(10)]
    assert len(set(outputs)) == 10
    assert all(v.startswith("000") for v in outputs)
    assert all(a != b for a, b in zip([f"000{i}" for i in range(10)], outputs))


def test_dates_times_boolean_and_duration():
    mapper = main.Mapper(seed=7)
    for value in [datetime(2024, 2, 29, 12, 30, 45), time(23, 59, 59, 123000),
                  timedelta(hours=25), True, False]:
        result = mapper.convert(value)
        assert type(value) is type(result)
        assert result != value
        assert mapper.convert(value) == result


def test_fractional_times_are_injective_including_zero():
    mapper = main.Mapper(seed=8)
    values = [time(12, 0, 0, n) for n in range(10000)]
    outputs = [mapper.convert(v) for v in values]
    assert len(set(outputs)) == len(values)
    assert all(type(v) is time for v in outputs)
    assert all(mapper.convert(v) == result for v, result in zip(values, outputs))


def test_blanks_zeros_internal_empty_rows_and_trailing_style(tmp_path):
    source = tmp_path / "in" / "source.xlsx"
    source.parent.mkdir()
    book = Workbook()
    sheet = book.active
    sheet.append(["列A", None, "列C"])
    sheet.append([0, None, False])
    sheet.append([None, None, None])
    sheet.append([" ", None, "文字"])
    sheet["Z100"].number_format = "0.00"
    book.create_sheet("空シート")
    book.save(source)
    main.run(request(), tmp_path)
    dummy = load_workbook(tmp_path / "out" / "dummy_data.xlsx")
    assert dummy.worksheets[0].max_row == 4
    assert dummy.worksheets[0].max_column == 3
    assert all(c.value is None for c in dummy.worksheets[0][3])
    overview = load_workbook(tmp_path / "out" / "data_overview.xlsx")
    assert list(overview["シート概要"].values)[1][1:] == (3, 3, 4, 5)


def test_metadata_not_copied_and_headers_are_literal(tmp_path):
    source = fixture_file(tmp_path)
    book = load_workbook(source)
    book.properties.creator = "SECRET_AUTHOR"
    sheet = book.worksheets[0]
    sheet["A1"] = "=HEADER_AS_TEXT"
    sheet["A1"].data_type = "s"
    sheet["A2"].comment = Comment("SECRET_COMMENT", "SECRET_AUTHOR")
    sheet["A2"].hyperlink = "https://invalid.example/SECRET_LINK"
    book.save(source)
    main.run(request(), tmp_path)
    dummy = load_workbook(tmp_path / "out" / "dummy_data.xlsx")
    assert dummy.worksheets[0]["A1"].data_type == "s"
    assert dummy.worksheets[0]["A1"].value == "=HEADER_AS_TEXT"
    for name in main.OUTPUTS[:2]:
        with ZipFile(tmp_path / "out" / name) as archive:
            payload = b"".join(archive.read(n) for n in archive.namelist())
            assert b"SECRET_" not in payload
            assert b"<f>" not in payload


def test_formulas_require_cached_result_and_emit_no_partial_files(tmp_path):
    source = fixture_file(tmp_path)
    book = load_workbook(source)
    book.worksheets[0]["G2"] = "=1+1"
    book.save(source)
    with pytest.raises(main.UserError, match="再計算"):
        main.run(request(), tmp_path)
    assert not list((tmp_path / "out").glob("*.xlsx"))


def test_cached_formula_is_replaced_as_value(tmp_path):
    source = fixture_file(tmp_path)
    book = load_workbook(source)
    book.worksheets[0]["G2"] = "=1+1"
    book.save(source)
    with ZipFile(source) as archive:
        parts = {n: archive.read(n) for n in archive.namelist()}
    parts["xl/worksheets/sheet1.xml"] = parts["xl/worksheets/sheet1.xml"].replace(b"<f>1+1</f><v></v>", b"<f>1+1</f><v>2</v>")
    with ZipFile(source, "w", ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    main.run(request(), tmp_path)
    dummy = load_workbook(tmp_path / "out" / "dummy_data.xlsx")
    assert dummy.worksheets[0]["G2"].data_type == "n"
    assert dummy.worksheets[0]["G2"].value != 2


@pytest.mark.parametrize("scenario", ["missing", "bad_extension", "corrupt", "empty", "header_only", "excel_error", "header_formula", "outside", "long_number"])
def test_user_correctable_inputs(tmp_path, scenario):
    source = fixture_file(tmp_path)
    req = request()
    if scenario == "missing":
        req["inputs"] = []
    elif scenario == "bad_extension":
        renamed = source.with_suffix(".pdf")
        source.rename(renamed)
        req["inputs"][0]["path"] = "in/source.pdf"
    elif scenario == "corrupt":
        source.write_bytes(b"not excel")
    elif scenario == "outside":
        req["outdir"] = "../escape"
    else:
        book = Workbook()
        sheet = book.active
        if scenario != "empty":
            sheet.append(["項目"])
        if scenario == "excel_error":
            sheet.append(["#VALUE!"])
        if scenario == "header_formula":
            sheet["A1"] = "=1+1"
            sheet.append([1])
        if scenario == "long_number":
            sheet.append([1234567890123456])
        book.save(source)
    before = hashlib.sha256(source.read_bytes()).digest() if source.exists() else None
    with pytest.raises(main.UserError):
        main.run(req, tmp_path)
    if source.exists():
        assert hashlib.sha256(source.read_bytes()).digest() == before
    assert not list((tmp_path / "out").glob("*.xlsx"))


def test_cli_one_json_line(tmp_path, monkeypatch, capsys):
    fixture_file(tmp_path)
    reqpath = tmp_path / "request.json"
    reqpath.write_text(json.dumps(request()), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["main.py", "--request", str(reqpath)])
    assert main.main() == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["status"] == "ok"


def test_routing_examples_and_suggestions_match():
    metadata = json.loads((Path(main.__file__).parent / "tool.json").read_text(encoding="utf-8"))
    texts = metadata["routing"]["examples"] + [x["content"] for x in metadata["routing"]["suggestions"]]
    for text in texts:
        assert any(re.search(pattern, text) for pattern in metadata["routing"]["patterns"])
