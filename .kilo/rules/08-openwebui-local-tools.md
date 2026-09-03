# Open WebUIからKiloローカルツールを使う標準接続

Windows上で動く既存KiloツールをOpen WebUIから利用する依頼では、処理本体を
Open WebUI Pipeや文章処理ブリッジへ再実装しない。次の既存経路を優先する。

```text
Open WebUI
  → tools/open_webui_*_pipe.py            操作判定・添付読込・HTTP送受信・結果の再添付
  → http://host.docker.internal:8010
  → local_tool_bridge/app.py              受付。入口は /health・/v1/tools・/v1/run の3つ固定
  → local_tool_bridge/connectors/*.py     1ツール1ファイルの宣言。渡すだけで処理は持たない
  → tools/*.py                            処理本体(実データ抽出・LLM処理・検証・別名保存)
  → 処理済みファイル
  → PipeがOpen WebUIのファイルストアへ登録・添付
```

## 役割分担

- Pipe: 依頼文の操作判定、添付ファイル読込み、HTTP送受信、結果ファイルの再添付。
- `local_tool_bridge`: 受け取って `tools/` のスクリプトへ渡すだけ。**業務ロジックを持たない**。
- Kiloローカルツール(`tools/`): 実データ抽出、LLM処理、検証、ファイル加工、別名保存、再読込検証。
- 文章処理ブリッジ(port 8008): 議事録・要約・整形など文章側だけを担当する。ハブとは別サービスだがどちらもWindows上の同じプロセスで動く。

## ハブへ通すもの／通さないもの

**原則: `tools/` のスクリプトをOpen WebUIから使うものは、すべてハブ経由にする。**
Pipeの中へ処理を書いてはいけない。書くと `tools/` 側と二重実装になり、
`http://localhost:8010/` の一覧にも出ないため「何が繋がっているか」が分からなくなる。

| | 例 |
|---|---|
| **ハブへ通す**(原則) | Excelのコメント生成・読み取り・変換など、`tools/*.py` を叩けば終わるもの |
| **通さない**(唯一の例外) | 独自のAPIを持つサービス = 文章処理ブリッジ(port 8008) |

例外が文章処理だけなのは、扱うものが違うからである。

- ハブ: ファイルを受けてファイルを返す。だから接続役が数十行で済む
- 文章処理: テキストと構造化オプション(上限文字数・次第・名簿・テンプレ)を受け、
  テキストと警告・件数を返す。入口も12個ある

**「統一されていないから揃える」を理由に文章処理をハブへ移してはいけない。**
両者はWindows上の同じプロセスで動き、状態も `http://localhost:8010/` に並ぶため、
運用上の分断はすでに無い。

受付はログオン時に自動起動する(タスク `minutes-pipeline-local-services`)。
繋がっているツールは **`http://localhost:8010/` をブラウザで開けば一覧で見える**
(JSONで欲しい場合は `/v1/tools`、読み込みエラー込みなら `/v1/status`)。
起動ログは `logs/local_services.log` に出る。

## 新しいローカルツールを接続する手順

**具体的な手順と接続役の見本は `.kilo/rules/09-hub-connector.md` にある。** 要点だけ再掲する。

1. 処理本体は `tools/` のスクリプトとして作る(結果はJSON1行で標準出力へ)。
2. `local_tool_bridge/connectors/` へファイルを1枚足す。`app.py` は編集しない。
3. 対応するPipeへ操作パターン・提案チップ・結果添付を追加する。
4. Pipe内からWindowsパス、リポジトリ内Python、Kiloプラグインを直接import・subprocess実行しない。
5. Pipeから受付へは`http://host.docker.internal:8010`を使う。`localhost`は使わない。
6. 受付の単体テスト、Pipe回帰テスト、Open WebUIコンテナからの疎通を確認する。
7. `open_webui_deploy.py`のdry-run後に`--apply`し、登録内容一致と実ファイルE2Eを別々に確認する。

## 維持する条件

- `data/`と`.env`は読まない・変更しない。
- 利用者の原本は上書きせず、`work/`の作業コピーと`output/`の別名成果物を使う。
- 既存Pipeへ追加する場合、登録IDと既存操作を維持する。
- Kilo CLIが失敗した場合、成功したふりをせずHTTPエラーとしてPipeへ返す。
- Pipe登録成功だけで完了にせず、ローカルツール実行と成果物再添付まで確認する。

## Excelコメント接続の既存実例

- Pipe: `tools/open_webui_excel_analysis_pipe.py`の`comment`操作。
- 受付: `POST http://host.docker.internal:8010/v1/run`(旧`/v1/excel/comment`も互換で受ける)。
- 接続役: `local_tool_bridge/connectors/excel_comment.py`(35行の宣言のみ)。
- 処理本体: `tools/excel_comment_build.py`。表示中の全コメント対象シートを一括処理する。
- 手動起動(自動起動が止まっているとき):
  `text-processing-bridge/.venv/Scripts/python.exe tools/start_local_services.py`

Open WebUIとの接続依頼で既存ローカルツールがある場合、まずこの方式を採用できるか調べる。
採用できない具体的理由が確認できた場合だけ、別の経路を検討する。
