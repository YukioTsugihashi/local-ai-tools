# 追加ツール基盤の段階別実装計画(Fable向け指示書・前半)

> **Status: 実装指示書(前半:基盤)。設計の正本は [TOOL_PACKAGE_DECISIONS.md](TOOL_PACKAGE_DECISIONS.md)(コミット 77a9647 時点)。**
>
> この文書と台帳が食い違ったら**台帳が正しい**。台帳の「確定」以外を実装してはいけない。
> 管理GUI・承認操作・更新・削除・世代管理の完成版は **D-10 以降の確定後**(第9章の保留工程)。
> この文書自体はコードを含まない。実装は各工程の依頼が出てから行う。

最終更新: 2026-08-31

---

## 0. 全工程に共通する決まり

### 0-1. してはいけないこと

- `data/`・`templates/`・`.env`・管理APIキーの**中身を読まない**(存在・ACL・環境変数名の有無だけは可)
- 禁止操作の試験は**架空の秘密ファイルだけ**で行う(工程3を参照)。本物を試験対象にしない
- 実在の入力ファイル・患者情報を使わない。テストデータはすべて生成スクリプトで作る
- **`git add .` を使わない**。`git status`・`git diff` で確かめて関係ファイルだけ add する
- **AIは `main` へのマージを行わない**(人の作業)
- Open WebUIへの**本番登録・実モデルの削除を勝手に行わない**。
  登録・削除を伴う確認は、テスト専用IDを使い、人の立ち会いで行う
- D-10 以降の未決事項(更新・削除・世代管理・承認GUI)を**推測で実装しない**
- 既存の [safe_task_runner.py](tools/safe_task_runner.py) は**変更しない**(Kilo用として現状維持。追加ツール用ランナーは別ファイル)

### 0-2. Git・ブランチ・テスト

- **一工程一ブランチ**。名前は `toolpack-stage<N>-<内容>`(各工程に明記)
- **工程の区切りで、人が `main` へマージする。** Fableは前工程がマージされた後、
  **更新済みの `main` から**次工程のブランチを作る。前工程の未マージブランチから派生させない。
  各工程の「次工程へ進む条件」には、この共通条件が常に加わる
- **1コミット1目的**。作業の区切りごとにコミットし、テスト→ `git commit` → `git push local` まで終えてから手を離す
- テストはリポジトリの既存2系統がそのまま効く(pre-commitフックが強制する):

```powershell
cd text-processing-bridge
.\.venv\Scripts\python.exe -m pytest -q            # ブリッジ側
.\.venv\Scripts\python.exe -m pytest ..\tools -q   # tools側(新テストはここに入る)
```

- `tools/*.py` は pre-commit が `py_compile` で構文検査する。新ファイルも自動で対象になる
- `local_tool_bridge/` か `app.py` を変えた工程では、動作確認前に**ローカルサービスの再起動**が要る
  (`Stop-ScheduledTask` / `Start-ScheduledTask minutes-pipeline-local-services`)。人の立ち会いで行う

### 0-3. Git管理する場所と、Git管理外の場所

| 区分 | 場所 | 内容 |
|---|---|---|
| **Git管理(コード・テンプレート・テスト)** | `tools/toolpack_*.py` | 検証器・保存領域管理・ランナー・契約・Pipeテンプレート・インストーラCLIと、それらのテスト |
| 〃 | `local_tool_bridge/installed_specs.py`・`local_tool_bridge/job_lock.py` | ハブ側ローダとプロセス間ロック |
| 〃 | `.gitignore`・`.kilo/rules/09-hub-connector.md`・既存コアの改修箇所 | 一度きりのコア改修(第8章の集計表) |
| **Git管理外(実行時データ)** | `additional-tools/` 全体 | 持ち込まれたパッケージ・展開物・生成Pipe・registry・診断ログ。`.gitignore` へ登録(工程2) |

**コードは必ずGit管理側に置く。** `additional-tools/` へ実行コードのテンプレートや共通ヘルパを置いてはいけない(復元不能・レビュー不能になる)。

### 0-4. テストデータの決まり

- 壊れたパッケージ・架空秘密ファイル・合成Excel/TXTは、**テストコードが一時領域(`tmp_path`)に生成する**。
  バイナリのフィクスチャをコミットしない
- 各モジュールは「ルートディレクトリを注入できる」設計にする
  (テストが実物の `additional-tools/` や `%USERPROFILE%` に触れないため)

### 0-5. 工程の一覧と依存

