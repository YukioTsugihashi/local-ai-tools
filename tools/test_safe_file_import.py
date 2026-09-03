from __future__ import annotations

import subprocess
import sys
from pathlib import Path


IMPORTER = Path(__file__).with_name("safe_file_import.py")


def _run(workspace: Path, source: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(IMPORTER), str(source)],
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_imports_external_file_without_changing_source(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "外部 帳票.xlsx"
    external.write_bytes(b"original")

    result = _run(workspace, external)

    assert result.returncode == 0, result.stderr
    assert external.read_bytes() == b"original"
    assert (workspace / "work" / "imports" / external.name).read_bytes() == b"original"


def test_uses_numbered_name_when_same_name_exists(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "book.xlsx"
    external.write_bytes(b"first")

    assert _run(workspace, external).returncode == 0
    external.write_bytes(b"second")
    result = _run(workspace, external)

    assert result.returncode == 0, result.stderr
    assert (workspace / "work" / "imports" / "book.xlsx").read_bytes() == b"first"
    assert (workspace / "work" / "imports" / "book_2.xlsx").read_bytes() == b"second"


def test_rejects_env_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / ".env"
    external.write_text("SECRET=test", encoding="utf-8")

    result = _run(workspace, external)

    assert result.returncode != 0
    assert ".envファイル" in result.stderr
    assert not (workspace / "work").exists()


def test_rejects_protected_workspace_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    protected = workspace / "data"
    protected.mkdir(parents=True)
    source = protected / "secret.xlsx"
    source.write_bytes(b"secret")

    result = _run(workspace, source)

    assert result.returncode != 0
    assert "機密領域 data/" in result.stderr
    assert not (workspace / "work").exists()
