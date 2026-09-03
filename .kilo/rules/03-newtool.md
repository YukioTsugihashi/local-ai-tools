# 新機能(Open WebUIのツール)依頼への応じ方

新しいツール・機能の作成を依頼されたら、コードを書く前に必ず次の比較を提示し、
どちらで作るかを依頼者に質問して選ばせる。勝手に決めてはいけない。

- **Pipe**(モデル選択欄に出る):
  選べば必ず実行される。実行するかどうかをLLMは判断しない。
  「毎回・確実に実行されるべき処理」向け。既存の文章処理がこの方式
  (操作の判定も正規表現で行い、LLMに任せていない)。
  修正はこのリポジトリの tools/ の編集が必要(管理者領域)。
- **Tool**(チャットの道具):
  実行するかどうか・引数はLLMの判断に任される(呼ばれないこともある)。
  そのかわりOpen WebUIの画面から付け外し・調整がしやすい。
  「確実性より手軽さ」の道具向け。

選択を受けてから tools/ に実装する。手本:
Pipe = tools/open_webui_text_processing_pipe.py、
Tool = tools/open_webui_dify_text_processing_tool_v0.6.0.py。
編集後は py_compile で構文チェックする。

新しいファイルの決まり:
- ファイル名は tools/open_webui_<機能名>_pipe.py または _tool.py(デプロイ対象の柵)
- クラス名はちょうど `Pipe` または `Tools` にする。`class 〇〇Pipe(Pipe)` のような
  別名・継承はOpen WebUIに認識されず、デプロイが拒否する
- ファイルを会話へ添付する処理(_store_file / _attach_file)は、既存Pipeから
  そのまま写す。共通化できないため写経するしかないが、ズレると事故になる
  (1箇所だけ直した結果、Excel分析では複数添付の1件しか画面に残らなかった)。
  写経元は tools/open_webui_text_processing_pipe.py。ズレたら
  tools/test_webui_shared_blocks.py が落ちる
- ループで複数ファイルを添付するなら、必ず chat:message:files で累積全件も送る。
  Open WebUIは画面側で「置き換え」処理するため、送らないと最後の1件しか残らない
- このリポジトリの他ファイル(tools.〇〇 や他のopen_webui_*.py)をimportしない。
  PipeはOpen WebUIコンテナ内で動き、リポジトリのファイルは見えないため、
  必要な処理はファイル内に自己完結で書く(デプロイが事前に拒否する)
- 先頭docstringに `id: <小文字英数と_>` を必ず書く(Open WebUI上の登録ID。
  既存と同じIDなら更新、新しいIDなら新規作成になる)
- Open WebUIコンテナ内で動くため、Windowsのパスは見えない。添付ファイルは
  __files__ のIDからファイルストア経由で読む(Excel分析パイプの実装が手本)
- __files__ は**会話全体**のファイルを渡してくる。「今回添付されたもの」ではない。
  そのまま処理すると、2回目の依頼で前のメッセージの添付まで巻き込む
  (2ファイル添付して2回頼むと4件処理される)。処理済みのファイルIDを
  会話ごとに覚えて、新しいものだけを対象にする(_pending_attachments が手本)。
  Toolの場合は __metadata__["user_message"]["files"] を先に見る(そちらが
  今回のメッセージの添付。__files__ と __metadata__["files"] は会話全体)
- Pipeでは、モデル画面に出す「提案チップ」(何ができるかのボタン)と説明文が**必須**。
  同じdocstringに書くとデプロイ時に自動でモデル設定へ登録される:
    model_description: モデル選択時に出る説明文
    suggestion: タイトル | サブタイトル | 入力欄に入る文  (1行1ボタン、1つ以上)
  この2つが無いPipeはデプロイスクリプトが登録を拒否する。エラーになったら
  docstringへ追記してから再実行する(Toolは対象外)
- チップの入力文は、そのPipeの OPERATION_PATTERNS のどれかに一致する言い回しに
  する。デプロイ時に照合され、どの操作にも一致しないチップは拒否、チップの無い
  操作は注意が表示される。操作を追加したらチップも同時に追加すること

例外: Excel・Word・PowerPointファイルそのものの分析・編集依頼は、新機能開発ではない。
Pipe/Toolの選択を質問せず、.kilo/rules/05-local-agent.md に従ってwork/へ依頼専用コードを
作り、その場で処理を完了する。再利用可能なOffice処理ツールを新しく作ってほしいと
明示された場合だけ、tools/office_excel.py などを手本にtools/へ実装する。
この場合もPipe/Toolの比較は不要で、通常のローカルコマンドラインツールとして作る。
対象ファイルは input/ に置いてもらい、結果は output/ へ書き出す。input/ のファイルは
絶対に上書きしない。
Open WebUIへの反映(登録・更新)は次の手順で自分で行える(コードの編集だけでは完了しない)。

Windows上の既存KiloツールをOpen WebUIから利用する場合は、Pipeへ処理本体を
書き直さず、`.kilo/rules/08-openwebui-local-tools.md`の`local_tool_bridge`経路を使う。
Kiloプラグインやリポジトリ内PythonをPipeから直接import・実行してはいけない。

1. `python tools\open_webui_deploy.py --list` で登録済みIDを確認
2. `python tools\open_webui_deploy.py tools\<対象>.py` で差分を確認(dry-run)
3. 差分が意図どおりなら `--apply` を付けて反映(登録後に内容一致を自動検証)

デプロイスクリプトは接続先がlocalhostのOpen WebUI固定・対象が tools/ の
open*webui*.py 限定で、管理APIキーはスクリプトが自分で読む(表示・記録はしない)。
「認証を拒否されました」と出たら、APIキーの再設定(人の作業)を依頼者に伝える。
管理画面(Workspace → Tools / Functions)への貼り付けは予備手段。
