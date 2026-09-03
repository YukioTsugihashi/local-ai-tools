# 追加ツールの作り方(オンラインAI向け仕様書)

このリポジトリのシステムへ、**新しい業務ツールを追加するための決まり**です。
ChatGPT・Codex・Claude などがこの文書を読んで、規格どおりのツールを作ります。

作ったツールは1ファイル(`.localtool`)にまとめられ、USB等で
**インターネットから遮断された院内PC**へ持ち込まれます。持ち込み先では
機械的な検査を通ったものだけが接続されます。**人が手直しできる前提はありません。**

まず [example_line_count/](example_line_count/) を読んでください。動く見本です。
新しいツールは、そのフォルダを丸ごと写して中身を書き換えるのが一番確実です。

---

## 0. 最初に知っておくこと

- **既存のコードを変更しないでください。** 追加するのは `tool-packages/<あなたのツールid>/` だけです
- **実データ・個人情報・実物の帳票を絶対に入れないでください。** サンプルは生成スクリプトで作ります
- 院内PCはオフラインです。**インターネットからライブラリを取ってくることはできません**
- 検査に落ちたパッケージは接続されません。落ちた理由は日本語で返ります

## 1. フォルダの形

```
tool-packages/<tool_id>/
├─ tool.json               外側の契約の宣言(必須)
├─ main.py                 処理本体(必須)
├─ README.md               利用者と管理者向けの説明(必須)
├─ tests/
│  └─ test_*.py            単体テスト(1つ以上必須)
├─ samples/
│  ├─ make_sample.py       合成サンプルの生成器(必須)
│  └─ smoke_request.json   スモークで使う依頼文(必須)
└─ resources/              任意。ひな型など(実データ禁止)
```

- **`manifest.sha256` は自分で書かないでください。** 固めるときに自動生成されます
- パスは **ASCII のみ**(英数と `. _ - /`)。日本語はファイルの**中身**には自由に使えます
- **`.pyc .pyd .dll .exe .bat .cmd .ps1 .pth` は同梱できません**(検査で落ちます)

## 2. 起動と返却の契約

### 起動

ツールは必ずこの形で呼ばれます。引数はこれだけです。

```
python main.py --request <request.json のパス>
```

`request.json` の中身:

```json
{
  "contract": 1,
  "instruction": "利用者が書いた依頼文そのまま",
  "inputs": [
    { "id": "in-1", "role": null, "path": "in/in-1_資料.txt", "filename": "資料.txt" }
  ],
  "outdir": "out",
  "config": {}
}
```

- `path` と `outdir` は **request.json のある場所からの相対パス**です。
  絶対パスを自分で組み立てないでください
- 複数ファイルを受け取るツールでは `inputs` が増えます。
  **順番やファイル名で役割を決めないでください。** 役割が要るなら `role` を見ます
  (`role` は利用者が明示的に指定したときだけ入ります)

### 添付ファイルの読み取り(`toolpack_textio`)

**添付ファイルを自分で `open` しないでください。** 読み取りはコアが持っています。

```python
import toolpack_textio  # 実行時にコアが用意します。同梱不要・依存の宣言も不要

try:
    text, filename = toolpack_textio.read_request_input(request, job_dir)
except toolpack_textio.UnreadableFile as error:
    # 利用者が直せる文面(理由と直し方)が入っています。そのまま返してよい
    print(json.dumps({"status": "user_error", "message": str(error),
                      "files": [], "skipped": [], "notes": []}, ensure_ascii=False))
    return 0
```

読める形式:

| 種類 | 拡張子 |
|---|---|
| そのまま文字 | `.txt` `.text` `.md` `.markdown` `.csv` `.tsv` `.log` `.json` `.yaml` `.yml` `.ini` `.rst` |
| 字幕・文字起こし | `.vtt` `.srt`(時刻と連番は落とします) |
| しるし付き | `.html` `.htm` `.xml`(タグは落とします) |
| Office | `.docx` `.pptx` `.xlsx` `.xlsm` `.xls` |

