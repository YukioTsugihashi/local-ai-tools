r"""追加ツールのフォルダを `.localtool` へ固める(manifest は自動生成)。

使い方:
  ...python.exe tools\toolpack_pack.py tool-packages\example_line_count
  ...python.exe tools\toolpack_pack.py <フォルダ> --out D:\受け渡し

設計の正本は TOOL_PACKAGE_DECISIONS.md(D-2)。

- `manifest.sha256` は**この道具が作る**。手で書かない
  (自分自身を除く全ファイルを列挙する決まりなので、手作業では必ず食い違う)
- 固めた直後に `toolpack_verify` で自己検査し、**通らないものは出力しない**。
  壊れたパッケージを持ち出させないため
- 作業ゴミ(`__pycache__` 等)は入れない。入れると検証器が「余り」で落とす
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
import zipfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import toolpack_verify  # noqa: E402

MANIFEST_NAME = toolpack_verify.MANIFEST_NAME
SUFFIX = ".localtool"

# 固めるときに落とす作業ゴミ。残すと検証器が manifest の「余り」として落とす
EXCLUDED_DIRS = frozenset({"__pycache__", ".pytest_cache", ".git", ".idea", ".vscode"})
EXCLUDED_NAMES = frozenset({".DS_Store", "Thumbs.db", MANIFEST_NAME})
EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo"})


class PackError(RuntimeError):
    """固められない。理由と対処を含める。"""


def collect_files(package_dir: Path) -> list[Path]:
    """パッケージへ入れるファイルを、順序を決めて集める。"""
    files: list[Path] = []
    for path in sorted(package_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(package_dir)
        if any(part in EXCLUDED_DIRS for part in relative.parts):
            continue
        if path.name in EXCLUDED_NAMES or path.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        files.append(path)
    return files


def build_manifest(package_dir: Path, files: list[Path]) -> str:
    """`<sha256>  <相対パス>` を1行ずつ。manifest 自身は入れない(D-2)。"""
    lines = []
    for path in files:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(package_dir).as_posix()}")
    return "\n".join(lines) + "\n"


def pack(package_dir: Path, out_dir: Path | None = None) -> Path:
    """フォルダを `.localtool` にして返す。自己検査に通らなければ作らない。"""
    package_dir = Path(package_dir).resolve()
    if not (package_dir / "tool.json").is_file():
        raise PackError(
            f"tool.json がありません: {package_dir}\n"
            "対処: 追加ツールのフォルダ(tool.json のある場所)を指定してください"
        )

    import json

    try:
        meta = json.loads((package_dir / "tool.json").read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise PackError(f"tool.json を読めません: {error}") from error
    tool_id = str(meta.get("id") or "")
    version = str(meta.get("version") or "")
    if not tool_id or not version:
        raise PackError("tool.json に id と version が要ります")

    files = collect_files(package_dir)
    if not files:
        raise PackError("入れるファイルがありません")

    out_dir = Path(out_dir).resolve() if out_dir else package_dir.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{tool_id}-{version}{SUFFIX}"

    manifest = build_manifest(package_dir, files)
    # 一時ファイルへ作り、自己検査に通ってから置く(壊れたものを残さない)
    with tempfile.TemporaryDirectory(prefix="toolpack_pack_") as temporary:
        staged = Path(temporary) / target.name
        with zipfile.ZipFile(staged, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, path.relative_to(package_dir).as_posix())
            archive.writestr(MANIFEST_NAME, manifest)

        result = toolpack_verify.verify_package(staged, Path(temporary) / "check")
        if not result.ok:
            details = "\n".join(str(error) for error in result.errors[:10])
            raise PackError(
                f"検査に通らないため作りませんでした({len(result.errors)}件):\n{details}"
            )
        target.write_bytes(staged.read_bytes())
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="追加ツールのフォルダを .localtool へ固める(manifestは自動生成)"
    )
    parser.add_argument("package", help="tool.json のあるフォルダ")
    parser.add_argument("--out", help="出力先ディレクトリ(既定はフォルダの隣)")
    args = parser.parse_args(argv)

    try:
        target = pack(Path(args.package), Path(args.out) if args.out else None)
    except PackError as error:
        print(str(error))
        return 1
    size_kb = target.stat().st_size / 1024
    print(f"作成しました: {target} ({size_kb:.1f} KB)")
    print("検査にも通っています。この1ファイルをUSB等で持ち込んでください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
