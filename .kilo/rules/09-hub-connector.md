# Open WebUIへツールを繋ぐ手順(ハブ経由)

Windows上のKiloツールをOpen WebUIから使えるようにする依頼は、この手順だけで行う。
受付(`local_tool_bridge`)は「受け取って渡すだけ」の役であり、**処理本体を書いてはいけない**。

## やること／やってはいけないこと

| | |
|---|---|
| ✅ やる | `local_tool_bridge/connectors/` へ**ファイルを1枚新規作成**する |
| ✅ やる | 処理本体は `tools/` のスクリプトとして作り、そこへ委譲する |
| ❌ やらない | `local_tool_bridge/app.py` や `hub.py` を編集する(入口は増やさない) |
| | ※ 2026-08-31 に**一度だけ**の基盤追加を行った(下記「追加ツールの供給源」)。<br>これは追加ツール受入基盤のためのもので、**ツールを繋ぐたびに編集してよいという意味ではない** |
| ❌ やらない | 接続役の中でデータ処理・文章生成・Excel操作を書く |
| ❌ やらない | 接続役で `subprocess` `urllib` `httpx` `openpyxl` 等を直接使う |

最後の2つは `tools/test_local_tool_bridge.py` の
「ハブに業務ロジックを置かない」検査で**物理的に落ちる**。
過去に接続役へ処理を書き写した結果、同じ機能が2箇所へ分裂して
経路ごとに結果が変わる事故が起きたため、この境界は固定してある。

## 手順

1. 処理本体を `tools/<機能名>.py` として作る(または既存のものを使う)。
   - 単体で `python tools\<機能名>.py 入力パス --オプション` として動くこと
   - 成果物は `output/` へ保存し、**結果はJSON1行で標準出力へ**返す
   - 元ファイルは上書きしない
2. `local_tool_bridge/connectors/<ツール名>.py` を新規作成する(下の見本を写す)。
3. `tools/open_webui_excel_analysis_pipe.py` などの対象Pipeへ、
   操作の正規表現1行・ラベル1行・提案チップ1行を足す。
   判定を正規表現で行うのは意図的である(`.kilo/rules/03-newtool.md`)。
4. テストを追加して実行する。
5. `python tools\open_webui_deploy.py tools\<対象Pipe>.py` でdry-run後、`--apply`。

受付は自動起動している(タスク`minutes-pipeline-local-services`)。
繋がったかは **ブラウザで `http://localhost:8010/`** を開けば一覧で見える
(15秒ごとに自動更新。書き間違えた接続役はその理由もここに出る)。

## 接続役の見本(これを写して名前と中身だけ変える)

```python
"""<このツールが何をするか1行>。"""

from __future__ import annotations

import json

from local_tool_bridge.hub import RunContext, RunResult, ToolSpec, resolved_path, run_repo_script


def run(context: RunContext) -> RunResult:
    output = run_repo_script(
        context,
        "tools/<機能名>.py",
        [str(context.inputs[0]), "--instruction", context.instruction],
    )
    result = json.loads(output.splitlines()[-1])
    return RunResult(
        files=[resolved_path(context.root, result["<成果物のキー>"])],
        message="<利用者へ返す一言>",
        skipped=list(result.get("skipped") or []),
    )


SPEC = ToolSpec(
    name="<ツール名。英小文字と_>",
    summary="<一覧に出る説明>",
    accepts=(".xlsx", ".xlsm"),   # 受け付ける拡張子
    run=run,
)
```

`SPEC` という名前の変数がある `.py` を置くだけで自動的に繋がる。
登録作業も `app.py` への追記も要らない。

## 追加ツールの供給源(2026-08-31 に追加した2つ目の経路)

上の「接続役を1枚置く」に加えて、**USB等で持ち込んだ追加ツール**が
`additional-tools/registry/registry.json` から繋がる経路がある
(設計は `TOOL_PACKAGE_DECISIONS.md`、実装計画は `TOOL_PACKAGE_IMPLEMENTATION_PLAN.md`)。

- 供給元は `local_tool_bridge/installed_specs.py`。`hub.discover()` が毎回読み直すので、
  追加・ロールバックはサービス再起動なしで反映される
- **名前が衝突したら、既存の接続役を必ず残して追加ツール側を拒否する**
  (持ち込んだものが既存ツールを消す構造にしない)
- 追加ツールの実行は必ず `tools/toolpack_runner.py`(隔離ランナー)経由。
  接続役と同じく、`installed_specs.py` にも業務ロジックと実行手段を持たせない
- 実行の直列化は `local_tool_bridge/job_lock.py`(プロセス間ロック)。
  ハブと管理アプリが同じ順番待ちに並ぶため、`threading.Lock` へ戻さないこと

**この経路は追加ツール専用である。** Kiloが作る既存ツールを繋ぐときは、
これまでどおり `connectors/` へ1枚置く。

## 使える道具(hub.py が提供する)

| 名前 | 用途 |
|---|---|
| `run_repo_script(context, "tools/x.py", [引数...])` | `tools/` のスクリプトを実行して標準出力を返す。**唯一許された実行手段** |
| `resolved_path(context.root, "output\\a.xlsm")` | スクリプトが返した相対パスを絶対パスへ直す |
| `saved_paths(出力文字列, context.root)` | 「保存しました...: パス」行から成果物を拾う(JSONを返さないツール向け) |
| `context.inputs` | 受け取ったファイル(作業ディレクトリ内)。実行後に自動で消える |
| `context.instruction` | 利用者の依頼文 |

## 結果の返し方

- `files`: 利用者へ返すファイル。Pipeがファイルストアへ登録して添付する
- `message`: チャットに出る一言
- `skipped`: **処理できなかったものを必ず入れる**。黙って減らすと「全部できた」と誤報告になる

## 動かないときの見方

1. `http://localhost:8010/` を開く。自分のツールが一覧に出ているか
2. 出ていなければ「読み込めなかったファイル」の欄に理由が出ている
   (`SPEC がありません` / 読み込み時の例外など)
3. ページ自体が開けない場合は受付が落ちている。`logs/local_services.log` を見る
4. それでも分からなければ `python tools\doctor.py` を実行する