読めない形式(`.pdf` `.doc` `.rtf` など)は、**何の形式で・どうすれば読めるか**を
含む `UnreadableFile` になります。拡張子を付け替えたファイルも中身で見分けます。

- 文字コードの判定(UTF-8 / cp932 / UTF-16 など)も中で行います
- `toolpack_textio` が使うライブラリを `requires.packages` へ書く必要はありません。
  **自分のコードが直接 import するものだけ**を書いてください
- ファイルの中身そのもの(Excelの書式や画像など)が要るツールは、
  これを使わず `inputs[..]["path"]` を直接開いてかまいません
  (例: Excel比較グラフは `openpyxl` でブックとして開きます)

`inputs.accepts` には、そのツールが**実際に扱える**拡張子だけを書いてください。
文字として読めればよいツールなら、上の一覧をそのまま書けます。

### 返却

**標準出力へJSONを1行だけ**出します。それ以外は何も出力しないでください。

```json
{"status": "ok", "message": "…", "files": ["結果.xlsx"], "skipped": [], "notes": []}
```

| 項目 | 意味 |
|---|---|
| `status` | `"ok"`(成功)または `"user_error"`(利用者が直せる失敗) |
| `message` | 利用者へ見せる文章 |
| `files` | 成果物。**`outdir` からの相対パス** |
| `skipped` | **利用者の対処が要るもの**。処理できなかったものは必ずここへ |
| `notes` | **対処が要らない補足**。`skipped` と混ぜないでください |

`skipped` と `notes` を混ぜると、成功したのに失敗したように見えます。
実際にその事故が起きたので、分けることが決まりになっています。

**進捗や本文を `print` しないでください。** 標準出力はこのJSON1行だけです。
失敗時の出力はログへ流れるため、**氏名や本文を出力してはいけません**。

### 失敗の伝え方

| 状況 | やり方 |
|---|---|
| 成功 | `status: "ok"` |
| 一部だけ処理できなかった | `status: "ok"` + `skipped` に理由 |
| 利用者が直せる失敗(シートが無い等) | `status: "user_error"` + `message` に対処 |
| ツール内部の異常 | 例外を投げてそのまま落ちる(0以外で終了) |

## 3. できないこと(実行時に強制されます)

院内PCでは、ツールは**隔離された子プロセス**で動きます。次は監査フックと
Windowsの機能で実際に止められます。「たぶん大丈夫」は通りません。

| 禁止 | 補足 |
|---|---|
| `outdir` の外への書き込み | OSレベルでも拒否されます |
| 入力ファイルの書き換え | 読み取り専用として扱ってください |
| 許可された場所以外の読み取り | 読めるのは request.json / 入力 / 自分のパッケージ / Python環境だけ |
| 子プロセスの起動 | `subprocess` `os.system` などは使えません |
| 通信 | 既定で全面禁止(下記のOllamaだけ例外) |
| リンクの作成 | シンボリックリンク・ジャンクション・ハードリンク |
| `ctypes` など低水準API | 検査でも落ちます |

**リポジトリの他のコードを import しないでください。** ツールは自分の
パッケージの中だけで完結させます(パッケージ内の兄弟モジュールは import できます)。

### ローカルLLM(Ollama)を使う場合

`tool.json` で宣言したときだけ、`127.0.0.1:11434` への通信が許されます。

```json
"permissions": { "ollama": true },
"requires": { "models": ["gemma4:26b"] }
```

- 使うモデル名を必ず書いてください(モデルが消えたときに気づくためです)
- 名前解決は `127.0.0.1` にしか使えません。外部DNSへは出られません

## 4. 使えるライブラリ

**院内PCに入っているものだけ**です。`requires.packages` へ `名前==版` で宣言します。
一覧に無いものを書くと検査で落ちます。

主なもの: `openpyxl==3.1.5` / `python-docx==1.2.0` / `python-pptx==1.0.2` /
`pandas==3.0.5` / `matplotlib==3.11.1` / `matplotlib-fontja==1.1.0` /
`XlsxWriter==3.2.9` / `xlrd==2.0.2` / `beautifulsoup4==4.15.0` / `PyYAML==6.0.2`

