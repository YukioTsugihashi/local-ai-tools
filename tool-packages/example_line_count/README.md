# 行数かぞえ(作成例)

追加ツールの**作り方の見本**です。実務では使いません。
新しいツールを作るときは、このフォルダを丸ごと写して中身を書き換えてください。

## 何をするツールか

ファイルを1つ受け取り、行数と文字数を数えて結果のテキストを返します。
Word(.docx)・PowerPoint(.pptx)・Excel(.xlsx / .xlsm / .xls)・字幕(.vtt / .srt)・HTML・テキスト(.txt / .md / .csv など)に対応しています。
**PDFは読めません。** 元のWordやテキストを添付してください(読めない形式のときは、その場で理由と直し方をお返しします)。

## 利用者の頼み方

- 「行数を数えて」
- 「文字数をかぞえて」

## 中身の説明

| ファイル | 役割 |
|---|---|
| `tool.json` | 外側の契約の宣言。受け取る形式・返す形式・判定語・依頼例など |
| `main.py` | 処理本体。`--request` で渡された request.json を読み、`outdir` へ書く |
| `tests/test_main.py` | 単体テスト。受入時に隔離環境で実行される |
| `samples/make_sample.py` | 合成サンプルの生成器。受入時のスモークで使う |
| `samples/smoke_request.json` | スモークで使う依頼文 |

`manifest.sha256` はこのフォルダには置きません。
`tools/toolpack_pack.py` が固めるときに自動で作ります。

## 持ち込む形にする

```
python tools/toolpack_pack.py tool-packages/example_line_count
```

`example_line_count-1.0.0.localtool` ができます。
固める前に検査が走るので、通らないファイルは出力されません。

詳しい決まりは [SPEC.md](../SPEC.md) を読んでください。
