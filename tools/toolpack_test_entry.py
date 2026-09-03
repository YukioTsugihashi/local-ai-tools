r"""パッケージ同梱の単体テストを、追加ツールと同じ隔離ランナーの中で実行する入口。

設計の正本は TOOL_PACKAGE_DECISIONS.md(D-9)。
**テストコードも「実行される可能性のあるコード」なので、本体と同じ隔離下で走らせる。**

これはコア側(Git管理下)のファイルで、パッケージからは差し替えられない。
インストーラがジョブ領域へ複製し、`--main` の入口として渡す。

pytest は標準出力へ大量に書くため、そのままでは
「標準出力はJSON1行だけ」の契約(D-5)を破る。ここで出力を捕まえ、
結果は要約だけを規定のJSONに載せて返す。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


def _reply(status: str, message: str) -> None:
    print(json.dumps(
        {"status": status, "message": message, "files": [], "skipped": [], "notes": []},
        ensure_ascii=False,
    ))


def main() -> int:
    parser = argparse.ArgumentParser(description="追加ツールの単体テストを隔離実行する")
    parser.add_argument("--request", required=True)
    args = parser.parse_args()

    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    tests_dir = (request.get("config") or {}).get("tests_dir")
    if not tests_dir or not Path(tests_dir).is_dir():
        _reply("user_error", "tests/ が見つかりません")
        return 0

    # テストが自分のパッケージを import できるようにする
    sys.path.insert(0, str(Path(tests_dir).parent))

    try:
        import pytest
    except Exception as error:
        _reply("user_error", f"pytest を読み込めません({type(error).__name__})")
        return 0

    # pytest の tmp_path は既定で「番号付きディレクトリ + current へのシンボリックリンク」
    # を作る。隔離はリンク作成を禁じているため(D-9)、そのままでは tmp_path を使う
    # テストが全部 PermissionError で落ちる。書込可能な場所を明示して、この段取りを省く。
    # TEMP はランナーがジョブ内の書込可能な tmp/ に向けている
    basetemp = Path(tempfile.gettempdir()) / "pytest"

    captured = io.StringIO()
    with redirect_stdout(captured), redirect_stderr(captured):
        code = pytest.main([
            str(tests_dir), "-q", "-p", "no:cacheprovider",
            f"--basetemp={basetemp}",
        ])

    if int(code) == 0:
        _reply("ok", "単体テストに合格しました")
        return 0
    # 失敗の手掛かりは要る。ただし末尾の要約だけに絞る
    tail = [line for line in captured.getvalue().strip().splitlines() if line.strip()][-3:]
    _reply("user_error", "単体テストが失敗しました: " + " / ".join(tail))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