正確な一覧は `text-processing-bridge/requirements.txt` と
`text-processing-bridge/requirements-office.txt` を見てください。

> **日本語のグラフを描くなら `matplotlib-fontja` を必ず宣言してください。**
> 隔離環境ではシステムフォントを読めないため、これが無いと日本語が豆腐になります。

## 5. tool.json の書き方

```json
{
  "schema_version": 1,
  "id": "duty_summary",
  "version": "1.0.0",
  "display_name": "当直表集計",
  "summary": "当直回数を診療科別に集計する",
  "inputs":  { "accepts": [".xlsx"], "min": 1, "max": 1 },
  "outputs": { "produces": [".xlsx"], "may_be_empty": false },
  "requires": { "core_api": ">=1,<2", "packages": ["openpyxl==3.1.5"], "models": [] },
  "permissions": { "ollama": false },
  "timeout_seconds": 300,
  "routing": {
    "label": "当直表集計",
    "patterns": ["当直|集計"],
    "examples": ["当直表を集計して"],
    "suggestions": [
      { "title": "当直表集計", "subtitle": "回数を数える", "content": "当直表を集計して" }
    ]
  },
  "smoke": {
    "make_sample": "samples/make_sample.py",
    "request": "samples/smoke_request.json",
    "expect": { "status": "ok", "files_min": 1, "suffixes": [".xlsx"] }
  }
}
```

| 項目 | 決まり |
|---|---|
| `id` | 英小文字・数字・`_` で3〜32文字。**フォルダ名と一致させる** |
| `version` | `1.0.0` の形 |
| `display_name` | 利用者に見せる名前。**承認状態を書かないでください**(院内側が付けます) |
| `timeout_seconds` | 1〜1500。既定は300 |
| `routing.patterns` | 利用者の依頼文を判定する正規表現。1つ以上 |
| `routing.examples` | 依頼文の例。**必ず `patterns` のどれかに一致させる** |
| `routing.suggestions` | 画面に出すボタン。`content` も `patterns` に一致させる |
| `smoke.expect` | 合成サンプルで動かしたときの期待値 |

依頼例やボタンの文が `patterns` に一致しないと、利用者がそう頼んでも
ツールへ届きません。**検査で落とします。**

## 6. 単体テストと合成サンプル

- `tests/test_*.py` を1つ以上入れてください。受入時に**隔離環境で実行**されます
- **実データを使わないでください。** テストの中で作った文字列やファイルだけを使います
- `samples/make_sample.py` は本体と同じ契約で動きます。
  作ったサンプルを `files` へ載せてください。受入側がそれを本体への入力にします

## 7. 提出のしかた

1. `tool-packages/<tool_id>/` を作る(**ここ以外は変更しない**)
2. Pull Request を出す。CI が検査を回します
3. 検査が通ったら、院内の担当者が次で1ファイルに固めます

```
python tools/toolpack_pack.py tool-packages/<tool_id>
```

固めるときにも検査が走るので、**通らないものは出力されません**。

## 8. 追加されたあと

- 追加ツールは**モデル一覧へ独立したモデルとして出ます**
- 新規は必ず **`【未承認】`** が付きます。正式運用にするには院内の承認が要ります
- 何か問題があれば、その場で取り消され、既存のツールには影響しません

## 9. よくある落とし方

| 落ちる理由 | 直し方 |
|---|---|
| 標準出力にJSON以外が混ざった | `print` を消す。デバッグ出力は残さない |
| `files` に `outdir` の外を書いた | 相対パスで、`outdir` に作ったファイルだけを載せる |
| 依頼例が判定語に一致しない | `patterns` か `examples` のどちらかを直す |
| 使えないライブラリを宣言した | requirements の一覧にあるものへ置き換える |
| 実データやバイナリを同梱した | 生成スクリプトへ置き換える |
| ファイル名に日本語を使った | パスはASCIIにする(中身の日本語は問題なし) |
| `manifest.sha256` を手で書いた | 消す。固めるときに自動生成される |
