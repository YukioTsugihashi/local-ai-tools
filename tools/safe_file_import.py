r"""明示指定された外部ファイルをKiloの作業領域へ安全に複製する。

実行例:
  text-processing-bridge\.venv\Scripts\python.exe tools\safe_file_import.py "D:\共有\帳票.xlsm"

コピー先は work/imports/ に固定し、同名ファイルがあれば連番を付ける。外部原本、
input/、output/には書き込まない。.envとリポジトリ内のdata/・templates/は拒否する。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def _validate_source(source: Path, workspace: Path) -> None:
    if source.name == ".env" or source.name.startswith(".env."):
        raise SystemExit(".envファイルは取り込めません")
    for protected_name in ("data", "templates"):
        protected = (workspace / protected_name).resolve()
        if _is_within(source, protected):
            raise SystemExit(f"機密領域 {protected_name}/ のファイルは取り込めません")
    if not source.is_file():
        raise SystemExit(f"ファイルが見つかりません: {source}")


def _unique_target(import_dir: Path, name: str) -> Path:
    target = import_dir / name
    if not target.exists():
        return target
    stem = Path(name).stem
    suffix = Path(name).suffix
    index = 2
    while True:
        candidate = import_dir / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def import_file(path: Path, workspace: Path) -> Path:
    source = path.expanduser().resolve()
    _validate_source(source, workspace)
    import_dir = (workspace / "work" / "imports").resolve()
    import_dir.mkdir(parents=True, exist_ok=True)

    if _is_within(source, import_dir):
        return source

    target = _unique_target(import_dir, source.name)
    shutil.copy2(source, target)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(
        description="明示指定された外部ファイルを work/imports/ へ複製する"
    )
    parser.add_argument("path", help="取り込むファイルの絶対パス")
    args = parser.parse_args()

    target = import_file(Path(args.path), Path.cwd().resolve())
    print(f"作業コピーを作成しました: {target}")
    print("外部の元ファイルは変更していません")


if __name__ == "__main__":
    main()
