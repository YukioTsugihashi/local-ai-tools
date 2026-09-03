# 柵(ガードレール)カタログ — 2026-08-14 時点の実装

> 2026-08-14 更新: 調査時点で見つかった「1シートの失敗で全滅する」構造3箇所と、
> 実害A・C・D・Eを修正済み。本書はその修正後の状態を書いている。残作業は §7。

このリポジトリには「Kiloに勝手なことをさせない」ための柵が5層に分散している。
本書は**どこに何の柵があり、何をすると発火し、どうすれば解除できるか**の一覧である。
ルール文(.kilo/rules)は「モデルへの指示」にすぎず破られうるが、
プラグイン・Python・git hook の柵は**例外を投げて物理的に止める**。この違いを明示する。

凡例: 🔒 = 物理的に拒否(例外/exit) / 📄 = 指示のみ(モデルが従わない可能性あり)

---

## 0. 要点 — 「1つエラーが出ると全部止まる」の正体(修正済み)

Excelコメント処理は**3段すべてが「全部か無か」**で、部分成功もリトライも存在しなかった。
29シート中1シートが壊れていると成果物はゼロになっていた。3段とも部分成功へ変更した。

| # | 場所 | 修正前 | 修正後 |
|---|---|---|---|
| 1 | [excel_comment_context.py](tools/excel_comment_context.py) `collect_comment_contexts` | リスト内包表記で**抽出ごと死ぬ** | 失敗シートを `skipped` へ退避して継続。全滅時だけ例外 |
| 2 | [excel-tools.ts](.kilo/plugin/excel-tools.ts) `processExcelComment` | `for` + `await` で生成済みも破棄 | シート単位で `try/catch`。0件成功時だけ例外 |
| 3 | [office_excel.py](tools/office_excel.py) `cmd_set_batch` | 1件不正で**1セルも書かれない** | 不正な行だけ弾いて書込み、理由を出力 |

シートを名指しされた場合(`--sheet`)だけは、握らずそのまま失敗を伝える。

実チェーンでの確認結果(壊れたシート1枚 + 存在しないシート指定1件を混ぜた場合):

```
抽出できたシート: ['内科（案）', '外科（案）']
飛ばしたシート  : [('糖内（案）', 'コメント記入例が見つかりません: 糖内（案）')]
内科（案）!AG5: '変更前' → '内科コメント'
外科（案）!AG5: '変更前' → '外科コメント'
書き込めなかった更新 (1件):
  - 2件目 シートがありません: 存在しない（案）
保存しました(元ファイルは無変更): output\sample_updated.xlsx
```

### 何が「糖内(案)」を落としていたか

原因は2つあり、どちらも1シートで全体を止めていた。

1. **抽出段** — `コメント記入例が見つかりません`。ひな形の体裁が他シートと違うと出る
2. **生成段** — `buildVerifiedComment` は比較データが無い項目に「比較可能な直近年度値がない。」を出すが、
   その結果**比較基準年度(R7年度など)の出現回数が3回未満**になると自前の検査に落ち、例外になっていた

2は、下書きが検査に落ちても**書込みは行い、注意として報告する**方式へ変えた
(そのシートの比較データが足りないだけで、書いた内容自体は正しいため)。

### 報告の変更

結果に必ず内訳が出る。黙って減らすと「全部できた」と誤報告されるため。

```
生成コメント (28シート成功 / 1シート失敗):
...
処理できなかったシート (1件):
  - 糖内（案）: コメント記入例が見つかりません
データ不足のまま書込んだシート (2件):
  - ○○（案）: 入院・外来・まとめの直近比較で比較基準年度を明記していません
```

---

## 1. Kiloプラグイン層 🔒 — 例外でツールを拒否

### [00-context-router.ts](.kilo/plugin/00-context-router.ts) — エージェント強制切替