| 工程 | ブランチ | 依存 | 成果物 | 状態 |
|---|---|---|---|---|
| 1. 検証器 | `toolpack-stage1-verifier` | なし | `.localtool` の安全展開と全数検査 | **完了** |
| 2. 保存領域とregistry | `toolpack-stage2-store` | 1 | `additional-tools/` と原子的な有効化 | **完了** |
| 3. ランナー第一段階 | `toolpack-stage3-runner` | 1 | 監査フック+最小env+ジョブオブジェクト | **完了** |
| 4. ランナー本番必須段階 | `toolpack-stage4-lowil` | 3 | 制限トークン+低整合性(**本番運用開始の前提**) | **完了** |
| 5. ハブとプロセス間ロック | `toolpack-stage5-hub` | 2, 3 | registry供給源と `_RUN_LOCK` 置換 | **完了**(実機確認済) |
| 6. deploy・doctor・生成Pipe | `toolpack-stage6-deploy-doctor` | 2, 5 | 登録経路と三面照合 | **完了** |
| 7. CLIインストーラ統合(追加のみ) | `toolpack-stage7-install-cli` | 1〜6 | 端から端までの受入 | **完了** |

**前半(基盤)は 2026-08-31 に全工程が main へ入った。** コア改修6件も全て消化済み。
実機で残っているのは第9章の「まだ行っていない実機確認」の2件。

工程は番号順に進める。**工程4を飛ばして本番運用を始めてはいけない**(台帳 D-9)。

---

## 1. 工程1: パッケージ形式と検証器

**目的**: `.localtool`(ZIP 1ファイル)を安全に展開し、台帳 D-2 の検査規約を全数実装する。
GitHub Actions と院内インストーラの両方から呼べる**検査の唯一の正本**を作る。

**前提**: 台帳 D-2(確定)・D-5 の tool.json 宣言項目(確定)。依存ライブラリ追加は不可
(jsonschema は wheelhouse に無い。**検証は標準ライブラリだけで手書きする**)。

**変更対象ファイルの案**:

- 新規 `tools/toolpack_verify.py` — 検証器本体(関数単位で呼べる構成+CLI)
- 新規 `tools/test_toolpack_verify.py`

**実装内容**:

1. **展開前のZIP検査**: エントリ名(絶対パス / ドライブ文字 / `..` / Windows予約名 `CON` `NUL` `COM1` 等 /
   バックスラッシュ / パス長 / **ASCII限定** / **重複エントリ** / **大文字小文字だけ違いWindowsで同一になるパス** /
   **末尾の空白・ピリオド** / **コロン(ADS)** / **暗号化エントリ** / **ZIP内のシンボリックリンク・特殊ファイル** /
   **正規化後のパス衝突**)。`extractall` を使わず検証済みの名前を1件ずつ自前の結合先へ展開。
   上限は**名前付き定数**にしてテストする:
   `MAX_ENTRIES = 500` / `MAX_FILE_BYTES = 100MB` / `MAX_TOTAL_BYTES = 200MB`(非圧縮合計)
2. **manifest.sha256 の突合**: 全列挙との照合。欠け・余り・ハッシュ不一致をそれぞれ別の理由で報告。
   **`manifest.sha256` 自身は照合一覧の対象外**(自分自身をハッシュ化できない)。
   それ以外の通常ファイルは**すべて列挙必須**
3. **tool.json の検証**: 台帳 D-5 の宣言項目(必須・型・値域)。`id` は `[a-z0-9_]{3,32}`、
   `timeout_seconds` は 1〜1500、`requires.packages` は
   `requirements.txt` + `requirements-office.txt` + `wheelhouse-win/` から**自動生成した許可リスト**と突合
4. **禁止同梱物の拒否**: `.pyc` `.pyd` `.dll` `.exe` `.bat` `.cmd` `.ps1` `.pth` 等(台帳 D-9)
5. **静的検査(AST)**: **実行され得る全 `.py`**(main.py・パッケージ内 import 先・
   `samples/make_sample.py`・テスト)を対象に、明白な禁止呼び出し
   (socket / subprocess / ctypes / `os.system` / `eval` / `exec` 等)と
   機密パスの文字列参照を拒否。**難読化で抜けられることは前提**(実行時のランナーが本命。台帳 7-3)
6. 失敗理由は「どの段階で・何が・どう直すか」を1件ずつ返す(オンラインAIへ突き返す文面になる)

**絶対に触らない範囲**: `hub.py` / `app.py` / `open_webui_deploy.py` / `doctor.py` /
既存 `tools/` の全ファイル / `.gitignore`(工程2で行う)/ 実データ。

**テスト**: 正常な最小パッケージ1つ+壊れたパッケージ群(トラバーサル名 / ZIP爆弾 / 予約名 /
非ASCIIパス / manifest欠け・余り・改ざん / tool.json不備各種 / 禁止バイナリ同梱)を
**テストコードが生成**し、正常が通り壊れが全部**理由付きで**落ちることを確認する。

**完了条件**: 上記テスト緑+既存全テスト緑。**壊れた例が1つでも黙って通ったら未完了**。

