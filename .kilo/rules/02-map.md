# コードマップ(どこに何があるか)

探索より先にこの表を見て、対象ファイルを直接開くこと。appの各ファイルは300行前後以下。

## データの流れ(構造化議事録 /v1/text/minutes_structured)

```
次第・名簿・文字起こし
  → agenda_parse.py / roster.py がパース(骨格と名簿を確定)
  → instructions.py がLLMへの指示文を生成
  → ローカルLLM(Ollama)が発言を振り分け … structured.py が2周制御
  → llm_lines.py がLLM出力(##項目/##発言/#議題 行)をパース
  → merge.py がブロック統合・重複除去・報告読み上げ落とし
  → document.py が描画用データ組み立て(緑=文字起こし由来/黄=次第由来/赤=不明)
  → render_minutes.py が docx 化(/v1/render/minutes_docx 経由)
```

## text-processing-bridge/app/

| ファイル | 責務 | 主な関数 |
|---|---|---|
| main.py | FastAPIエンドポイント定義のみ。処理本体は持たない | summarize_text, create_structured_minutes ほか |
| processing.py | 汎用処理の実行制御(長文分割・再試行・数値の欠落/捏造ガード) | _process, _run_workflow, _missing_number_tokens, _invented_number_tokens |
| structured.py | 構造化議事録パイプライン(2周抽出・記号救済・話し言葉修正) | run_structured_minutes, _extract_pass, _infer_agenda, _polish_spoken_style |
| agenda_parse.py | 次第のパースと骨格(Agenda/AgendaTopic/AgendaItem) | parse_agenda, build_inferred_agenda, _item_key |
| roster.py | 出席者名簿のパースと呼称の表記合わせ | parse_roster_categories, parse_roster, normalize_person |
| instructions.py | LLMへ渡す指示文の生成(良い例/悪い例の実例つき) | build_format_instruction, build_topic_inference_instruction |
| llm_lines.py | LLM出力の行形式パース(崩れた行の救済含む) | parse_structured_lines, parse_topic_lines |
| merge.py | ブロック統合・近似重複除去・報告読み上げ判定 | merge_chunk_results, looks_like_report, _is_duplicate |
| document.py | 描画用データ組み立て(出典マーカーの色分け) | build_document_data |
| chunking.py | 長文の段落・文境界分割 | split_text, split_text_with_overlap |
| kebatori.py | ルールベースのフィラー除去(ルールは config/kebatori.yaml) | apply_kebatori, normalize_line_endings |
| prompts.py | 操作ごとのプロンプトと生成条件(旧Difyワークフローから移設) | PromptSpec, LLM_PROMPTS, KEBATORI_DETECT |
| local_workflow.py | 操作の振り分け。旧Difyワークフローの operation-router に相当 | LocalWorkflowClient, WorkflowRunResult |
| ollama_client.py | Ollama /api/chat を1往復する薄いクライアント | OllamaChatClient, configured_model |
| config.py | 環境変数の読み込み(接続先・分割閾値・タイムアウト) | ollama_base_url, llm_timeout_seconds, long_text_threshold_chars ほか |
| models.py | リクエスト/レスポンスのPydanticモデル | StructuredMinutesRequest ほか |
| render_minutes.py | 議事録テンプレ(docx)への流し込み。行XML複製で書式を完全保持 | render, _fill_cell, _blank_header |
| export.py | 結果の配布用ファイル生成(txt/md/csv/docx/xlsx) | render_output, build_docx, build_xlsx |
| result_page.py | Toolの結果埋め込みページ(HTML)生成 | build_result_page |
| oplog.py | PIIレス運用ログ(件数・実行ID・状態のみ。本文・名簿・鍵は記録禁止)。ローテーション付き、書込失敗は本処理へ波及させない | log_event |
| archive.py | 受け取り文章と生成成果物の控えをgit管理外の `work/imports/`・`output/` へ保存 | save_import, save_output |

## テスト対応

`app/X.py` を変えたら、まず `tests/test_X.py` を回す(1:1対応)。

