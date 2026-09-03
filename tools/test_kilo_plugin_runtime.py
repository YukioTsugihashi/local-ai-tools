"""
Kiloプラグイン(TypeScript)の実行時テストを pytest から回す。

.mjs のテストは単体でも `node --experimental-strip-types --test` で動くが、
それだと誰も実行しないまま腐る。pre-commitが回すpytestへ載せて、
プラグインを壊したらコミットが止まるようにする。

nodeが無い環境ではskipする(オフライン移行前の別PC等)。
node本体は D:\\offline-kit\\installers\\node-v22.23.2-x64.msi から入れられる。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / ".kilo" / "plugin"
# msiexec /a で展開した場合の既定の場所。PATH上にあればそちらを優先する
EXTRACTED_NODE = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs" / "PFiles64" / "nodejs" / "node.exe"
)


def _node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    return str(EXTRACTED_NODE) if EXTRACTED_NODE.is_file() else None


def test_plugin_runtime_tests_pass() -> None:
    node = _node()
    if node is None:
        pytest.skip("nodeが見つかりません(D:\\offline-kit\\installers のmsiから導入できます)")

    tests = sorted(PLUGIN_DIR.glob("*.test.mjs"))
    assert tests, "プラグインの実行時テストが1件もありません"

    result = subprocess.run(
        [node, "--experimental-strip-types", "--test", *(str(path) for path in tests)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    assert result.returncode == 0, (
        "プラグインのテストが失敗しました:\n"
        + "\n".join((result.stdout + result.stderr).splitlines()[-40:])
    )