**失敗時の戻し方**: 新規ファイルのみなので `git revert` 1回で消える。

**次工程へ進む条件**: 完了条件を満たし、push local 済み。

**今回の工程に含めないもの**: 配置(工程2)・実行(工程3)・スモーク実行 ・
オンラインAI向け SPEC.md(後半のGitHub整備で作る)。

**Fableへそのまま渡せる依頼文**:

```text
TOOL_PACKAGE_DECISIONS.md(正本)と TOOL_PACKAGE_IMPLEMENTATION_PLAN.md の工程1を読み、
ブランチ toolpack-stage1-verifier で tools/toolpack_verify.py と
tools/test_toolpack_verify.py を新規作成してください。
台帳 D-2 の検査規約と D-5 の tool.json 宣言項目を全数実装します。
標準ライブラリのみ。フィクスチャはテストコードが tmp_path へ生成し、バイナリをコミットしない。
既存ファイルは一切変更しない。実データ・実秘密ファイルを使わない。
テスト(pytest ..\tools と ブリッジ側)→ commit(1目的ずつ)→ push local まで。
mainへのマージはしない。
```

---

## 2. 工程2: 専用領域とregistry

**目的**: `additional-tools/` の構成(台帳 D-3 確定)と、registry を唯一の有効化スイッチとする
原子的な配置・原状復帰を実装する。

**前提**: 工程1完了。台帳 D-3・D-4 の構成が正本。

**変更対象ファイルの案**:

