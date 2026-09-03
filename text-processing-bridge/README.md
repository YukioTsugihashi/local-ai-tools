# 文章処理ブリッジ(text-processing-bridge)

議事録づくりのための文章処理を受け持つローカルサービス(port 8008)。
**要約・整形・議事録化・ケバ取り・指示修正**を提供し、Open WebUI の
「文章処理」モデルから呼ばれる。

**Dify は使っていない。** 2026-08-14 に完全撤去し、Windows 上の FastAPI が
ローカルLLM(Ollama)を直接呼ぶ形になった。

## 動かし方

ハブ(8010)と一緒に、リポジトリ直下の起動スクリプトで立ち上がる。

```powershell
text-processing-bridge\.venv\Scripts\python.exe tools\start_local_services.py
```

Windows のログオン時にタスク `minutes-pipeline-local-services` が自動実行するため、
通常は手で起動しない。**待ち受けは `127.0.0.1` だけ**で、院内LANの他端末からは届かない
(TOOL_PACKAGE_DECISIONS.md の D-16)。Open WebUI はコンテナから
`host.docker.internal:8008` で呼ぶが、Docker Desktop がそれを折り返しへ中継するため届く。

文章処理専用の画面は `http://localhost:8008/text-tools`。

## 入口

| 経路 | 用途 |
|---|---|
| `GET /health` | 生きているか |
| `GET /version` | 版の確認 |
| `GET /text-tools` | 文章処理の専用画面 |
| `POST /v1/text/summarize` | 要約 |
| `POST /v1/text/rewrite` | 整形 |
| `POST /v1/text/minutes` | 議事録化 |
| `POST /v1/text/kebatori` | ケバ取り |
| `POST /v1/text/refine` | 指示にそった修正 |
| `POST /v1/render/export` | Word/Excel などへの書き出し |

## 設定

`.env.example` を `.env` へ複製して使う(`.env` は Git 管理外)。

| 変数 | 意味 |
|---|---|
| `OLLAMA_BASE_URL` | ローカルLLMの場所。コンテナから呼ぶ形のため既定は `host.docker.internal` |
| `BRIDGE_LLM_MODEL` | 使うモデル |
| `LLM_TIMEOUT_SECONDS` | LLM呼び出しの制限時間 |
| `BRIDGE_HOST` / `BRIDGE_PORT` | FastAPI の待ち受け |
| `LONG_TEXT_THRESHOLD_CHARS` | この文字数を超えるLLM処理は自動で分割する(既定8000) |
| `LONG_TEXT_CHUNK_CHARS` | 分割時の1ブロックの文字数(既定6000) |
| `LONG_TEXT_OVERLAP_CHARS` | 分割の重なり(既定500) |

分割は段落・行・文末の境界でだけ行い、要約と議事録は部分結果を統合パスで一本化する。

## 動作確認

```powershell
Invoke-RestMethod http://127.0.0.1:8008/health

$body = @{ text = "えー 本日の会議を開始します。確認します。確認します。"; profile = "default" } |
  ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8008/v1/text/kebatori `
  -ContentType "application/json; charset=utf-8" `
  -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

## テスト

```powershell
text-processing-bridge\.venv\Scripts\python.exe -m pytest -q          # ブリッジ側
text-processing-bridge\.venv\Scripts\python.exe -m pytest ..\tools -q  # ツール側
```

## ケバ取りの規則を変える

`config/kebatori.yaml` の対象プロファイルを編集し、
タスク `minutes-pipeline-local-services` を再起動すると読み直される。

## 関連文書

- `AGENTS.md` — リポジトリ全体の約束事
- `.kilo/rules/02-map.md` — どのファイルが何をするか
- `TOOL_PACKAGE_DECISIONS.md` — 追加ツール受入の設計判断(この橋渡しの外側)
