# Open WebUI を更新するときの手順と危険箇所

現行: **0.9.6**(`ghcr.io/open-webui/open-webui:main`、2026-06-02ビルド)

更新は「動いているものを触る」作業である。ここに書いた順で行い、
各段階で止まれるようにしておく。

---

## 何が壊れうるか

Pipe/Tool は Open WebUI の**内部Python API**を直接呼んでいる。
公開APIではないので、本体の更新で予告なく移動・改名されうる。

| 依存先 | 使っている場所 | 壊れたときの症状 |
|---|---|---|
| `open_webui.models.users.Users` | 全Pipe/Tool の `_store_file` | ファイル添付が実行時に失敗 |
| `open_webui.routers.files.upload_file_handler` | 同上 | 同上 |
| `open_webui.models.files.Files` | 添付ファイルの読み込み | 添付Excelを読めない |
| `open_webui.storage.provider.Storage` | 同上 | 同上 |
| `starlette.datastructures.UploadFile / Headers` | 同上 | 同上 |
| `files` / `chat:message:files` イベント | 結果ファイルの添付 | **複数添付が1件に潰れる**(自動検知できない) |
| `/api/v1/files/{id}/content?attachment=true` | 添付リンク | ダウンロードできない |
| `/api/v1/functions` `/api/v1/tools` | `open_webui_deploy.py` | 登録・更新が失敗(すぐ気づける) |

**登録・デプロイは成功するのに実行時だけ落ちる**のが厄介な点。
Pipeはコンテナ内で実行されるため、外からは正常に見える。

`upload_file_handler` の引数変更には既に耐性がある
(`inspect.signature` で渡す引数を絞っている)。壊れるのは「移動・改名」のとき。

---

## 更新前に必ずやること

### 1. データのバックアップ（最重要）

更新するとDBが自動マイグレーションされる。**マイグレーション後は古い版へ戻せないことがある。**
イメージを戻してもDBが新しい形式のままだと起動しない。

```powershell
docker run --rm -v open-webui:/data -v D:\offline-kit\docker:/backup alpine `
  tar czf /backup/open-webui-data-$(Get-Date -Format yyyyMMdd).tar.gz -C /data .
```

現在のデータ量は約4.5GB(設定・チャット履歴・アップロード・vector_db・webui.db)。

### 2. 登録済みPipe/Toolの控え

リポジトリが正なので原則不要だが、更新前の状態を残すなら:

```powershell
python tools\open_webui_deploy.py --list
```

---

## 更新手順

```powershell
# 1. 新しいイメージを取得（インターネットが要る）
docker pull ghcr.io/open-webui/open-webui:main

# 2. 止めて作り直す（データはボリュームに残る）
docker stop open-webui; docker rm open-webui
docker run -d --name open-webui --restart unless-stopped `
  -p 3000:8080 `
  -v open-webui:/app/backend/data `
  -e OLLAMA_BASE_URL=http://host.docker.internal:11434 `
  ghcr.io/open-webui/open-webui:main
```

```powershell
# 3. 画面の操作案内(ツアー)を入れ直す
#    /static/ はイメージ側にあるため、コンテナを作り直すと消える
python tools\webui_tour.py deploy --apply
```

上記が現行コンテナの完全な再現(2026-08-14時点で `docker inspect` から確認)。
ExtraHosts・GPU割当・追加ネットワークは無し。既定と違う環境変数は
`OLLAMA_BASE_URL` の1つだけ。

---

## 更新後の確認

### 自動で見られるもの

```powershell
python tools\doctor.py
```

- **「Pipeが使うOpen WebUIの内部APIは揃っています」** ← 上の表の依存先をコンテナ内で実際にimportして確かめる。ここが異常なら添付機能が壊れている
- 「Open WebUI登録内容とリポジトリが一致」 ← 登録が飛んでいないか

### 人が押さないと分からないもの

**複数ファイルの添付**は自動検知できない。Excel分析でコメント生成を実行し、
**成果物と確認用レポートの2件が両方チャットに残るか**を必ず確認する
(`files` イベントの追記／置き換えの挙動に依存しており、
2026-08-14に実際これで1件しか残らない不具合が起きた)。

---

## 切り戻し

イメージの控えは `D:\offline-kit\docker\ghcr.io_open-webui_open-webui_main.tar`。
**2026-08-14に `docker load` で検証済み — 現行と同一のイメージIDになる。**

```powershell
docker stop open-webui; docker rm open-webui
docker load --input D:\offline-kit\docker\ghcr.io_open-webui_open-webui_main.tar
# 上の docker run をそのまま実行

# DBがマイグレーション済みで起動しない場合は、データも戻す
docker run --rm -v open-webui:/data -v D:\offline-kit\docker:/backup alpine `
  sh -c "rm -rf /data/* && tar xzf /backup/open-webui-data-<日付>.tar.gz -C /data"
```

---

## 更新するべきか

**オフライン化前が唯一の機会**である。切断後は `docker pull` ができないため、
そのときの版で固定される。

- 更新する場合: 上の手順で、**必ずデータのバックアップを取ってから**。
  問題が出たら切り戻せる状態を作ってから触る
- 固定する場合: 閉じた環境なので、既知の動く状態を維持するのは妥当な判断。
  ただし以後、不具合修正も受け取れない

`main` は動くタグなので、`docker pull` すると**どこまで飛ぶか分からない**。
0.9.6 からの差が大きいほど内部APIの移動リスクは上がる。
慎重にいくなら、更新後に必ず `doctor.py` と複数添付の確認を行うこと。
