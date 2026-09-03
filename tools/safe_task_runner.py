r"""Kiloが生成した一時Pythonスクリプトを限定されたファイル権限で実行する。

実行例:
  text-processing-bridge\.venv\Scripts\python.exe tools\safe_task_runner.py work\task.py

一時スクリプトは work/ に置く。リポジトリと input/ は読み取り専用、書き込みは
work/ と output/ の中だけに限定する。.env・data/・templates/ は読み取りも拒否する。
外部通信と子プロセス起動も拒否する。

これは悪意あるコードに対する完全なOSサンドボックスではなく、ローカルLLMの
誤操作から重要ファイルを守るための実行ガードである。
"""

from __future__ import annotations

import argparse
import os
import runpy
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def _resolved(value: object, workspace: Path) -> Path | None:
    if isinstance(value, int):
        return None
    try:
        path = Path(os.fsdecode(value))
    except (TypeError, ValueError):
        return None
    if not path.is_absolute():
        path = workspace / path
    return path.resolve(strict=False)


def _is_sensitive_read(path: Path, workspace: Path) -> bool:
    if path.name == ".env" or path.name.startswith(".env."):
        return True
    return any(
        _is_within(path, (workspace / name).resolve())
        for name in ("data", "templates")
    )


def _is_write_open(mode: object, flags: object) -> bool:
    if isinstance(mode, str) and any(mark in mode for mark in ("w", "a", "x", "+")):
        return True
    if isinstance(flags, int):
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        return bool(flags & write_flags)
    return False


def _install_guard(workspace: Path, work_dir: Path, output_dir: Path) -> None:
    writable = (work_dir, output_dir)

    def require_writable(value: object, operation: str) -> Path | None:
        path = _resolved(value, workspace)
        if path is not None and not any(_is_within(path, root) for root in writable):
            raise PermissionError(f"{operation}は禁止されています: {path}")
        return path

    def audit(event: str, args: tuple[object, ...]) -> None:
        if event == "open" and args:
            path = _resolved(args[0], workspace)
            if path is not None and _is_sensitive_read(path, workspace):
                raise PermissionError(f"機密ファイルの読み取りは禁止されています: {path}")
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else None
            if _is_write_open(mode, flags):
                require_writable(args[0], "work/・output/以外への書き込み")
        elif event in {"os.remove", "os.rmdir", "os.chdir", "os.chmod", "os.truncate"} and args:
            require_writable(args[0], event)
        elif event in {"os.rename", "os.replace"} and len(args) >= 2:
            require_writable(args[0], event)
            require_writable(args[1], event)
        elif event == "os.mkdir" and args:
            require_writable(args[0], event)
        elif event in {"os.system", "subprocess.Popen", "socket.connect", "socket.bind"}:
            raise PermissionError(f"一時スクリプトでは {event} を実行できません")

    sys.addaudithook(audit)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="work/ の一時Pythonを安全制限付きで実行する"
    )
    parser.add_argument("script", help="work/ 内のPythonスクリプト")
    parser.add_argument("args", nargs=argparse.REMAINDER, help="スクリプトへ渡す引数")
    options = parser.parse_args()

    workspace = Path.cwd().resolve()
    work_dir = (workspace / "work").resolve()
    output_dir = (workspace / "output").resolve()
    script = _resolved(options.script, workspace)
    if script is None or not _is_within(script, work_dir):
        raise SystemExit("実行できるスクリプトは work/ の中だけです")
    if script.suffix.lower() != ".py" or not script.is_file():
        raise SystemExit(f"Pythonスクリプトが見つかりません: {options.script}")

    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = work_dir / ".tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = str(temp_dir)
    os.environ["TMP"] = str(temp_dir)
    tempfile.tempdir = str(temp_dir)
    sys.dont_write_bytecode = True

    _install_guard(workspace, work_dir, output_dir)
    sys.argv = [str(script), *options.args]
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