- main.py → test_api.py
- structured.py → test_structured_api.py
- chunking.py → test_chunking.py / kebatori.py → test_kebatori.py
- agenda_parse.py / roster.py / llm_lines.py / merge.py / document.py → 同名の test_*.py
- export.py → test_export.py / result_page.py → test_result_page.py / render_minutes.py → test_render_minutes.py
- oplog.py → test_oplog.py(本文・氏名がログへ漏れないことの検証を含む)
- archive.py → test_archive.py(保存先・ファイル名無害化・保存失敗時の継続)
- test_smoke.py: 全モジュールをimportする保険(移動漏れ・import切れを検出)
- local_workflow.py → test_local_workflow.py(操作の振り分けとLLM候補の検証) / ollama_client.py → test_ollama_client.py(障害の安全なエラー化)
- test_e2e_stub.py: LLMスタブで文字起こし→構造化→docx描画まで通す(数値保持・出典マーカー。テンプレートは合成、実テンプレ非依存)
- tools/open_webui_text_processing_pipe.py → tools/test_pipe_routing.py(ルーティング判定。ホストvenvで実行)
- tools/open_webui_excel_analysis_pipe.py → tools/test_excel_pipe_routing.py(操作判定とopenpyxl処理。ホストvenvで実行)
- tools/open_webui_help_pipe.py → tools/test_help_pipe.py(`_answer` のペイロードが ``think: false`` を含み、``num_ctx`` が資料規模(MAX_CONTEXT_CHARS)に足りること、依頼文づくりの判定・整形・リンクへの反映をOllama非接続で検証。ホストvenvで実行)
- tools/excel_reader.py・office_excel.py・office_template.py・safe_task_runner.py・safe_file_import.py・excel_comment_context.py・excel_comment_report.py・excel_comment_build.py・doctor.py・open_webui_deploy.py → tools/test_*.py(同名。ホストvenvで実行)
- local_tool_bridge/ → tools/test_local_tool_bridge.py(接続役の宣言検査と、ハブに業務ロジックを置かない境界検査)
- .kilo/plugin/*.ts → .kilo/plugin/*.test.mjs(判定ロジックの実行時テスト)。tools/test_kilo_plugin_runtime.py が pytest から node で回す(node不在ならskip)
- tools/doctor.py の開発ツール・オフライン資材チェック → tools/test_doctor.py(PATHから node/java/gcc が使えるか、取り直せない資材が揃っているか)
- open_webui_excel_upload_guard.js → webui_tour/upload-guard.test.mjs(分析Excelは抽出を止め、出席者名簿は止めないこと。議事録が名簿のテキストを使うため)
- tools/webui_static_guard.py → tools/test_webui_static_guard.py(画面カスタマイズが消えていないかの見張り。5分ごとに自動実行)
- webui_tour/tour.js・tools/webui_tour.py → tools/test_webui_tour.py(loader.jsの書き換え→取り消しが1バイト一致で戻ること、dry-runで書き込まないこと。webui_tour/tour.test.mjs も同じpytestからnodeで回す)

## app以外

| パス | 内容 |
|---|---|
| tools/open_webui_text_processing_pipe.py | Open WebUI の Pipe(モデルとして動く)。正規表現ルーティングでブリッジを呼ぶ。ファイル生成・テンプレ流し込みはブリッジの /v1/render/* に委譲 |
| tools/open_webui_dify_text_processing_tool_v0.6.0.py | Open WebUI の Tool(チャット中のLLMがfunction callingで呼ぶ)。登録名は「文章処理(ツール版)」、登録IDは dify_bridge のまま(既存の登録を壊さないため)。埋め込みページのHTMLはブリッジの /v1/render/result_page が生成 |
| tools/open_webui_excel_analysis_pipe.py | Open WebUI の Pipe(「Excel分析」モデル)。正規表現で操作を判定し、処理はすべてハブ(port 8010)へ委譲。openpyxlを使うのはシート名の取得だけ(どのシートを指すかの判断はPipeの仕事) |
| tools/test_webui_shared_blocks.py | Pipe間で写経している共通処理(_store_file/_attach_file)のズレ検知。Open WebUIのPipeは共通モジュールをimportできないため重複が消せない。1箇所だけ直すとここが落ちる |
| tools/open_webui_deploy.py | Open WebUIへのPipe/Tool登録・更新(管理API・localhost限定)。--listで一覧、無指定はdry-run差分表示、--applyで反映+検証。登録IDは各ファイルdocstringの id: 行。Kiloは実行のみ可(編集は人が行う) |
| tools/excel_reader.py | Excel全シートの一覧表示・CSV化(--csv-sheetで1シートに絞れる)に加え、範囲表示(--sheet/--range)・値検索(--find)(読み取り専用)。Open WebUIのExcel読み取りもハブ経由でこれを使う |
| tools/open_webui_transcription_refiner_tool.py | Open WebUI管理画面へ直貼りされていたToolの回収版。実行するとNameErrorになり一度も動いていなかった。役割も /v1/text/kebatori と重複するため2026-08-14に登録を解除した。記録として残すだけで、再登録しない |
| tools/check_sheet_presence.py | Excelシート内に所定の表が含まれるかの確認(読み取り専用、結果はoutput/へ) |
| tools/conftest.py | tools/ のテストを実行ディレクトリに依存させないためのsys.path設定(pre-commitはtext-processing-bridgeからpytestを起動する) |
| tools/start_local_services.py | ハブ(8010)と文章処理ブリッジ(8008)を1プロセスで起動。**既定は 127.0.0.1 で待つ**(LANから届かせない。D-16)。Docker Desktop は host.docker.internal 宛てを折り返しへ中継するので、Open WebUI からは届く。広げるには `--host` を明示。ログオン時にタスク minutes-pipeline-local-services が自動実行。--log でファイル出力 |
| tools/ollama_watchdog.py | Ollamaのコミット暴走を検知し、`--fix` で再起動する見張り(doctorは読取専用なので手を出す役はこちら)。コミット90%以上**かつ**ollama.exe 40GB以上の両方でのみ実行。15分ごとにタスク minutes-pipeline-ollama-watchdog が自動実行、記録は logs/ollama_watchdog.log |
| tools/webui_static_guard.py | Open WebUIの画面カスタマイズ(custom.css・Excelアップロードガード・操作案内)が消えていないか見張り、消えていたら戻す。`open_webui` をコンテナ内でimportすると`STATIC_DIR` が初期化されるため、doctorを走らせるたびに消えていた(2026-08-18に特定) |
| tools/doctor.py | オフライン運用の自己診断(読み取り専用)。接続・コンテナ・モデル・資材・運用ログを検査し、日本語で対処を出す。Ollamaは「接続できるか」(check_ollama_connection)と「処理が溜まっていないか」(check_ollama_backlog、Ollamaログから流量・503・DL進行を推定)を別項目で出す。Gitは「今どのブランチにいるか」(check_git)と「バックアップ先へ送れているか」(check_git_backup、remote local)を別項目で出す。障害調査は必ずこれから始める。--out output\doctor_report.txt で保存 |
| tools/excel_comment_context.py | KiloのExcelコメント生成に必要な実データ・比較結果・記入例・書込先を抽出。シート省略時は表示中の全コメント／分析内容欄を対象化。抽出できないシートは`skipped`へ退避し、残りの処理を止めない |
| tools/excel_comment_build.py | 抽出結果から検証済みコメントを組み立て、ローカルLLMで文体整形し、一括書込みまで通す。**確認用レポート(コメント一覧)は既定で作らない**(利用者判断。`--report` で作る)。`--sheet`・`--no-llm` は実物1シートのプレビュー用。Open WebUI経路の処理本体 |
| tools/excel_comment_report.py | 複数シートへ書き込んだコメント全文・貼付先リンク・参照表の静的コピーを、元ブック無変更で別の確認用xlsxへ縦積み |
| .kilo/plugin/excel-tools.ts | Kiloのexcel_*ツール実体とセッション制御。コメント本文の組み立ては持たず tools/excel_comment_build.py へ委譲する(Kilo経由とOpen WebUI経由で結果を食い違わせないため) |
| .kilo/plugin/stage-guard.ts | 複雑な変更を3〜10工程へ分割し、編集前計画・工程順序・編集後テスト・Open WebUI dry-runをツールフックで強制 |
| local_tool_bridge/app.py | Open WebUIからKiloツールを呼ぶ受付(port 8010)。入口は `/`(状態画面)・`/health`・`/v1/tools`・`/v1/status`・`/v1/run` で、ツールごとに増やさない。旧`/v1/excel/comment`は登録済みPipe互換で残置。ログオン時に自動起動(タスク`minutes-pipeline-local-services`) |
| local_tool_bridge/hub.py | ハブの骨組み。ToolSpec/RunContext/RunResultの定義、`connectors/`の自動発見(`discover`は失敗理由も返す)、`run_repo_script`(接続役に許された唯一の実行手段)、周辺サービスの生死確認(`neighbour_status`) |
| local_tool_bridge/page.py | `http://localhost:8010/` の状態表示ページ。繋がっているツールと読み込めなかった接続役を出す表示専用。オフラインで開くため外部リソースを一切読まない |
| local_tool_bridge/connectors/ | 1ツール1ファイルの接続宣言。`SPEC`を持つ.pyを置くだけで繋がる。業務ロジック禁止。現在 excel_comment(コメント生成)と excel_read(一覧・検索・範囲・CSV) |
| local_tool_bridge/installed_specs.py | **第2の供給源**。`additional-tools/registry/registry.json` の有効版を `ToolSpec` にする。`discover()` が毎回読み直すのでサービス再起動なしで反映される。壊れた項目はそのパッケージだけ隔離。名前が衝突したら**既存の接続役を残して追加側を拒否**。実行は `tools/toolpack_runner.py` へ委譲し、業務ロジックも実行手段も持たない |
| local_tool_bridge/job_lock.py | 実行を1件ずつに直列化するプロセス間ロック(Windows名前付きミューテックス)。ハブの実行と管理アプリのスモークが同じ順番待ちに並ぶ。作れない環境ではプロセス内ロックへ落ちるが、`cross_process` でその事実が見える |
| OPEN_WEBUI_UPGRADE.md | Open WebUI更新の手順と危険箇所。Pipeが依存する内部Python APIの一覧、データバックアップ、現行コンテナの完全な再現コマンド、切り戻し(控えtarは検証済み) |
| .kilo/rules/08-openwebui-local-tools.md | Open WebUI ↔ Windows上Kiloツールの全体構成 |
| .kilo/rules/09-hub-connector.md | ハブへツールを繋ぐ具体手順と接続役の見本 |
| tools/office_excel.py | Excelのセル表示・単一変更・複数シート一括変更。対象セルXMLだけを差し替えて数式キャッシュとVBAを保持。`set`/`set-batch` とも templates/gui のひな型Excel(comment_template 名前付きセル)の書式を書き込み先へ移植する。セル内の部分書式(見出しだけ太字)も移す。当てるのは生成コメントだけで、罫線・列幅は移植しない |
| webui_tour/ | Open WebUIの画面へ重ねる操作案内(ツアー)の正本。tour.js が実行本体、tours/*.json が台本。外部リソースを一切読まない。入口は画面の隅の「?」ボタン(押すと台本の一覧が開く。URLの `#tour=<id>` でも開始可)。覆いは pointer-events:none なので案内中も画面は普通に操作できる。自動クリックはしない |
| tools/webui_tour.py | 画面カスタマイズ(操作案内・custom.css・Excelガード)の検査・確認・配備。差し込み先は **index.html**。**static/ は使わない**(open_webui の import のたびに初期化される。doctorの内部API確認がその引き金だった)。`check`/`preview`(作り物の画面で見え方確認)/`deploy`/`remove`。無指定はdry-run、`--apply` で反映。**コンテナを作り直すと静的ファイルは消えるので deploy をやり直す** |
| tools/open_webui_help_sync.py | Open WebUIのナレッジ「院内ツールヘルプ」へ運用ドキュメントとコード本体を同期する。中身のsha256を `logs/help_sync_state.json` に持ち、**変わったファイルだけ入れ直す**(`--full` で全件、`--reset` で作り直し)。対象から外れたファイルはナレッジから落とす。旧方式のモデル hospital_help が残っていたら消す。data/・templates/・.envは絶対に載せない。**ログオン5分後と2時間ごとにタスク `minutes-pipeline-help-sync` が自動実行**(`--log` で `logs/help_sync.log` へ記録)。すぐ反映したいときだけ手で `--apply` |
| tools/open_webui_help_pipe.py | 使い方ヘルプのPipe。ナレッジをbge-m3で自分で検索し、その結果だけをgemma4へ渡して答えさせる。Open WebUIのナレッジ機能に任せるとモデルが検索ツールを呼んだまま止まり回答が空になるため、検索を確定的に行う。担当の振り分け(`ROUTES`)も正規表現で決め、作業依頼には「文章処理」「Excel分析」への切替リンクを回答の先頭に出す。「どう頼めばいいか」と聞かれたとき(`REQUEST_TEXT_PATTERN`)は、資料をもとに依頼文を作って切替リンクの入力欄へ入れる(`_clean_request_text`)。`kilo:` で始まる質問は利用者が行き先を宣言したものとして、Kiloへ貼る依頼文をLLMに書かせ、機械で検めてから出す(`_verify_kilo_request`。資料の例文の丸写しと、質問にも検索結果にも無いファイル名の創作を落とす。3回作り直して通らなければ、用件と検索結果を並べただけの `_kilo_request` へ退避する)(語から意図を当てる方式は誤判定が多く取り下げた。書式・置き場所の質問は宣言があっても除外) |
| tools/office_template.py | Excel/Word内のfield・threshold目印を置換するGUI編集テンプレート試作。`minutes` は唯一の実装 `render_minutes.py` を呼び、実物Wordをプレビューする。`check`/`preview`/`deploy` はWordで書式編集した議事録テンプレートの検証→サンプル確認→コンテナ反映 |
| tools/office_word.py | Wordの本文表示・文字列置換・新規作成 |
| tools/office_ppt.py | PowerPointの文言表示・置換・新規作成 |
| tools/safe_task_runner.py | Kiloが生成した依頼専用Pythonを、読み書き範囲・通信・子プロセスを制限して実行。**追加ツール用ではない**(そちらは toolpack_runner。この2つは別物で、混ぜない) |
| tools/toolpack_verify.py | 持ち込まれた `.localtool`(ZIP)の検査の**唯一の正本**。展開前にエントリ名(トラバーサル・予約名・ADS・大小文字衝突・末尾空白等)・暗号化・リンク・禁止バイナリ・上限(500件/100MB/200MB)を見て、`extractall` を使わず1件ずつ展開する。manifest.sha256 は自分自身を除く全列挙と突合し、欠け・余り・不一致を別々に報告。tool.json は標準ライブラリだけで検証(依存は requirements 2本+wheelhouse から作る許可リストと照合)。実行され得る全PythonをAST検査するが、難読化は抜けられる(実行時の閉じ込めは toolpack_runner の仕事) |
| tools/toolpack_store.py | `additional-tools/`(incoming/staging/installed/rejected/logs/registry)の管理。**registry.json だけが有効化スイッチ**で、ディレクトリの存在は有効を意味しない。staging→installed は `os.rename`、registry 更新は一時ファイル+`os.replace` で原子的。**孤立版は自動削除せず列挙のみ**(doctor が報告)。rejected へは診断だけ残し、失敗パッケージの院内コピーは消す |
| tools/toolpack_contract.py | 追加ツールの起動・返却契約。request.json(contract/instruction/inputs[id・role・相対path]/outdir/config)の生成と、結果JSON(status/message/files/skipped/notes)の検証。**files は outdir 内・通常ファイル・許可拡張子・20件・200MB・ハードリンク/ジャンクション拒否**まで見る(「読んで返す」持ち出しを止める最後の砦) |
| tools/toolpack_child.py | 追加ツールを走らせる子プロセスの**固定ブートストラップ**(コア側にありパッケージから差し替えられない)。**子プロセスの中で最初に監査フックを登録**してから main.py を読む(親のフックでは子を監視できない)。読み取り許可リスト・outdir/tmp/mpl 限定の書込・リンク作成遮断・子プロセス禁止・通信禁止(ollama宣言時のみ127.0.0.1:11434)。nulデバイスだけは無害なので許可(pytestの出力捕捉が開く) |
| tools/toolpack_state.py | 追加ツールが**いま実際にどうなっているか**を問い合わせる(稼働中のハブ 8010 と Open WebUI)。同じプロセスで `hub.discover()` を呼んでも、**本番のサービスが止まっていても『認識している』**と出るため、実際にHTTPで聞く。**問い合わせられなかったときは None** を返し、「見に行けなかった」と「見に行ったが無かった」を混ぜない(混ぜると、動いているものを消したり、消えていないものを消えたと報告する) |
| tools/toolpack_backup.py | `additional-tools/` をUSB等へ書き出す・戻す(save / restore / verify。台帳 D-17)。Git管理外でバックアップに乗らず、`push local` の宛先も同じドライブなので、ディスク故障で両方消える。**守りたいのは承認の記録**(誰がいつ承認したかは registry にしか無い)。書いたあと読み直して1件ずつ照合し、**確かめられなければ成功にしない**。戻す前に安全用を書き出し、書庫の台帳の形を検査してから入れ替える(壊れたものを上書きしない)。作業中の staging / incoming と、14日で消える logs は入れない |
| tools/toolpack_gui.py | 追加ツールの管理画面(tkinter デスクトップアプリ。`追加ツールの管理.cmd` から pythonw で起動)。**判断を持たず**、追加・入れ替えは `toolpack_install`、無効化・削除・前の版へ戻すは `toolpack_manage`、自己診断は `doctor` を呼び、**その出力をそのまま出す**(試験されているのはコマンド側の経路。画面が独自判断を持つとテストが実際の挙動を覆わなくなる)。台帳・ハブ・Open WebUI の3系統を突き合わせ、確認できなかったものを「登録なし」と混ぜない。一覧は**1行 = Open WebUI に登録されているモデル1つ**のツリーで、そのモデルが使うツール(ハブの接続役)と残っている版はモデルの下へ1段下げて畳む(行数がモデルの数どまりになり、まとめて追加しても伸びない)。**承認されているかは列**であってまとめ方ではない。**モデルでないものをモデルとして並べない** — `excel_read`/`excel_comment` は Excel分析が使うツール、リポジトリにあるだけで未登録のものも非モデルで、どちらも「気になること」へ回す。**もとからある正式モデルは `CORE_MODEL_IDS` に明示**(全ファイルを拾うと廃止済みまで承認済みに見える)。表示名は Open WebUI が第一候補、取れなければパッケージの表示名。版の行では「前の版へ戻す」だけ、ツール・ファイルの行は表示のみ。**どのモデルもファイルの行を持つ**(もとからあるモデルはリポジトリのファイル、追加ツールは生成Pipe。管理外は「実体がありません」と出るので理由が分かる)。もとからある本番モデルは承認済みとして出す(運用で使われており承認手続きの対象外)。無効にした追加ツールは登録が無くても戻せるよう残す。権限は人ではなく**対象**で分け、承認済み・もとからある・管理外は見るだけ(共用アカウントでは本人確認ができないため。D-11・D-15)。長い処理は別スレッドで、**出力を拾うのは同時に1つだけ**(`redirect_stdout` はプロセス全体に効く) |
| tools/toolpack_manage.py | 追加ツールの管理(list / disable / enable / rollback / approve / unapprove / remove / purge)。**承認は版ごとで、人数は `additional-tools/registry/approval_policy.json` で変えられる**(既定2人/2人。壊れていても緩くならない)。承認すると `【未承認】` が外れるのでPipeを作り直して貼り直す。**台帳・Open WebUI・稼働中のハブの3か所が目的の状態になったと確かめられたときだけ成功**にする(確かめられなければ `Unconfirmed`)。失敗したら台帳を戻すだけでなく**前のPipeを貼り直す**。**承認済みは断る**(画面のボタンを暗くするだけでは足りない)。台帳を変える経路は `toolpack_store.admin_lock` の中。**追加と更新は toolpack_install の仕事**で、こちらは入った後の操作だけ。無効化も削除も Open WebUI からは消すが**ファイル実体は残す**(消したら戻せず、外部バックアップ D-17 が未決のため)。実体を消す purge だけ `--yes` を要求し、**登録が残っているものは消さない**。前の版へ戻すのは「有効版の差し替え + その版の generated/ を貼り直す」だけ(台帳 D-10) |
| tools/toolpack_textio.py | 添付ファイルを**文字として読む**コア側の共通部品。テキスト各種・字幕(.vtt/.srt は時刻と連番を落とす)・HTML・Word・PowerPoint・Excel(新旧)に対応し、文字コードも判定する。ランナーが**ジョブ直下へ複製**し、パッケージは `import toolpack_textio` で使う(ジョブ直下は読めるが書けないので差し替え不可。`sys.path` もコア側が先)。**解析は隔離の中で走る**ので、持ち込まれたファイルを親プロセスで開かない。読めない形式は「何の形式で・どうすれば読めるか」を返し、拡張子を付け替えたファイルも中身で見分ける。**PDFは wheelhouse にライブラリが無く未対応** |
| tools/toolpack_winjob.py | Windowsジョブオブジェクト(ctypes。pywin32はwheelhouseに無い)。メモリ2GB・プロセス1・`KILL_ON_JOB_CLOSE` による子ツリー全滅 |
| tools/toolpack_winsec.py | 制限トークン+低整合性レベルでの起動(ctypes)。書込可能にする out/tmp/mpl だけへ低整合性ラベルを付け、**outdir外への書込をOSに拒否させる**。`CREATE_SUSPENDED` で起動→ジョブ割当→再開なので隙が無い。**縛るのは書込だけで読取ではない**(読取制限は監査フック依存) |
| tools/toolpack_runner.py | 追加ツールの隔離実行の親。最小env(鍵・USERPROFILEを渡さない)を新規に作り、子の入口を `toolpack_child.py` に固定して起動する。隔離方式は `low`(既定・本番前提)と `basic`(切戻し)を明示指定で、**失敗しても黙って降格しない**。`--self-check` で監査フックとOS強制の両方が効いているか確かめる(doctorから実行)。venvのpython.exeはリダイレクタで `ActiveProcessLimit=1` に抵触するため、pyvenv.cfg からベース実体を解決し site-packages を PYTHONPATH で渡す |
| tools/toolpack_pipe_template.py | 追加ツールの**固定Pipeひな型**。オンラインAIにPipe本体を書かせず、これだけから生成する。動くPipeの形をしているのは `test_webui_shared_blocks.py` の写経ズレ検査を効かせるため(生成物はGit管理外にできて走査に入らない) |
| tools/toolpack_pipegen.py | ひな型へ tool.json の検証済み宣言値(表示名・ID・説明・入力条件・判定語・依頼例)を差し込んで生成Pipeを作る。**新規は常に【未承認】**で、承認状態の切替はD-14以降。判定語は静的リテラルのまま畳むので deploy のチップ照合が生成後も効く |
| tools/toolpack_test_entry.py | パッケージ同梱の単体テストを、追加ツールと同じ隔離下で走らせるコア側の入口。pytestの出力を捕まえ、結果の要約だけを規定のJSONで返す(標準出力はJSON1行という契約を守るため) |
| tools/toolpack_pack.py | 追加ツールのフォルダを `.localtool` へ固める。**manifest.sha256 を自動生成**し(手書きでは必ず食い違う)、固めた直後に検証器で自己検査して**通らないものは出力しない**。`__pycache__` 等の作業ゴミは入れない |
| tools/toolpack_ci.py | GitHub側の審査。`scope` は追加ツール以外を変更したPRを落とす(検査器やコアを触る抜け道も塞ぐ)、`verify` は `tool-packages/` 配下を**院内と同じ検証器**で検証する。ここへ検査を書き足さない(2箇所に分かれると食い違う) |
| tool-packages/ | オンラインAI向けの作業場。`SPEC.md`(仕様書)と `example_line_count/`(動く作成例)。**実データ・バイナリは置かない**。持ち込む形にするのは toolpack_pack.py |
| .github/workflows/tool-package.yml | 追加ツールPRの検査(変更範囲・パッケージ検証・検証器自身のテスト)。**院内はこの結果を信用せず再検査する**。早く間違いを返すための場 |
| tools/toolpack_name_guard.py | 追加ツールの承認表示(`【未承認】`)が管理画面から外されていないか**5分ごとに見張り、外されていたら戻す**(タスク `minutes-pipeline-toolpack-name-guard`)。印そのものは守れないので書き換えを検知して戻す方式。戻すのは表示名だけで承認状態には触れない。`webui_static_guard.py` と同じ考え方。**pythonwで動くため sys.stdout が None になる罠**に対処済み(台帳 5-17) |
| tools/toolpack_install.py | 受入CLI(**追加のみ**)。展開・形式検査・名前衝突検査・単体テスト・サンプル実行・配置・台帳登録・Pipe生成・Open WebUI登録・疎通確認を順に行う。無指定は検査だけ、`--apply` で取り込む。**どの段階で落ちても逆順で全部戻す**。テストとサンプル実行は job_lock を取って利用者のジョブと順番待ちを共有する |
| additional-tools/ | 追加ツールの実行時領域(git管理外)。incoming/staging/installed/rejected/logs/registry。**コードやひな型はここへ置かない**(復元もレビューもできなくなる) |
| TOOL_PACKAGE_DECISIONS.md ・ TOOL_PACKAGE_IMPLEMENTATION_PLAN.md | 追加ツール受入・モデル管理構想の設計判断台帳(確定/推奨/未決を区別)と、段階別の実装計画。この構想に関わる作業は会話の要約ではなく台帳を先に読む |
| tools/safe_file_import.py | ユーザーが明示指定した外部ファイルを、原本無変更でwork/imports/へ複製 |
| input/ | 依頼者が処理してほしいOfficeファイルを置く場所(git管理外) |
| output/ | ツールが結果を書き出す場所。input/ は絶対に上書きしない(git管理外) |
| work/ | Kiloが依頼ごとに生成する一時コードと外部ファイルの作業コピー。safe_task_runner.py以外からは実行しない(git管理外) |
| text-processing-bridge/requirements.txt ・ requirements-office.txt | ブリッジ本体用とOffice操作用の依存。どちらもホストvenvへ入れる |
| data/ ・ templates/ | 機密。読まない・触らない |
| templates/gui/ | 例外: 書式編集用ひな型(合成データ)。Excelコメントの書式の正本。詳細は templates/gui/README.md |
| templates/ の並び | `gui/`=編集する正本(パスがコードに直書き。移動禁止) / `original/`=実物の原本(戻し用) / `_scratch/`=使い捨て。案内は templates/README.md |
| offline-docs/ | ライブラリの公式ドキュメント置き場(読み取り専用)。fastapi-repo/・python-docx-repo/・xlsxwriter-repo/・ollama-repo/・owui-docs/(Open WebUI)・libs/duckdb-llms.txt・libs/pydantic-llms-full.txt が直接読める。libs/の各*.zipは同名フォルダへ展開済み(numpy-2.5・pandas-2.3/3.0・scipy-1.18・scikit-learn・pytestのHTMLドキュメント。zip原本は保存用で読まない)。**web/content-main/files/en-us/web/** は MDN(HTML・CSS・JavaScript・DOM)のmarkdown。CSSやDOMの仕様はここを読む(RAGには入れない。量が40倍で使い方ヘルプの検索が薄まるため)。仕様が不確かなときは推測せずここを読む |

## 変更時の約束

- 関数の別ファイルへの移動・モジュール階層の変更・クラス化は、タスクで明示されない限り行わない
- ファイル・主要な関数・エンドポイントを追加/削除したら、コミット前にこの表(02-map.md)と AGENTS.md の表を更新し、同じコミットに含める。このファイルは編集フェンスの例外として更新できる(差分承認あり)。書き方は既存の行に合わせ、1行1ファイルで簡潔に
