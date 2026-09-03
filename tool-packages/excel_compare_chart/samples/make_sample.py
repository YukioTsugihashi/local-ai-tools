"""検査用の架空のExcelを作る。**実在の部署・実際の数値は使わない。**

**実物の帳票と同じ作り**にしてある。以前は1行目が見出しの素直な表で、
受入検査は通るのに実物ではまったく読めなかった(2026-08-31)。
検査が実物の作りを通らないなら、通っても意味がない。

再現している形:
  ・上に表題と注意書きがあり、見出しは 1 行目ではない
  ・年度が段になった多段見出し(「R7年度 / 入院 / 入院収入」)
  ・項目名がB列(略称)とC列(正式名)に分かれる
  ・数値の途中に、真偽値が並ぶ確認行が入る
"""

from __future__ import annotations

import argparse
import json
import os

ROWS = [
    ("内", "架空内科", 120, 138),
    ("外", "架空外科", 86, 79),
    ("小", "架空小児科", 54, 61),
    ("整", "架空整形外科", 97, 103),
    ("皮", "架空皮膚科", 42, 40),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="架空の比較用Excelを作る")
    parser.add_argument("--request", required=True)
    args = parser.parse_args()

    request = json.loads(open(args.request, encoding="utf-8").read())
    target = os.path.join(
        os.path.dirname(args.request), request["outdir"], "架空の診療科別集計.xlsx"
    )

    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "集計"

    # 上部の表題と注意書き(見出しを1行目から押し下げる)
    sheet["B2"] = "架空病院 診療科別実績(検査用のダミー)"
    sheet["B4"] = "※この数値は実在しません"

    # 多段見出し。年度は上の段に一度だけ書き、下の段に内訳を置く
    sheet["B7"] = "(1)入院関係の実績"
    sheet["D7"] = "R7年度"
    sheet["F7"] = "R8年度"
    for column in ("D", "F"):
        sheet[f"{column}8"] = "入院"
    for column, name in (("D", "入院収入"), ("E", "件数"), ("F", "入院収入"), ("G", "件数")):
        sheet[f"{column}9"] = name
    sheet["B9"] = "略称"
    sheet["C9"] = "診療科"

    for offset, (short, name, before, after) in enumerate(ROWS):
        row = 10 + offset
        sheet.cell(row=row, column=2, value=short)
        sheet.cell(row=row, column=3, value=name)
        sheet.cell(row=row, column=4, value=before)
        sheet.cell(row=row, column=5, value=before // 3)
        sheet.cell(row=row, column=6, value=after)
        sheet.cell(row=row, column=7, value=after // 3)

    # 確認行(数値ではなく真偽値が並ぶ)。ここで表が割れないことも検査に含める
    check_row = 10 + len(ROWS)
    sheet.cell(row=check_row, column=2, value="チェック")
    for column in range(4, 8):
        sheet.cell(row=check_row, column=column, value=False)

    book.save(target)

    print(json.dumps({
        "status": "ok",
        "message": "架空の集計Excelを作りました",
        "files": ["架空の診療科別集計.xlsx"],
        "skipped": [],
        "notes": [],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
