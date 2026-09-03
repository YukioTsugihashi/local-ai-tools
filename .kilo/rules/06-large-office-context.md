# Large Office file context policy

Officeファイル本体をモデルのコンテキストへ直接添付・展開しない。
Officeファイルは専用ツールから読み、標準出力には必要な部分だけを返す。

Excelは次の順序で調査する。

1. `tools/excel_reader.py PATH --index-only --no-csv` でシート索引だけを取得する。
2. `--find QUERY --no-csv` で根拠候補のセルを検索する。
3. `--sheet NAME --range A1:F20 --no-csv` で必要範囲だけを取得する。
4. 結論に使う範囲を最後に再取得し、シート名とセル座標を回答へ記載する。

一度の範囲取得は500セル以下、検索結果は50件以下とし、それ以上は分割する。
生成したCSVの本文を会話へ貼らず、成果物のパスだけを示す。
