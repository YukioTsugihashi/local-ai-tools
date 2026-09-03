"""
Excelファイルのシート内に指定された三つの票が含まれているかを確認するツール。

使い方(リポジトリ直下で。対象ファイルは input/ に置いてもらう):
  text-processing-bridge\\.venv\\Scripts\\python.exe tools\\check_sheet_presence.py "input\\R8第1四半期 科別分析.xlsm"

- 入力ファイルには一切書き込まない
- 結果は標準出力に出力
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

# PowerShell経由(既定cp932)だと日本語出力が化けるため、常にUTF-8で出す
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Excelファイルのシート内に指定された三つの票が含まれているかを確認"
    )
    parser.add_argument("path", help="対象のExcelファイル(.xlsx / .xlsm / .xls)")
    args = parser.parse_args()

    excel_path = Path(args.path)
    if not excel_path.exists():
        raise SystemExit(f"ファイルが見つかりません: {excel_path}")

    # 出力先は output/ に固定する。入力(input/)と結果を混ぜない。
    output_dir = Path("output")

    xls = pd.ExcelFile(excel_path)
    print(f"ファイル: {excel_path}")
    print("検出されたシート:")
    
    # 指定された三つの票のキーワードリスト
    target_keywords = [
        "入院関係の実績",
        "外来関係の実績", 
        "医業収入（入外合算）実績"
    ]
    
    # シート名に含まれるキーワードを検索するために準備
    found_sheets = []  # 見つかったシート名のリスト
    missing_sheets = []  # 見つからないシート名のリスト
    
    for i, sheet_name in enumerate(xls.sheet_names, 1):
        print(f"  {i}. {sheet_name}")
        
        # シート名にキーワードが含まれているかを確認
        for keyword in target_keywords:
            if keyword in sheet_name:
                found_sheets.append((keyword, sheet_name))
                break  
        else:
            # いずれのキーワードも見つからなかった場合は未検出と判断
            missing_sheets.append(sheet_name)
    
    print("\n--- 結果 ---")
    print("指定された三つの票が含まれているかどうかを確認:")
    
    for keyword in target_keywords:
        found = any(kw == keyword for kw, _ in found_sheets)
        if found:
            print(f"  ✓ {keyword}: 見つかりました")
        else:
            print(f"  ✗ {keyword}: 見つかりませんでした")
    
    if missing_sheets:
        print("\n--- 以下に含まれないシート名 ---")
        for sheet_name in missing_sheets:
            print(f"  - {sheet_name}")
    
    # 三つの票がすべて見つかれば成功
    all_found = all(any(kw == keyword for kw, _ in found_sheets) for keyword in target_keywords)
    if all_found:
        print("\n✓ 全ての票が見つかりました。")
    else:
        print("\n✗ 要件を満たす票が見つかりません。")


if __name__ == "__main__":
    main()