# Roo → Kilo 移行の確認手順(2026-08-07)

管理者(自分)向け。先生向けの手順書ではない。

## 何をしたか

- Kilo Code v7.4.20 をインストール(`kilocode.kilo-code`。VSIXは `D:\offline-kit\vsix\` に確保済み)
- Roo設定一式を `D:\offline-kit\roo\backup-2026-08-07\` へバックアップ
- 柵を全部Kilo形式へ移植。**Rooは残したまま**(検証が済むまで並行)

| 旧(Roo) | 新(Kilo) | 中身 |
|---|---|---|
| `.roo/rules/*.md` | `.kilo/rules/*.md` | 同内容をコピー |
| `.rooignore` | `.kilocodeignore` | 同内容(コメントだけ修正) |
| `.roomodes` | `.kilo/agents/{code,debug,docs-writer}.md` | fileRegex→permissionのglobへ変換 |
| allowed/deniedCommands | `kilo.jsonc` の `permission.bash` | 全項目移植。既定は ask |
| modeApiConfigs | `kilo.jsonc` の `model` + `agent.plan/ask` | 移行時: code系=qwen3-coder-64k、plan/ask=gpt-oss-64k → 2026-08-14: plan/code/debug=qwen3.6-128k、ask=gemma4:26b → **2026-08-17: plan/code/debug=qwen3.8-128k、ask=gemma4:26b(qwen3.6-128kは切り戻し用に登録を残す)** |
| autoImportSettingsPath | 不要(設定はリポジトリ内の `kilo.jsonc` に完結) | — |

追加の柵: `share: disabled`(クラウド送信なし)、`autoupdate: false`、`webfetch/websearch: deny`、`.env`/`data/`/`templates/` はignoreとpermissionの二重ブロック。Office文書等の依頼専用コードは `work/` に作り、`safe_task_runner.py` が読み書き範囲を制限して実行する。@メンション等の外部ファイルは `safe_file_import.py` で `work/imports/` へ複製し、外部原本を加工しない。

## CLIで確認済み(全部合格)

1. ask → gpt-oss-64k で応答(ログイン不要で動作)
2. code → qwen3-coder-64k に振り分け(2026-08-14からは qwen3.6-128k)
3. 通常のリポジトリ編集(AGENTS.md等)→ **承認必須**(`edit "*" ask`)。機密・元データ・Kilo安全設定はdeny
4. `text-processing-bridge/.env` の読み取り → **拒否**(`read "**/.env" deny`)
5. `git status` → 無確認で実行(allowリスト)
6. `git push` → **拒否**(裸の `git push` も `"git push *"` にマッチ)
7. docs-writer → *.md のみ編集可、エージェント別permissionがグローバルより優先
8. **通常のコード編集は承認必須(ask)** → CLI非対話では自動拒否・ファイル無傷。UIでは差分承認ダイアログになる
9. permissionパターンは `/` 表記でもWindowsパス(`\`)に正規化マッチする
10. 想定外コマンド(venv作成など)は既定 `ask` → 非対話では安全に自動拒否
11. **オフラインドキュメント参照**: `offline-docs/`(D:\offline-kit\docs へのジャンクション)経由で読める。Kiloはジャンクションを実体パスに解決して外部扱いにするため、`external_directory` に `D:/offline-kit/docs/**: allow` を追加して解決(他の外部パスはaskのまま)。全体検索はジャンクションに潜らないのでコンテキストも汚さない。編集は `offline-docs/**: deny` で読み取り専用。場所は 02-map.md に記載済み。**注意**: libs/ の *.zip(numpy・pandas・scipy・scikit-learn・pytest・Python本体)は展開しない限り読めない

### plan-first の扱い(重要な設計変更)

プロンプト層の「計画→承認→実装」はqwen3-coderに安定して効かなかった(3回実測: ルールのみ=無視/プロンプト強化=計画は出すが承認を待たず編集/questionツール強制=無視)。
そのため**承認の柵を物理層へ移した**: code/debugはフェンス内でも `edit: ask`。
LLMがルールを無視しても、人間が差分を承認するまでファイルは一切変わらない。
計画提示の指示文は残してある(出るときは出る)が、頼りにしない。

## 画像対応モデル(2026-08-07追加)

- qwen3-vl:30b(MoE 30B-A3B)を導入し、`qwen3-vl-64k` を既存流儀(num_ctx 65536)で作成。Modelfileは `D:\offline-kit\modelfiles\qwen3-vl-64k.Modelfile`
- Kilo両設定(グローバル+プロジェクト)へ登録。**画像を渡すには `attachment: true` だけでは足りず `modalities: { input: ["text","image"] }` が必須**(readツール・添付の両方がこのフラグで判定される。実測で確定)
- 役割分担(2026-08-17更新): 計画・実装=qwen3.8(plan/code/debug)、相談=gemma4(ask)、予備=qwen3.6・gpt-oss、画像=qwen3-vl(**visionエージェント**。編集・bash不可の分析専用)
- CLI実測済み: 添付(--file)とreadツールの両経路で日本語込みのテスト画像を正読。Glob/Readのツール呼び出しも動作
- **モデルの自動切替について(実測済みの結論)**: 画像内容に応じた自動切替機能はKiloに無い。task委譲(ask→vision)はgpt-ossが委譲せず自力Read→諦めで**不発**。CLIの@visionメンションも展開されず。**確実なのは「エージェントをvisionに切り替える」のみ**(切替でモデルは自動追従)。visionは mode: all にしてあるのでUIの@メンション・task委譲の受け皿にはなっている
- UIでの残り確認: ①visionエージェントでチャットへの画像貼り付け(またはShift+ドラッグ。PNG/JPEG/GIF/WebP)②ask等で画像を貼ったときの警告・モデル候補の挙動(登録済みqwen3-vl-64kが候補に出るか)③@visionメンションがUIのメンションピッカーから動くか

## VS Code UIで確認すること(残り)

1. VS Code再起動 → Kiloサイドバーを開く。**ログイン不要でチャットできるか**(プロバイダーはollamaを選ぶ。Kiloクラウドのサインインは無視してよい)
2. エージェント一覧に code / debug / docs-writer / plan / ask が出るか、モデル表示が上の振り分け通りか
3. チャットから app配下の修正依頼 → **編集前に差分承認ダイアログが出るか**(これが承認の柵)→ 承認後に適用→pytestまで動くか
4. `data/` の読み取り依頼 → 拒否されるか(UI側の.kilocodeignore経路)
5. 差分承認を「拒否」したらファイルが変わらないままかも1回確認
6. **ネットを切って**新しいセッションが開始できるか(モデルカタログはmodels.devから取得→キャッシュされる実装。オフライン初回起動の確認は必須)
7. 問題なければ: Roo拡張をアンインストール、`settings.json` から `roo-cline.*` を削除(バックアップあり)。`.roo/`・`.roomodes`・`.rooignore` はその後の任意のタイミングで削除

## 既知の揺れ・環境仕様

- 旧qwen3-coderはまれにツール呼び出しを生テキストで吐いて空振りした(再実行で回復)。後継のqwen3.6も同系テンプレートのため、発生したら同様に再実行(Kilo 7.4.21には生テキストツールコール検出→ネイティブAPI誘導の防御も入っている)
- 中国語で応答することがあった → 各エージェント定義に「応答は必ず日本語で書く」を追加済み
- Kiloのコマンド実行は**毎回新しいPowerShell 5.1**: `&&`不可・`cd`は次の実行へ引き継がれない。テストは1行形 `cd text-processing-bridge; .\.venv\Scripts\python.exe -m pytest -q`(01-workflow.mdと許可リストを修正済み。モデルも&&で失敗すると自力で;に直せていた)

## 保留(次フェーズ)

- Phase 2以降: vLLMを独立コンテナで追加 → Kilo → Open WebUI/Dify の順に接続替え(計画通り、今回は未着手)
- 先生向け `ROO_TASKS.md` のKilo版への書き換えは、UI検証が済んでから

## 権限柵の再検証手順（2026-08-07追記）

CLIのデバッグ経路では `ask` が承認なしで進む場合が確認されたため、Rooを削除する前に
VS Code UIで次を必ず確認する。テスト中はAuto Approveを無効にし、実データがある
`data/`・`templates/`・`input/`の既存ファイルは使用しない。

1. codeエージェントへ `text-processing-bridge/tests/_kilo_ui_probe.py` の新規作成を依頼する。
2. ファイル作成前に差分承認ダイアログが表示されることを確認し、最初は拒否する。
3. 拒否後にファイルが存在しないことを確認する。
4. 新しいセッションで同じ依頼を行い、承認後だけ作成されることを確認する。
5. generalとorchestratorへ `input/_kilo_ui_probe.txt` の作成を依頼し、承認の有無にかかわらず
   permission denyで作成されないことを確認する。
6. codeエージェントへ `git add`・`git commit` を依頼し、無承認では実行されないことを確認する。
7. planエージェントで `excel_reader.py --no-csv` 等の読み取り専用Officeコマンドだけが実行でき、書き込みコマンドは拒否されることを確認する。askは従来どおりbash拒否を確認する。
8. codeエージェントに架空のOfficeファイル加工を依頼し、`work/` の一時コード作成と `output/` の成果物作成は無承認で進み、`input/` の元ファイルが変わらないことを確認する。
9. 一時コードから `input/` への書き込みと `.env` の読み取りを試し、safe_task_runner.pyが拒否することを確認する。
10. 架空の外部Officeファイルを@メンションし、work/imports/へコピーされ、外部原本が無変更であることを確認する。
11. 全試験後、`_kilo_ui_probe.py`だけを削除し、`git status --short`で他の変更がないことを確認する。

上記のうち1つでも失敗した場合はRooを削除せず、KiloのAuto Approveを有効にしない。
