"""架空の2シートExcel。実データ・個人情報は含まない。"""
import argparse
from datetime import datetime, time
import json
from pathlib import Path
from openpyxl import Workbook


def create_sample(path):
    book = Workbook()
    sheet = book.active
    sheet.title = "架空データ"
    sheet.append(["患者番号", "診療科", "日付文字列", "日付数値", "日時", "時刻", "測定値", "空欄", "混在"])
    for i in range(150):
        sheet.append([f"00{100000 + i % 75}", "架空内科" if i % 2 else "架空外科", "20260816", 20260816,
                      datetime(2026, 8, 16, 9, 30), time(9, 30), 12.34 if i % 3 else 0, None,
                      "架空文字" if i % 2 else 1234])
        sheet.cell(i + 2, 5).number_format = "yyyy/mm/dd hh:mm"
        sheet.cell(i + 2, 6).number_format = "hh:mm"
        sheet.cell(i + 2, 7).number_format = "0.00"
    second = book.create_sheet("架空対応表")
    second.append(["患者番号", "診療科"])
    second.append(["00100000", "架空外科"])
    book.save(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    reqpath = Path(args.request).resolve()
    request = json.loads(reqpath.read_text(encoding="utf-8"))
    out = reqpath.parent / request["outdir"]
    out.mkdir(parents=True, exist_ok=True)
    create_sample(out / "sample.xlsx")
    print(json.dumps({"status": "ok", "message": "合成Excelを作成しました", "files": ["sample.xlsx"], "skipped": [], "notes": []}, ensure_ascii=False))


if __name__ == "__main__":
    main()
