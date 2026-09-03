r"""GitHub側の審査(変更範囲の検査と、全パッケージの検証)。

使い方(CIから):
  python tools/toolpack_ci.py scope --base origin/main
  python tools/toolpack_ci.py verify

設計の正本は TOOL_PACKAGE_DECISIONS.md(D-1・柱1)。

**院内側はこの結果を信用しない。** GitHub側の検査は、オンラインAIへ早く
間違いを返すためのものである。持ち込まれたパッケージは院内側で必ず再検査する。

検査の中身は `toolpack_verify` を呼ぶだけで、**院内インストーラと同じ正本**を使う。
ここへ検査を書き足さないこと(2箇所に分かれると必ず食い違う)。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import toolpack_pack  # noqa: E402
import toolpack_verify  # noqa: E402

PACKAGES_DIR = "tool-packages"

# 追加ツールのPRで触ってよい場所。ここ以外を変えたPRは落とす
ALLOWED_PREFIXES = (f"{PACKAGES_DIR}/",)
ALLOWED_FILES = ("README.md",)

# 触られたら特に困る場所(検査そのものと、その動かし方)
PROTECTED_HINTS = (".github/", "tools/toolpack_", "tools/doctor.py", "local_tool_bridge/")


def changed_files(base: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"変更ファイルを取れませんでした: {result.stderr.strip()}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def command_scope(base: str) -> int:
    """追加ツール以外を変更していないか見る。"""
    files = changed_files(base)
    if not files:
        print("変更されたファイルはありません。")
        return 0

    outside = [
        name for name in files
        if not name.startswith(ALLOWED_PREFIXES) and name not in ALLOWED_FILES
    ]
    print(f"変更ファイル: {len(files)}件 / 範囲外: {len(outside)}件")
    if not outside:
        print(f"OK: 変更は {PACKAGES_DIR}/ の中だけです。")
        return 0

    print("")
    print("追加ツールのPRで変更してよいのは tool-packages/ の中だけです。")
    print("範囲外の変更:")
    for name in outside:
        mark = " ← 既存システムの中核" if name.startswith(PROTECTED_HINTS) else ""
        print(f"  - {name}{mark}")
    print("")
    print("対処: 既存コードへの変更を取り消し、追加するツールのフォルダだけにしてください。")
    print("     コア側の修正が要る場合は、追加ツールとは別のPRにして人のレビューを受けてください。")
    return 1


def command_verify() -> int:
    """tool-packages/ 配下の全パッケージを、院内と同じ検査器で検証する。"""
    packages_root = ROOT / PACKAGES_DIR
    if not packages_root.is_dir():
        print(f"{PACKAGES_DIR}/ がありません。検証対象なし。")
        return 0

    packages = sorted(
        path.parent for path in packages_root.glob("*/tool.json")
    )
    if not packages:
        print("検証するパッケージがありません。")
        return 0

    failed = 0
    for package in packages:
        name = package.relative_to(ROOT).as_posix()
        if package.name != _declared_id(package):
            print(f"NG {name}: フォルダ名と tool.json の id が違います")
            failed += 1
            continue
        try:
            # 固める処理そのものが検査を含む(院内と同じ正本を通る)
            import tempfile

            with tempfile.TemporaryDirectory(prefix="toolpack_ci_") as temporary:
                built = toolpack_pack.pack(package, Path(temporary))
                size_kb = built.stat().st_size / 1024
            print(f"OK {name} ({size_kb:.1f} KB)")
        except toolpack_pack.PackError as error:
            print(f"NG {name}:")
            for line in str(error).splitlines():
                print(f"    {line}")
            failed += 1

    print("")
    print(f"検証: {len(packages)}件中 {len(packages) - failed}件が合格")
    return 1 if failed else 0


def _declared_id(package: Path) -> str:
    import json

    try:
        return str(json.loads((package / "tool.json").read_text(encoding="utf-8")).get("id"))
    except Exception:
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="追加ツールPRの審査(GitHub側)")
    sub = parser.add_subparsers(dest="command", required=True)
    scope = sub.add_parser("scope", help="変更範囲が追加ツールに収まっているか")
    scope.add_argument("--base", default="origin/main", help="比較元(既定 origin/main)")
    sub.add_parser("verify", help="tool-packages/ 配下を院内と同じ検査器で検証する")
    args = parser.parse_args(argv)

    if args.command == "scope":
        return command_scope(args.base)
    return command_verify()


if __name__ == "__main__":
    raise SystemExit(main())