| 発火条件 | 動作 |
|---|---|
| 依頼文に `.xlsx / .xlsm / .xls / Excel / エクセル / Word / ワード / PowerPoint` | **無条件で `code` へ**。ユーザーが別エージェントを明示選択していても上書き([:62](.kilo/plugin/00-context-router.ts#L62)) |
| 画像添付 + 説明依頼 + 変更語なし | `vision` + qwen3-vl-64k へ |
| (不具合\|バグ\|エラー\|例外\|失敗\|動かない\|ログ) AND (原因\|調査\|診断\|解析\|直して\|修正\|解決\|対応) | `debug` へ |
| (README\|AGENTS.md\|Markdown\|ドキュメント\|.md) AND 変更語 AND コード語なし | `docs-writer` へ |

**注意**: 「エラーのログを調べて」と書くだけで debug へ飛ぶ。Excel語が1つでも入っていればそれが最優先で、他の判定は一切見られない。

### [excel-tools.ts](.kilo/plugin/excel-tools.ts) — Excelコメント文脈のロック

発火条件([:140-144](.kilo/plugin/excel-tools.ts#L140-L144)):
`(.xlsx|.xlsm|.xls|Excel|エクセル)` **AND** `(コメント|所見|評価文|分析文|分析内容)`

この2語が同居した瞬間、そのセッションは「Excelコメント文脈」になり:

| 柵 | 内容 | 解除方法 |
|---|---|---|
| ツール全面拒否 | `excel_write` `write` `edit` `apply_patch` `notebook_edit` `notebook_execute` `task` が例外 | **`excel_comment` が1回終われば自動で外れる**。書込み不要な依頼なら「変更しない」「調べるだけ」等を添える |
| bash拒否([:950-957](.kilo/plugin/excel-tools.ts#L950-L957)) | コマンドに `.xlsx/.xlsm/.xls/excel_reader.py/office_excel.py/safe_file_import.py` が含まれると例外 | なし |
| パス制限([:189-202](.kilo/plugin/excel-tools.ts#L189-L202)) | `input/` `work/` 以外のExcelを拒否 | `excel_import` で work/imports へ複製してから |
| 全シート強制([:672-681](.kilo/plugin/excel-tools.ts#L672-L681)) | `--all` 固定。**1シートだけ処理する経路が無い** | なし |

読取り系(read/glob/grep)は誤判定時の調査手段として意図的に常時許可されている([:938-941](.kilo/plugin/excel-tools.ts#L938-L941))。

### [stage-guard.ts](.kilo/plugin/stage-guard.ts) — 工程管理の強制

発火条件: `(実装|追加|変更|修正|統合|移植|構築|登録|デプロイ|作り直)` **AND**
(下記ドメイン語が**3種類以上** **OR** パスらしき文字列が**4個以上** **OR** `統合|移植|複数ファイル|段階|アーキテクチャ`)

ドメイン語: `API/エンドポイント/bridge` `Pipe/Tool/Open WebUI` `Docker/コンテナ/compose` `テスト/pytest/E2E/回帰` `登録/デプロイ/反映/--apply` `TypeScript/Python/.ts/.py`

以前は2種類/3個で発火し、「office_excel.py を修正してテストして」程度
(`修正` + `.py` + `テスト` = 2種)でも段階管理へ落ちていた。閾値を上げて第3の観点を要求する。

| 柵 | 内容 |
|---|---|
| 計画必須([:179-181](.kilo/plugin/stage-guard.ts#L179-L181)) | plan未登録で `edit/write/apply_patch/notebook_edit` が全部例外 |
| 工程数([:55-57](.kilo/plugin/stage-guard.ts#L55-L57)) | 3〜10工程ちょうど。2工程でも11工程でもエラー |
| 各工程の必須項目([:66-68](.kilo/plugin/stage-guard.ts#L66-L68)) | `name` `change` `verify` が全部必要 |
| advance条件([:124-128](.kilo/plugin/stage-guard.ts#L124-L128)) | `evidence` 必須 + 直前の編集後にテスト成功していないと例外 |
| --apply条件([:184-190](.kilo/plugin/stage-guard.ts#L184-L190)) | 全工程到達 **かつ** テスト成功 **かつ** 同セッションでdry-run成功 |
| complete条件 | 未完了工程なし(**または** 打ち切る理由を `evidence` に書く) + 編集したならテスト成功 + デプロイ依頼ならデプロイ成功 |
| **reset**(追加) | `stage_guard(action="reset")` で段階管理を解除できる。詰まったときの出口 |

状態は `.kilo/stage-guard-state.json` へ保存されるようになり、VS Code再起動後も続きから進められる
(保存・復元の失敗は握って進行を止めない)。計画前の状態は次のメッセージで作り直せるため保存しない。

---

## 2. エージェント定義層 🔒 — 編集権限([code.md](.kilo/agents/code.md))

| 権限 | 対象 |
|---|---|
| **deny** | `input/**` `data/**` `templates/**` `.env` `.env.*` `.venv/**` `offline-docs/**` `kilo.jsonc` `.kilocodeignore` `.kilo/**` |
| **deny**(柵の自己緩和防止) | `tools/safe_task_runner.py` `tools/safe_file_import.py` `tools/open_webui_deploy.py` |
| **ask**(承認プロンプト) | `tools/*.py` `text-processing-bridge/app/*.py` `.kilo/rules/02-map.md` その他すべて |
| **allow** | `output/**` `work/**` のみ |

`task: false` — サブエージェントへの委譲は最初から不可。

---

## 3. ルール層 📄 — 指示のみ(.kilo/rules/)

| ファイル | 主な制限 |
|---|---|
| [01-workflow.md](.kilo/rules/01-workflow.md) | 変更後テスト必須 / `.env`・`data/` は読取も不可 / 最小差分 / **クラス化・関数移動・シグネチャ変更は指示無しに禁止** / `_patch*.py` 禁止 / 日本語結果はファイル経由で読む |
| [02-map.md](.kilo/rules/02-map.md) | grep前にマップを見る(索引) |
| [03-newtool.md](.kilo/rules/03-newtool.md) | 新ツールは**Pipe/Tool比較を提示してユーザーに選ばせる**(勝手に決めるの禁止) / ファイル名 `open_webui_*_pipe.py` / クラス名はちょうど `Pipe` or `Tools` / docstringに `id:` `model_description:` `suggestion:` 必須 / 他ファイルimport禁止 |
| [04-plan-first.md](.kilo/rules/04-plan-first.md) | **編集前に question ツールで承認必須**。文章で聞くだけでは不可。例外はOffice加工とすぐやって指示 |
| [05-local-agent.md](.kilo/rules/05-local-agent.md) | Office処理の定型経路 / **一部シートのみ処理は禁止・全シート一括** / 独自Python作成禁止 / 例外を握りつぶして exit 0 にするの禁止 |
| [06-large-office-context.md](.kilo/rules/06-large-office-context.md) | 一度の範囲取得は**500セル以下**、検索結果**50件以下** |
| [07-staged-changes.md](.kilo/rules/07-staged-changes.md) | stage_guard運用 / **「ガードに止められたら迂回スクリプトを作るな」** |
| [08-openwebui-local-tools.md](.kilo/rules/08-openwebui-local-tools.md) | PipeからWindowsパス・リポジトリPython・Kiloプラグインを直接import/subprocess禁止 / 接続先は `host.docker.internal:8010` 固定 |

---

## 4. Pythonツール層 🔒

### [safe_task_runner.py](tools/safe_task_runner.py) — `sys.addaudithook` による実行時サンドボックス

| 柵 | 内容 |
|---|---|
| 実行対象([:108-111](tools/safe_task_runner.py#L108-L111)) | `work/` 内の `.py` のみ |
| 書込先([:68-72](tools/safe_task_runner.py#L68-L72)) | `work/` `output/` 以外への書込・削除・改名・mkdir は `PermissionError` |
| 機密読取([:77-78](tools/safe_task_runner.py#L77-L78)) | 機密パスの `open` を拒否 |
| **子プロセス・通信全面禁止**([:90-91](tools/safe_task_runner.py#L90-L91)) | `os.system` `subprocess.Popen` `socket.connect` `socket.bind` は無条件で `PermissionError` |

最後の1つが重い。**安全ランナー内のスクリプトからはOllamaも叩けない**(socket禁止)。

### その他

| ファイル | 柵 |
|---|---|
| [safe_file_import.py](tools/safe_file_import.py) | `.env` 拒否 / 機密ディレクトリ拒否 |
| [office_excel.py](tools/office_excel.py) | `--value-file` は `work/`・`output/` 限定([:283](tools/office_excel.py#L283)) / 書込後の再読込検証が不一致なら `RuntimeError`([:339](tools/office_excel.py#L339), [:406](tools/office_excel.py#L406)) |
| [excel_reader.py](tools/excel_reader.py) | `.xlsx/.xlsm` のみ / `--index-only`・`--find`・`--sheet/--range` は同時指定不可 |
| [open_webui_deploy.py](tools/open_webui_deploy.py) | 接続先はlocalhost固定 / 対象は `tools/` の `open*webui*.py` のみ / Kiloは編集不可(deny) |
| [local_tool_bridge/app.py](local_tool_bridge/app.py) | `.xlsx/.xlsm` のみ / ファイル名サニタイズ / `_RUN_LOCK` で**同時実行1件**([:19](local_tool_bridge/app.py#L19)) |

---

## 5. git層 🔒 — [.githooks/pre-commit](.githooks/pre-commit)

`core.hooksPath = .githooks` で**有効**(確認済み)。順に実行し、1つでも失敗するとコミット中止:

1. **コードマップ強制**([:11-24](.githooks/pre-commit#L11-L24)) — `text-processing-bridge/app/*.py` `tools/*.py` `.kilo/plugin/*.ts` の**追加・削除・改名**を含むコミットで、`02-map.md` か `AGENTS.md` が同じコミットに無ければ中止(`test_` 前置は除外)
2. `pytest -q`(text-processing-bridge)失敗 → 中止
3. `py_compile tools/*.py` 失敗 → 中止
4. `pytest tools` 失敗 → 中止

`.venv` が無い場合はテストを飛ばしてコミットを通す(1だけは効く)。

---

## 6. 調査中に見つかった実害(柵とは別の不具合)

| | 内容 | 状態 |
|---|---|---|
| A | LLM生成が事実上デッドコード | ✅ 修正 |
| B | 同じパイプラインが2実装ある | ⬜ 未対応 |
| C | 文字サイズ13の検証が機能していない | ✅ 修正 |
| D | 稼働率が捏造されうる | ✅ 修正 |
| E | stage_guard の状態が揮発 | ✅ 修正(§1参照) |

### A. LLM生成が事実上デッドコード ✅修正済み

[excel-tools.ts:600](.kilo/plugin/excel-tools.ts#L600) のリトライループは

```ts
for (let attempt = 0; attempt < 1; attempt += 1) {
```

**1回しか回らない**。`previousFailure` も `attempt > 0` の再プロンプトも到達しない。さらに末尾([:660-661](.kilo/plugin/excel-tools.ts#L660-L661)):

```ts
if (!previousFailure && normalized(comment) === normalized(verifiedComment)) return comment
return verifiedComment
```

`normalized` は空白と一部記号を除くだけなので、LLMが少しでも文体を整えると不一致になり
**必ず `verifiedComment`(決定論的な下書き)が返る**。
つまり**1シートにつき1回のLLM呼び出しが丸ごと捨てられている**。29シートなら29回。

> 今日(8/14) qwen3-coder-64k が59回呼ばれ、`/v1/chat/completions` に2分24秒かかった回があるのは、この捨てられる生成が主因と考えられる。

Python版([excel_comment_direct.py](local_tool_bridge/excel_comment_direct.py))は
同じ結論(数値が変わったら下書きへ戻す)を**例外を投げずに** `return verified` で処理していた。

**修正**: TS版をPython版の方式へ揃えた。整形結果は
「下書きと数値・単位が完全一致」かつ「全検査を通過」したときだけ採用し、
それ以外は下書きへ戻す。LLM失敗はすべて下書きへのフォールバックにし、例外は投げない。
これで呼び出しが捨てられなくなり、同時に生成段の主要な例外経路も消えた。

### B. 同じパイプラインが2実装ある ⬜未対応

`build_verified_comment` が **TS版([excel-tools.ts:213-343](.kilo/plugin/excel-tools.ts#L213-L343))と
Python版([excel_comment_direct.py:67-224](local_tool_bridge/excel_comment_direct.py#L67-L224))に手書きで二重実装**されている。
入口によって走る実装が変わる:

- Kilo直接(`excel_comment` ツール) → TS版(**落ちる**)
- Open WebUI経由(`/v1/excel/comment`) → Python版(**落ちない**)

検証条件の数も既にズレている(TS版は約20種の `invalidReason` 検査、Python版は数値一致のみ)。
同じ依頼が経路によって別結果になる。

部分成功化は両方へ入れたので当面の実害は消えているが、`build_verified_comment` の
二重実装は残っている。TS版は既に `excel_comment_context.py` `office_excel.py`
`excel_comment_report.py` をPythonへ委譲しているため、
残るコメント本文組み立てもPythonモジュールへ出せば一本化できる。

補足: Open WebUI経路は Pipe(`excel_analysis`)が登録済みだが、
Windows側受付(`local_tool_bridge`, port 8010)は常駐しておらず
`tools/start_local_services.py` の手動起動が要る。現状この経路は動いていない。

### C. 文字サイズ13の検証が機能していない ✅修正済み

[office_excel.py:410-416](tools/office_excel.py#L410-L416):

```python
for sheet_name, coordinate in comment_formats:
    if not _has_inline_font_size(target, sheet_name, coordinate, 13):
        # 検証に失敗した場合でもエラーとせず、フォントサイズ13で再書き込みする
        print(f"警告: 文字サイズが13ではありません。再書き込み処理を行います: {sheet_name}!{coordinate}")
    pass  # エラーを発生させずに処理を続ける
```

コメントは「再書き込みする」と書いてあるが**再書き込みしていなかった**。警告を出すだけ。
さらに `tools/test_office_excel.py` は12ptを期待したまま残っており、**pre-commitが常に失敗する状態**だった。

**修正**: サイズを `COMMENT_FONT_SIZE = 13` の1箇所へ集約し、
検証は「反映されたかの確認 → 不一致なら警告」に統一(見た目の不一致で書込み全体を失敗にしない)。
テストも定数を参照するよう更新した。

### D. 稼働率が捏造されうる ✅修正済み

```ts
const occupancyParts = [`Ⅳ．稼働率は${number(occupancy?.latest_value ?? 0, 2)}％である。`]
```

`?? 0` が「データ無し」を「0」として出力し、稼働率データが取れないシートで
**「稼働率は0.00％である。」**と実在しない実績を報告していた。
値が取れない場合は「比較可能な値がない。」を出すよう変更。
まとめ行が「・。」になる経路も併せて塞いだ。

### E. stage_guard の状態が揮発 ✅修正済み

§1参照。`.kilo/stage-guard-state.json` へ保存するようにした。

---

### B. 二重実装 ✅解消済み

`build_verified_comment` は Kiloプラグイン(TypeScript)と受付側(Python)に手書きで
二重実装され、経路によって結果が食い違っていた。

**修正**: 実装を `tools/excel_comment_build.py` の1本に寄せ、
Kiloプラグインは `runPython` でそれを呼ぶだけにした
(**excel-tools.ts は 1021行 → 316行**)。Open WebUI経路もハブ経由で同じスクリプトを通る。

nodeが無く `excel-tools.test.mjs` を実行できなかったため、その検査は
`tools/test_excel_comment_build.py` へ移した。プラグインに `buildVerifiedComment` や
Ollama直叩きが残っていたら**pytestが落ちる**。

---

### F. Excel読み取りがハブを通っていなかった ✅解消済み

`index / find / range / table` を Open WebUIコンテナ内の openpyxl で処理しており、
`tools/excel_reader.py` の再実装(約170行)だった。WebUIの提案チップには5件出るのに
ハブの一覧には1件しか出ない、という食い違いも起きていた。

**修正**: `connectors/excel_read.py` を追加し、Pipeは操作判定とシート選択だけを行って
処理はハブ経由で `tools/excel_reader.py` へ渡すようにした(**Pipe 621行 → 434行**)。
読み取り処理をPipeへ書き戻すと `tools/test_excel_pipe_routing.py` が落ちる。

ハブは `options`(operation/query/sheet/range)を受け取れるようになった。
宣言していないキーは受付側で弾く。

確認済み: ハブ経由の4操作と異常系(未対応の操作・未宣言のオプション)、
登録内容の一致、**Open WebUIのチャット画面からのE2E**。

---

## 7. 残っている作業

現時点で未解決のものは無い。

### PC全体が固まる障害と、その検知漏れ(2026-08-14)

**症状**: RAM 100%・Cドライブ 100%。ただしC:の空きは1459GBあり、容量ではなかった。

**実体**: メモリではなく**コミット(仮想メモリ)**が 230.1/230.5GB = 99.8%。
`ollama.exe serve` 単独で **194GB** を抱えていた(モデル本体の `llama-server` は3.4GB)。
コミットが限界に達したためWindowsがページファイルへ書き続け、
ディスク100%とPC全体の停止を招いた。**2つの症状は同じ原因**だった。

**原因**: Kiloの `codebase_search` が有効なのに、保存先の qdrant が存在しなかった。
埋め込みだけを作り続け、保存できずに終わらない。1日12,806回の `/api/embed`。
git管理下のコードは109ファイルしかない(索引の必要が薄い)。
→ `kilo.jsonc` で **`codebase_search: false`** に。探索は `02-map.md` + grep で足りる。

**検知漏れ**: 当初の滞留チェックは「503」と「10分で1000件超」を見ていた。
今回は **400/500 で、1件3〜4分かかるため件数が伸びず**、両方をすり抜けた。

| 追加した検知 | 内容 |
|---|---|
| メモリのコミット率 | 90%以上で異常、75%以上で注意。**原因が何であれ捕まる**最後の砦 |
| 応答時間 | 60秒超が3件以上で異常。件数が伸びない詰まりを捕まえる |
| 失敗率 | 2xx以外が3割超で異常。成功しない処理の再試行ループを捕まえる |

**自動復旧**: `tools/ollama_watchdog.py`(15分ごと、タスク `minutes-pipeline-ollama-watchdog`)。
doctorは読取専用の約束があるため、手を出す役を別ツールに分けた。
**コミット90%以上 かつ ollama.exe 40GB以上**の両方を満たしたときだけ再起動する
(片方だけでは正常な高負荷と区別できず、別プロセスが原因のときにOllamaを落としても解決しない)。
判定境界は `tools/test_ollama_watchdog.py` で固定。記録は `logs/ollama_watchdog.log`。

### Pipe間の写経 — 消せないが、ズレたら落ちるようにした(2026-08-14)

Open WebUIのPipeはコンテナ内で**1ファイル単体**として実行されるため、
リポジトリの共通モジュールをimportできない(`open_webui_deploy.py` も禁止している)。
「ファイルを預けて会話へ添付する」処理(`_store_file` / `_attach_file`)は
各Pipeへ写すしかなく、**重複は構造上消せない**。

実際に腐っていた。`_attach_file` の複数添付対応が文章処理パイプにしか無く、
**Excel分析ではコメント生成の成果物と確認用レポートのうち1件しか画面に残らなかった**
(Open WebUIは `files` イベントを画面側で「置き換え」処理するため、
`chat:message:files` で累積全件も送る必要がある)。

消せないので**気づけるようにした** — `tools/test_webui_shared_blocks.py`:

| 検査 | 内容 |
|---|---|
| 写経のズレ | 同じ方式を採るPipe間で `_store_file` / `_attach_file` が一致すること(プラグイン名ラベルとdocstringの差は許す) |
| 複数添付の取りこぼし | ループ内で添付するなら累積送信も必ず行うこと。docstringに書いてあるだけでは通さない(ASTで辞書リテラルの値を見る) |
| import禁止 | Pipeがリポジトリ内モジュールをimportしていないこと |

写経元は `tools/open_webui_text_processing_pipe.py`。1箇所直したら他へ反映する。
`.kilo/rules/03-newtool.md` にも手順として書いた。

### `__files__` は会話全体 — 前のメッセージの添付を巻き込んでいた ✅解消済み(2026-08-14)

Open WebUIが渡す `__files__` は「今回添付されたファイル」ではなく
**その会話に添付された全ファイル**。そのまま処理していたため、
2ファイル添付して依頼 → さらに2ファイル添付して依頼、とすると
**2回目に4件処理していた**(利用者から報告)。
文章処理では、前の会議の文字起こしが新しい議事録へ混ざる形で出る。

会話ごとに処理済みのファイルIDを覚え、新しいものだけを処理する:

| 対象 | 直し方 | 新しい添付が無いとき |
|---|---|---|
| Excel分析Pipe | `_pending_attachments` / `_remember_handled` | 直前に扱ったファイルへの続けての依頼とみなす |
| 文章処理Pipe | `_pending_files` / `_remember_handled` | 会話の土台(前回の処理結果)を使う |
| 文章処理Tool | `__metadata__["user_message"]["files"]` を最優先 | `__files__` → `__metadata__["files"]` の順で拾う |

境界は `tools/test_pipe_routing.py` と `tools/test_excel_pipe_routing.py` で固定。
会話をまたいだ取り違え(chat1の既処理がchat2に効く)も検査に含めた。

### Kiloプラグインの実行時テスト ✅解消済み(2026-08-14)

node/bun/deno が無く `.ts` を実行できなかったため、静的検査しか掛けられていなかった。
`D:\offline-kit\installers\node-v22.23.2-x64.msi` を `msiexec /a` で展開して導入した
(管理者権限もネット接続も不要)。

`.kilo/plugin/*.test.mjs` を `node --experimental-strip-types --test` で実行する。
**単体で置くと誰も走らせないまま腐るため**、`tools/test_kilo_plugin_runtime.py` から
pytest 経由で回す。pre-commit が pytest を回すので、プラグインを壊すとコミットが止まる。
node が無い環境ではskipする。

検査している柵: Excelコメント文脈の発火条件、誤判定時の逃げ道、
`input/`・`work/` 以外のExcelを拒否するパス制限、段階管理の発火閾値、
コメント組み立てがPython側へ委譲されたままであること。

### 解決済み(2026-08-14)

- **`transcription_refiner`**: 管理画面から直貼りされ、リポジトリに実体が無かった。
  回収したうえで実行したところ `re` 未importで必ず例外になり、一度も動いていなかった。
  役割も `/v1/text/kebatori` の下位互換だったため**登録を解除**。ソースは記録として残す
- **`dify_bridge` / `universal_file_export` の食い違い**: 前者はリポジトリが新しく(0.8.0 対 0.7.0)、
  後者は書式だけの差だった。**リポジトリを正としてデプロイ**し、登録済み4件すべてが
  リポジトリと一致する状態にした
- **院内の経営目標を公開しない**: `.kilo/plugin/resources/excel-focus-policy-map.txt` は
  稼働率や逆紹介率の**実際の目標値**を持っていた。どこからも読まれていなかったため
  2026-09-03 に Git 管理から外し、実物は `data/private/`(管理外)へ移した。
  同種のもの(実際の目標値・実績値・院内固有のアドレス・実在の氏名)は
  **追跡対象へ置かない**。置くなら `data/` 配下にする

- **Dify関連の完全削除**: `<利用者フォルダ>\dify`(842MB)、`offline-docs/dify-docs`(85MB)、
  Dify用イメージを削除。ついでに廃止済みの `offline-docs/roo-docs`(339MB)も削除

**登録済みプラグインは全てリポジトリが唯一の出所**になった。
管理画面から直接貼ると、この状態が崩れてレビューも復元もできなくなる。

## 8. Open WebUI ↔ Kiloツールのハブ(2026-08-14 に作り直し)

受付は「受け取って渡すだけ」の役に固定した。入口は3つで、ツールごとに増やさない。

| 入口 | 用途 |
|---|---|
| `GET /` | **状態画面**。繋がっているツールと、読み込めなかった接続役の理由を表示 |
| `GET /health` | 生存確認 |
| `GET /v1/tools` | 繋がっているツールの一覧(JSON) |
| `GET /v1/status` | 一覧 + 読み込みエラー(JSON) |
| `POST /v1/run` | ツール名を指定して実行 |
| `POST /v1/excel/comment` | 旧形式。登録済みPipeを壊さないための互換 |

状態画面はオフラインPCで開くため、外部のCSS・JS・フォントを一切読み込まない
(テストで固定してある)。接続役の書き間違いは受付を落とさず、
そのファイルだけ繋がらずに理由が画面へ出る。

ツール追加は `local_tool_bridge/connectors/` へ **ファイルを1枚置くだけ**
(`SPEC` を持つ `.py` を自動発見する)。`app.py` は編集しない。
手順と見本は [09-hub-connector.md](.kilo/rules/09-hub-connector.md)。

**柵**: `tools/test_local_tool_bridge.py` の
「ハブに業務ロジックを置かない」検査が、受付側での `openpyxl` / `urllib.request` /
`httpx` / `subprocess`(hub.py以外)の使用を**物理的に落とす**。
処理本体が2箇所へ分裂する事故はこれで再発しない。

**起動**: ログオン時に自動起動する(タスク `minutes-pipeline-local-services`)。
ハブ(8010)と文章処理ブリッジ(8008)を1プロセスで持つ。
以前は手動起動が必要で、起動を忘れた日はWebUIから使えなかった。
起動ログは `logs/local_services.log`。

---

## 9. 既存ドキュメントとの関係

本書以前に、柵を横断的にまとめたものは**無かった**。関連する既存文書:

- [AGENTS.md](AGENTS.md) — アーキテクチャとディレクトリ表。「重要な決まり」5項目のみ
- [HELP_KILO_BEGINNER.md](HELP_KILO_BEGINNER.md) — 利用者向け操作ヘルプ。柵は断片的に言及されるが一覧ではない
- [.kilo/rules/02-map.md](.kilo/rules/02-map.md) — 関数レベルの索引。柵の一覧ではない
- [.roo/rules/](.roo/rules/) — Roo時代の旧ルール(2026-08-07に Kilo へ移行済み。**現在は未使用**)