- 新規 `tools/toolpack_store.py` — 領域生成 / staging→installed / registry読み書き / 原状復帰 / 保持期限掃除
- 新規 `tools/test_toolpack_store.py`
- 変更 [.gitignore](.gitignore) — `additional-tools/` を1行追加(**コア改修 #4**)

**実装内容**:

1. レイアウト生成: `incoming/ staging/ installed/ rejected/ logs/ registry/`
2. 版ディレクトリの分離: `installed/<tool_id>/<version>/` 配下に
   `source/package.localtool` / `package/` / `generated/` / `install.json`
3. **staging → installed は同一ボリューム内の `os.rename`**。
   registry 更新は一時ファイルへ書いて `os.replace`(台帳 D-3)
4. **registry.json だけが有効版の正本**。ディレクトリの存在は有効化を意味しない。
   registry に無い installed ディレクトリは不活性だが、**自動削除しない**
   (将来のロールバック用の版まで消えるため)。自動掃除は **staging の未完了物だけ**。
   installed 内の孤立版・旧版は doctor が報告するだけにする(削除の扱いは D-10 確定後)。
   registry 登録APIは registry 内の `id` 重複を登録前に拒否する
   (既存connector名との衝突検査は工程5・7が行う)
5. 原状復帰: 失敗時は staging を削除。rejected/ へは**個人情報を含まない診断結果だけ**を残し、
   失敗した `.localtool` の院内PC側コピーは削除する(原本は持ち込み元のUSBに残っている)。
   成功時は incoming と staging を削除
6. `logs/` の保持期限14日の掃除関数(呼び出しは工程7とdoctorから)

**絶対に触らない範囲**: `hub.py` / `app.py` / `deploy` / `doctor` / 既存 `tools/`。
`.gitignore` は追記1行のみ。

**テスト**: すべて `tmp_path` 注入で実行。途中クラッシュの模擬
(rename後・registry更新前に中断→**不活性で無害**なこと)、同名版の二重インストール拒否、
registry の原子的更新、掃除の期限判定。

**完了条件**: テスト緑。実 `additional-tools/` を作る場合も registry 空なら既存動作に影響ゼロ。

**失敗時の戻し方**: `git revert`。`additional-tools/` はGit管理外なのでディレクトリ削除で戻る。

**次工程へ進む条件**: 完了条件+push local。

**含めないもの**: ハブへの接続(工程5)・Open WebUI登録(工程6)・承認記録の運用(後半)。

**Fableへそのまま渡せる依頼文**:

```text
工程2をブランチ toolpack-stage2-store で実施してください。
tools/toolpack_store.py と tools/test_toolpack_store.py を新規作成し、
.gitignore へ additional-tools/ を1行追加します(それ以外の既存ファイルは変更しない)。
台帳 D-3 の構成・規約(registry唯一・os.rename/os.replace・原状復帰)を全数実装。
テストは tmp_path 注入のみで、実ユーザープロファイルへ触れないこと。
テスト → commit → push local まで。mainへのマージはしない。
```

---

## 3. 工程3: 専用ランナー第一段階

**目的**: 台帳 D-9 の第一段階(監査フック+最小環境変数+ジョブオブジェクト)を実装し、
架空秘密ファイルによる禁止操作試験で固定する。

**前提**: 工程1完了(パッケージを検証済みとして受け取る)。台帳 D-9(確定)・D-5(契約)。
pywin32 は使えない(**ctypes で実装**。台帳 5-13)。

**変更対象ファイルの案**:

- 新規 `tools/toolpack_runner.py` — **親側**: 最小env構築・ジョブオブジェクト適用・起動・
  タイムアウト・セルフテスト(`--self-check`)
- 新規 `tools/toolpack_child.py` — **子プロセスの固定ブートストラップ**(Git管理。
  最初に監査フックを登録し、その後 `runpy` でパッケージの `main.py` を読み込む)
- 新規 `tools/toolpack_winjob.py` — ジョブオブジェクト(ctypes): `KILL_ON_JOB_CLOSE`・メモリ・プロセス数
- 新規 `tools/toolpack_contract.py` — request.json 生成/読込・結果JSON検証・`files` 検証
- 新規 `tools/test_toolpack_runner.py`・`tools/test_toolpack_contract.py`

**実装内容**:

0. **監査フックは子プロセスの中で有効にする。** 親ランナーのフックでは子プロセスを監視できない
   (監査フックはプロセス単位。既存 [safe_task_runner.py](tools/safe_task_runner.py) と同じ構造)。
   ランナーは必ず `toolpack_child.py` を子のエントリとして**コアのGit管理パスから**起動し、
   パッケージ側から置換できないようにする
1. **読み取り許可リスト**: request.json / 入力コピー / 自パッケージ配下 / 明示許可した共通資材のみ。
   それ以外の `open` は PermissionError
2. **書き込み許可**: ジョブ専用 outdir / ジョブ内 TEMP・TMP / `MPLCONFIGDIR` のみ
3. **リンク作成の遮断**: `os.link` / `os.symlink` / `_winapi.CreateJunction`(発火は実測済み。台帳 5-13)
4. **通信**: 既定全面禁止。`permissions.ollama: true` のときだけ `127.0.0.1:11434` への
   `socket.connect` を許可
5. **最小環境変数を新規作成**(親から継承しない): PATH系の最小構成+TEMP/TMP/MPLCONFIGDIR。
   鍵・トークン・USERPROFILE系を渡さない
6. ジョブオブジェクト: メモリ 2GB / プロセス数 1 / `KILL_ON_JOB_CLOSE`。`CREATE_NO_WINDOW`。
   タイムアウトは tool.json(既定300・上限1500)
7. 結果検証(`toolpack_contract`): stdout がJSON1行のみ / `status` ok・user_error /
   `files` は相対・通常ファイル・許可拡張子・**20件以内・合計200MB以内**・
   `resolve()` 後が outdir 配下・**`st_nlink > 1` 拒否・`Path.is_junction()` 拒否** /
   入力コピーの実行前後ハッシュ照合
8. 失敗診断: `additional-tools/logs/` へ台帳 D-9 の「記録するもの」だけを書く。
   **stdout/stderr の生データ・本文・氏名・元ファイル名を書かない**。エラーコード表の初版を定義

**絶対に触らない範囲**: `safe_task_runner.py` / `hub.py` / `app.py` / 既存 `tools/`。

**テスト**:

- **架空秘密ファイル**を `tmp_path` に作り(`fake-data/patient.txt`・`fake-home/admin_api_key.txt`・
  `fake-project/.env`・`fake-outside/secret.txt`)、読めないことを確認。**本物は使わない**
- 禁止操作試験ツール群(テストが生成): outdir外書込 / 入力改変 / 子プロセス / LAN接続 /
  未宣言Ollama接続 / ハードリンクを `files` で返す / `../` 返却 / JSON以外のstdout /
  無限ループ(タイムアウト) / メモリ大量確保 / 大量・巨大出力 / exit 0 でJSON無し
- 正常系: 合成TXT/Excelを処理する最小ツールが契約どおり動く
- `--self-check`: ランナー自身が禁止操作を試みて PermissionError になることを確認するモード
  (工程6で doctor から呼ぶ)

**完了条件**: 禁止試験が**全部**止まり、正常系が通り、既存全テスト緑。

**失敗時の戻し方**: `git revert`(新規ファイルのみ)。

**次工程へ進む条件**: 完了条件+push local。

**含めないもの**: 制限トークン・低整合性(工程4)/ ハブからの呼び出し(工程5)/
Ollama実スモーク(工程7)。

**Fableへそのまま渡せる依頼文**:

```text
工程3をブランチ toolpack-stage3-runner で実施してください。
tools/toolpack_runner.py・toolpack_winjob.py・toolpack_contract.py とテスト2本を新規作成。
台帳 D-9 第一段階と D-5 の契約検証を全数実装します。ctypesのみ(pywin32不可)。
禁止試験は tmp_path に作る架空秘密ファイル4種と生成した試験ツールだけで行い、
本物の data/・.env・鍵ファイルには一切触れない。safe_task_runner.py は変更しない。
テスト → commit → push local まで。mainへのマージはしない。
```

---

## 4. 工程4: 専用ランナー本番必須段階

**目的**: 制限トークン+低整合性レベルで **outdir外への書込をOS強制へ格上げ**する。
台帳 D-9 のとおり、これは「将来の追加」ではなく**本番運用開始の前提**である。

**前提**: 工程3完了。非昇格で実装可能(自トークンの制限版は特権不要。台帳 5-13)。

**変更対象ファイルの案**:

- 新規 `tools/toolpack_winsec.py` — 制限トークン生成・低整合性設定・`CreateProcessAsUser`(ctypes)
- 変更 `tools/toolpack_runner.py` — 起動部の差し替え(段階1/段階2をフラグで切替可能に)
- 変更 `tools/test_toolpack_runner.py` — 再検査の追加

**実装内容**:

1. 制限トークン(`CreateRestrictedToken`)+ 低整合性レベルでツールプロセスを起動
2. ジョブ内の書込可能領域(outdir・TEMP・MPLCONFIGDIR)へ低整合性ラベルを付与
3. **第一段階だけでは防げなかった操作の再検査**: 監査フックを意図的に無効化した試験プロセスが
   `ctypes` 直呼びで outdir 外へ書こうとしても **OS が拒否する**ことを確認する試験
4. **限界の明記をコードと表示に残す**: 低整合性は読み取りを遮断しない。
   成功表示・診断・ドキュメント文字列で「読み取り隔離は監査フック依存」という事実を隠さない(台帳 D-9)

**絶対に触らない範囲**: 工程3と同じ+`toolpack_verify.py` / `toolpack_store.py`。

**テスト**: 工程3の禁止試験全部を段階2構成で再実行+ctypes直書き試験。
低整合性で matplotlib・openpyxl の正常系が動くこと(互換性確認)。

**完了条件**: フックに頼らずとも outdir 外書込が OS で止まる試験が緑。既存全テスト緑。

**失敗時の戻し方**: `git revert`。段階1へのフラグ切戻しが常に可能な構成にしておく。

**次工程へ進む条件**: 完了条件+push local。

**含めないもの**: AppContainer(採用しない)/ 読み取りのOS遮断(限界として明記する側)。

**Fableへそのまま渡せる依頼文**:

```text
工程4をブランチ toolpack-stage4-lowil で実施してください。
tools/toolpack_winsec.py を新規作成し、toolpack_runner.py の起動部だけを差し替えます。
制限トークン+低整合性+書込領域ラベル付与を ctypes で実装し、
「監査フック無効でもOSがoutdir外書込を拒否する」再検査テストを追加します。
読み取りは遮断できないという限界を表示・docstringに明記すること。
テスト → commit → push local まで。mainへのマージはしない。
```

---

## 5. 工程5: ハブとプロセス間ロック

**目的**: registry を毎リクエスト読む第2の ToolSpec 供給源をハブへ足し(コア改修 #1)、
`_RUN_LOCK` をプロセス間ロックへ置き換える(**コア改修 #6**)。

**前提**: 工程2(registry)・工程3(ランナー)完了。
[hub.py](local_tool_bridge/hub.py) の `discover()` は毎リクエスト実行される(台帳 5-6)。

**変更対象ファイルの案**:

- 新規 `local_tool_bridge/installed_specs.py` — registry→`ToolSpec` 供給源(毎回評価・
  パッケージ単位の例外隔離・名前衝突検査・ランナー経由の `run` 生成)
- 新規 `local_tool_bridge/job_lock.py` — プロセス間ロック(Windows名前付きMutex
  またはロックファイル。`acquire`/`release`/`is_busy` を提供)
- 変更 `local_tool_bridge/hub.py` — `discover()` へ供給源を1つ追加する数行のみ
- 変更 `local_tool_bridge/app.py` — `_RUN_LOCK` を `job_lock` へ置き換える箇所のみ
- 変更 `.kilo/rules/09-hub-connector.md` — 「hub.py を編集しない」柵の文面追従(一度きりの基盤追加の記録)
- 変更 `tools/test_local_tool_bridge.py` — 柵テストの追従+新テスト

**実装内容**:

1. `installed_specs.py` は registry.json を**呼ばれるたびに**読み、有効版だけを `ToolSpec` にする。
   壊れた項目はそのパッケージだけ errors へ隔離(`discover()` の既存思想と同じ)。
   既存 connectors の SPEC と名前が衝突したら、**既存connectorを必ず維持し、
   追加ツール側だけを拒否**して理由を errors へ出す
   (壊れた追加物で既存ツールが消える設計にしない)
2. `run` は必ず `toolpack_runner`(工程3/4)経由で `main.py --request …` を起動する。
   request.json の生成は `toolpack_contract`。**業務ロジックをここへ書かない**
   (`FORBIDDEN_IMPORTS` 検査が新モジュールにも効くようテストを拡張)
3. `job_lock`: ハブの実行とインストーラのスモークが**同じロック**を取る。
   `is_busy()` で待機状態を外部(後のGUI)から読める
4. `app.py` の `_RUN_LOCK` を `job_lock` に差し替え(挙動は従来どおり直列1件)

**絶対に触らない範囲**: `connectors/excel_read.py`・`excel_comment.py`(既存2枚)/
`page.py`(registry由来ツールの「呼んでいるスクリプト」欄が空になるのは既知・軽微。台帳 D-4 #5)/
既存Pipe群 / `run_repo_script`(既存ツールの経路は不変)。

**テスト**: registry へテスト項目を注入して `load_specs()` が**再起動なしで**拾う /
壊れた registry 項目の隔離 / 名前衝突で両方止まる / ロックを別プロセス(subprocess)から
取得・待機・解放する試験 / `is_busy` の整合。

**完了条件**: テスト緑+**人の立ち会いで**サービス再起動1回 → 手動配置したパッケージが
`http://localhost:8010/` に出る → registry から外すと(再起動なしで)消える、を実機確認。

**失敗時の戻し方**: `git revert` 後に**サービス再起動**(app.py/hub.py を触った工程のため必須)。

**次工程へ進む条件**: 実機確認まで完了+push local。

**含めないもの**: Open WebUI 登録(工程6)/ `/v1/status` の応答項目追加(GUIが要る後半で判断)。

**Fableへそのまま渡せる依頼文**:

```text
工程5をブランチ toolpack-stage5-hub で実施してください。
local_tool_bridge/installed_specs.py と job_lock.py を新規作成し、
hub.py の discover() への供給源追加と app.py の _RUN_LOCK 置換だけを行います
(既存connectors・page.py・既存Pipeは変更しない)。
.kilo/rules/09-hub-connector.md の柵文面と tools/test_local_tool_bridge.py を同時に追従させ、
新モジュールにも「業務ロジック禁止」検査を効かせること。
テスト → commit → push local。サービス再起動と実機確認は人の立ち会いで行う。
mainへのマージはしない。
```

---

## 6. 工程6: deploy・doctor・生成Pipe対応

**目的**: 生成Pipeの登録経路(コア改修 #2)、registry・ディスク・Open WebUI の三面照合
(コア改修 #3)、Pipeテンプレートを整える。

**前提**: 工程2・5完了。削除・無効化APIは**サーバー側に存在**(台帳 5-3 改・実機確認済み)だが、
院内クライアントには未実装。

**変更対象ファイルの案**:

- 変更 [tools/open_webui_deploy.py](tools/open_webui_deploy.py) —
  `validate_target_path` に `additional-tools/installed/<id>/<ver>/generated/` 直下を追加。
  **registry の有効版に一致するファイルだけ許可**する。
  `ApiClient` へ 無効化・削除(Function+モデル設定)メソッドを追加
- 変更 [tools/doctor.py](tools/doctor.py) — `check_registered_plugins` の照合対象へ
  registry 有効版の生成Pipeを追加。新検査「registry ⇔ ディスク ⇔ Open WebUI の三面照合」
  (**管理外モデルは表示のみ。変更・削除しない**)。
  **installed 内の孤立版・旧版の報告**(削除はしない。工程2の方針)。
  ランナー `--self-check` の定期実行を追加
- 新規 `tools/toolpack_pipe_template.py` — 生成Pipeの正本テンプレート
  (自己完結・`id:`/`model_description:`/`suggestion:` 必須・`OPERATION_PATTERNS` 静的リテラル・
  ファイル添付の写経ブロックは [open_webui_text_processing_pipe.py](tools/open_webui_text_processing_pipe.py) から)
- 新規 `tools/toolpack_pipegen.py` — tool.json → Pipe生成(テンプレートへ差し込むだけ)
- 新規 `tools/test_toolpack_pipegen.py`・doctor/deployの既存テストへの追従

**実装内容の要点**:

1. テンプレートは**Git管理側が正本**(台帳 D-4 で確定)。オンラインAIがPipe本体を自由に書く方式は
   採らない。**パッケージから差し込めるのは検証済みの表示名・ID・説明・入力条件・判定語・依頼例だけ**。
   生成コピーは `generated/` に置かれ既存テストの走査外なので、
   **テンプレート自体を** `test_webui_shared_blocks.py` 相当の写経ズレ検査の対象へ加える
2. 生成Pipeのファイル名は `open_webui_<tool_id>_pipe.py` を維持
   (deployの既存検査群が無改修で効く。台帳 D-4)
3. **新規追加モデルは工程6・7では常に「未承認」として生成する**(`【未承認】` 接頭辞固定。
   パッケージには書かせない)。承認状態の変更・承認済み表示への切替は **D-14 以降**で、
   ここでは承認GUI・承認操作を先取りしない
4. 削除・無効化メソッドの実機挙動確認は**人の立ち会い+テスト専用ID**で行い、
   終わったら人が削除する。自動テストはHTTPをモックする

**絶対に触らない範囲**: 既存Pipe3本の登録内容 / 管理外モデル / 実モデルの削除・無効化(立ち会い外)。

**テスト**: `validate_target_path` の許可・拒否(tools直下は従来どおり通ること)/
三面照合の各不一致パターン(registryのみ・ディスクのみ・Open WebUIのみ・内容食い違い)/
生成Pipeが deploy のチップ照合(`check_suggestion_routing`)を通ること。

**完了条件**: テスト緑+**この時点でコア改修6件が全て消化されている**(第8章の表で確認)。

**失敗時の戻し方**: `git revert`。Open WebUI 側にテストIDが残っていたら人が削除する。

**次工程へ進む条件**: 完了条件+push local。

**含めないもの**: 実パッケージの本番登録(工程7)/ 一覧GUI・承認操作(後半)/
`MODEL_ORDER_LIST` の並び順制御(後半のGUIと一緒に)。

**Fableへそのまま渡せる依頼文**:

```text
工程6をブランチ toolpack-stage6-deploy-doctor で実施してください。
open_webui_deploy.py の validate_target_path 拡張と ApiClient への無効化・削除メソッド追加、
doctor.py の三面照合(管理外モデルは表示のみ)、
tools/toolpack_pipe_template.py と toolpack_pipegen.py の新規作成を行います。
自動テストは Open WebUI への HTTP をモックし、本番登録・実モデル削除を行わない。
実機での削除API挙動確認はテスト専用IDで人の立ち会いのもと行う。
テスト → commit → push local まで。mainへのマージはしない。
```

---

## 7. 工程7: CLIインストーラ統合(追加のみ)

**目的**: 工程1〜6の部品を1本の受入フローへ統合する。**追加だけ**を扱う
(更新・削除・世代管理は D-10 確定後の後半)。

**前提**: 工程1〜6完了。

**変更対象ファイルの案**:

- 新規 `tools/toolpack_install.py` — CLI:
  `incoming` の `.localtool` → staging展開 → 検証(工程1)→
  **既存ツール・registry との名前衝突検査(登録前)** →
  単体テスト実行(ランナー内)→
  スモーク(合成サンプル生成もランナー内・Ollama宣言ツールは**実Ollama**)→
  installed へ配置(工程2)→ registry 登録(**常に未承認として**)→ Pipe生成(工程6)→
  deploy dry-run 差分表示 → **人の承認入力を待って** `--apply` → 疎通確認 → 完了表示。
  失敗時、rejected/ には診断結果のみを残し `.localtool` の院内コピーは削除する(工程2の方針)
- 新規 `tools/test_toolpack_install.py` — E2E(deploy部分はモック)

**実装内容の要点**:

1. 各段階の成否を台帳 D-14 の diag 方針で記録し、画面には「段階名+理由+ログ名」を出す
2. **失敗したらどの段階でも完全に戻す**(台帳 第6章のロールバック対象に従う。
   登録後の失敗は工程6の削除メソッドで取り消す)
3. スモークは `job_lock`(工程5)を取ってから実行。塞がっている間は
   「現在の処理が終わるのを待っています」を標準出力に表示(GUIは後半)
4. 成功表示に「モデル一覧での名前」「依頼文の例」を含める(利用者が使えなければ意味がない)
5. 限界の明示(台帳 D-9)を完了表示に含める

**絶対に触らない範囲**: 既存Pipe・既存モデル・コア(工程1〜6で改修済みの範囲以外)。

**テスト**: 正常な合成パッケージのE2E(deployモック)/ 意図的に壊したパッケージ群が
**各段階で**落ちて完全に戻ること / スモーク待機(ロック塞ぎの模擬)。

**完了条件**: E2Eテスト緑+**人の立ち会いで**、合成パッケージ1件を実際に
Open WebUI のモデル一覧まで通し、使えることと、削除で完全に消えることを実機確認。

**失敗時の戻し方**: `git revert`+実機に残ったテスト登録は人が削除。

**次工程へ進む条件**: これで前半(基盤)完了。後半は D-10 以降の確定待ち。

**含めないもの**: GUI / 承認操作(2名承認の画面)/ 更新・削除・世代管理 /
help_sync 連携 / GitHub側整備(SPEC.md・Actions)。

**Fableへそのまま渡せる依頼文**:

```text
工程7をブランチ toolpack-stage7-install-cli で実施してください。
tools/toolpack_install.py と tools/test_toolpack_install.py を新規作成し、
工程1〜6の部品を「追加のみ」の受入フローへ統合します。
E2EテストはdeployをモックしOpen WebUIへ本番登録しない。
実機での通し確認(登録→利用→削除)は合成パッケージとテスト専用IDで人の立ち会いのもと行う。
失敗時は台帳第6章の対象を全て戻すこと。
テスト → commit → push local まで。mainへのマージはしない。
```

---

## 8. 一度きりのコア改修の集計(6件)

| # | 対象 | 消化する工程 |
|---|---|---|
| 1 | `hub.py discover()` へ registry 供給源追加 | 工程5 |
| 2 | `open_webui_deploy.py validate_target_path` 拡張(+削除・無効化メソッド) | 工程6 |
| 3 | `doctor.py` 三面照合・registry有効版の照合 | 工程6 |
| 4 | `.gitignore` へ `additional-tools/` | 工程2 |
| 5 | `page.py backing_script()`(表示のみの軽微。**対応しない選択も可**) | 工程5で判断 |
| 6 | **`_RUN_LOCK` → プロセス間ロック**(`app.py`) | 工程5 |

工程6の完了条件で全件消化を確認する。

---

## 9. 後半工程(保留。D-10 以降の確定後に着手)

### 実機確認(2026-08-31 に完了)

前半の積み残しだった2件は、テスト専用IDと合成パッケージで実施済み(台帳 5-3・5-16)。

1. **削除APIの実挙動** — 台帳どおりの呼び方で削除でき、再取得でも消えていることを確認。
   本番3件は前後で不変
2. **通し確認** — 合成パッケージが9段階すべてを通り、モデル一覧へ `【未承認】` 付きで載り、
   稼働中のハブ経由で実際に動き、削除で完全に消えることを確認

**前半(基盤)はこれで完了。**

### 完了したもの(この指示書の範囲外だったが、その後に実施済み)

- **実ツール3件**(文字起こし健康診断・決定事項一覧・Excel比較グラフ)。
  D-7の3件を通し、3件とも `【未承認】` 付きでモデル一覧へ載り、ハブ経由で動作
- **GitHub側整備** — `tool-packages/SPEC.md`・作成例(`example_line_count`)・
  `tools/toolpack_ci.py`(scope / verify)
- **承認表示の見張り** — `tools/toolpack_name_guard.py`(5分ごと。台帳 5-17)
- **実データでの確認** — 実物の帳票と実物の文字起こしで通した(台帳 5-18・5-20)。
  合成データだけの検査では実物を1件も読めなかったため、
  同梱サンプルを実物と同じ作り・実物と同じ形式へ作り替えた

### 以下は**この指示書では実装しない**。未決事項を推測で実装しない(0-1)。

- ~~管理GUI(tkinter。一覧・待機表示・検査結果表示)— D-11・D-15~~ → **確定・実装済み**
  (`tools/toolpack_gui.py` / `追加ツールの管理.cmd`)
- 承認操作(2名・数クリック)と承認台帳 — D-14
- ~~更新・無効化・削除・世代管理・前バージョンへの復元 — D-10~~ → **確定・実装済み**
  (`toolpack_install.py --update` / `toolpack_manage.py`。本番3件で実機確認。台帳 5-21)
- help_sync 連携(追加ツールREADMEのナレッジ投入)
- D-16(LAN露出への対処)・D-17(外部バックアップ)

---

## 10. 変更履歴

| 日付 | 内容 |
|---|---|
| 2026-08-31 | **現状へ更新。** 実ツール3件・GitHub側整備・承認表示の見張り・実データでの確認が<br>完了しているのに「未検証」と書かれたままだったため、第9章を書き直した。<br>あわせて、直したツールを入れ替える経路が無いこと(D-10の制約)を明記 |
| 2026-08-31 | 初版。台帳(77a9647)の確定事項から前半7工程を起こした |
| 2026-08-31 | **前半7工程の実装が完了し main へ入った。** 工程一覧へ状態を記録し、<br>まだ行っていない実機確認2件(削除APIの実挙動・合成パッケージの通し確認)を第9章へ明記 |
| 2026-08-31 | レビュー(GPT)の8点を反映。孤立版の自動削除禁止 / rejectedは診断のみ /<br>名前衝突は既存維持・追加側のみ拒否 / ZIP検査の細部と上限定数 /<br>子プロセス用固定bootstrap(`toolpack_child.py`)追加 / Pipe固定テンプレート確定 /<br>新規モデルは常に未承認 / 工程間に人のmainマージを必須化 |
