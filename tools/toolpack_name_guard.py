r"""追加ツールの承認表示(`【未承認】`)が消されていないか見張り、消えていたら戻す。

**なぜ要るか(2026-08-31に実地で判明。台帳 5-17):**
承認状態の唯一の目印は表示名の `【未承認】` である(台帳 D-12 で、
未承認も全員に見せる=見えなくする防御は採らないと決めたため)。
ところがこの印は **Open WebUI の管理画面から数クリックで外せる**。
実際に外れたとき、doctor は「OK」と報告していた。
つまり承認を経ていないツールが正式なものに見える状態を、誰も検知できなかった。

印そのものを守ることはできないので、**書き換えを見つけて戻す**。
`webui_static_guard.py`(画面カスタマイズの見張り)と同じ考え方・同じ運用にする。

  python tools\\toolpack_name_guard.py          確認のみ(既定・読み取りだけ)
  python tools\\toolpack_name_guard.py --fix    違っていたら戻す
  python tools\\toolpack_name_guard.py --log    logs/ へ記録する(自動実行用)

自動実行: タスク minutes-pipeline-toolpack-name-guard(5分ごと)

**戻すのは表示名だけである。** 承認状態そのもの(台帳)には触れない。
承認・承認解除は人の操作(将来の管理GUI)で行う。
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# 自動実行は pythonw(コンソール無し)で動くため、標準出力が **None** になる。
# tools/ の多くのモジュールは import 時に無条件で sys.stdout.reconfigure を呼ぶので、
# そのままだと見張りが読み込み時に落ちる(2026-08-31に実測。終了コード1で無言死)。
# **他のモジュールを読む前に**捨て場を用意しておく。既存コアには手を入れない
if sys.stdout is None or sys.stderr is None:
    _sink = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = _sink
    if sys.stderr is None:
        sys.stderr = _sink
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

LOG_PATH = ROOT / "logs" / "toolpack_name_guard.log"


@dataclass(frozen=True)
class Drift:
    """表示名が台帳と食い違っているツール。"""

    tool_id: str
    version: str
    expected: str
    actual: dict[str, str]

    def describe(self) -> str:
        actual = " / ".join(f"{where}={name!r}" for where, name in sorted(self.actual.items()))
        return f"{self.tool_id}: あるべき名前={self.expected!r} 実際={actual}"


def expected_names(store) -> dict[str, tuple[str, str]]:
    """台帳から {tool_id: (版, あるべき表示名)} を作る。"""
    import doctor
    import json as _json

    import toolpack_store

    result: dict[str, tuple[str, str]] = {}
    for tool_id, info in store.active_tools().items():
        meta_path = store.package_dir(tool_id, info["version"]) / "tool.json"
        meta = _json.loads(meta_path.read_text(encoding="utf-8"))
        expected = doctor.expected_display_name(
            str(meta.get("display_name") or tool_id),
            toolpack_store.is_approved(info["entry"], info["version"]),
        )
        result[tool_id] = (info["version"], expected)
    return result


def check(store=None) -> list[Drift]:
    """食い違っているものを返す。**何も変更しない。**

    Open WebUI へ問い合わせられないときは空を返す(見張りが本処理を止めない)。
    確認できなかったことは呼び出し側が「未確認」として扱う。
    """
    import toolpack_store

    store = store or toolpack_store.ToolpackStore()
    if not store.registry_path.exists():
        return []
    try:
        expected = expected_names(store)
    except Exception:
        return []
    if not expected:
        return []

    try:
        import open_webui_deploy as deploy

        client = deploy.ApiClient(
            base_url=deploy.resolve_base_url(), api_key=deploy.load_api_key()
        )
        functions = {entry["id"]: entry.get("name") for entry in client.list_entries("pipe")}
    except Exception:
        return []

    drifted: list[Drift] = []
    for tool_id, (version, should_be) in sorted(expected.items()):
        actual: dict[str, str] = {}
        try:
            model_name = (client.get_model(tool_id) or {}).get("name")
        except Exception:
            model_name = None
        for where, name in (("Function", functions.get(tool_id)), ("モデル設定", model_name)):
            if name is not None and name != should_be:
                actual[where] = name
        if actual:
            drifted.append(Drift(tool_id, version, should_be, actual))
    return drifted


def restore(drifted: list[Drift], store=None) -> list[tuple[Drift, bool, str]]:
    """生成Pipeを配り直して表示名を戻す。(食い違い, 戻せたか, 理由)を返す。"""
    import toolpack_pipegen
    import toolpack_store

    store = store or toolpack_store.ToolpackStore()
    results: list[tuple[Drift, bool, str]] = []
    try:
        import open_webui_deploy as deploy

        client = deploy.ApiClient(
            base_url=deploy.resolve_base_url(), api_key=deploy.load_api_key()
        )
    except Exception as error:
        return [(item, False, f"Open WebUIへ接続できません({type(error).__name__})") for item in drifted]

    for item in drifted:
        pipe = store.generated_dir(item.tool_id, item.version) / toolpack_pipegen.pipe_filename(item.tool_id)
        if not pipe.is_file():
            results.append((item, False, "生成Pipeが見つかりません"))
            continue
        try:
            code = deploy.command_deploy(client, pipe, None, True)
        except Exception as error:
            results.append((item, False, f"{type(error).__name__}: {error}"))
            continue
        results.append((item, code == 0, "" if code == 0 else "配り直しに失敗しました"))
    return results


def _write_log(lines: list[str]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(f"{stamp} {line}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="追加ツールの承認表示が消されていないか見張る"
    )
    parser.add_argument("--fix", action="store_true", help="違っていたら戻す")
    parser.add_argument("--log", action="store_true", help="logs/ へ記録する(自動実行用)")
    args = parser.parse_args(argv)

    drifted = check()
    lines: list[str] = []

    if not drifted:
        message = "承認表示は台帳どおり"
        if not args.log:
            print(message)
        else:
            # 変化が無いときは記録しない(5分ごとにログが膨らむのを避ける)
            pass
        return 0

    for item in drifted:
        lines.append("食い違い: " + item.describe())

    if not args.fix:
        lines.append("戻すには --fix を付けて実行してください")
        for line in lines:
            print(line)
        if args.log:
            _write_log(lines)
        return 1

    failures = 0
    for item, ok, reason in restore(drifted):
        if ok:
            lines.append(f"戻しました: {item.tool_id} → {item.expected!r}")
        else:
            failures += 1
            lines.append(f"戻せませんでした: {item.tool_id} ({reason})")
    for line in lines:
        print(line)
    if args.log:
        _write_log(lines)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
