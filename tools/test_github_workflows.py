"""GitHub Actions のワークフローが、GitHub に読める形かを見る。

**ここは手元で回せない場所なので、書式の間違いに気づきにくい。**
実際、ジョブIDを日本語(`検査:`)にしていたため、GitHub がワークフローを
読めず、ジョブが1つも動かないまま4回連続で失敗していた(2026-09-04)。
失敗の見え方は「名前がファイルパスのまま・ジョブ0件」で、
中のコマンドはどれも正しかった。

手元では次を確かめる:

- YAML として読めるか
- ジョブIDが GitHub の規則(英数字・アンダースコア・ハイフン)に合っているか
- 参照している実行ファイルがリポジトリに存在するか
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
# GitHub の規則: 英字か _ で始まり、英数字・- ・_ だけ
JOB_ID_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def workflow_files() -> list[Path]:
    if not WORKFLOW_DIR.is_dir():
        return []
    return sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))


def load(path: Path) -> dict:
    yaml = pytest.importorskip("yaml", reason="PyYAML が無い環境では読めない")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_ワークフローが1つ以上ある() -> None:
    assert workflow_files(), "GitHub側の検査が無くなっている"


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_YAMLとして読める(path: Path) -> None:
    data = load(path)
    assert isinstance(data, dict), f"{path.name} の形が違います"
    assert data.get("jobs"), f"{path.name} に jobs がありません"


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_ジョブIDは英数字(path: Path) -> None:
    """日本語のジョブIDにすると、**ジョブが1つも動かないまま失敗する。**

    手元のコマンドがいくら正しくても、GitHub が読めなければ意味が無い。
    """
    bad = [
        job_id for job_id in load(path)["jobs"]
        if not JOB_ID_PATTERN.match(str(job_id))
    ]
    assert not bad, (
        f"{path.name} のジョブID {bad} は GitHub が読めません。"
        "英数字にして、見せる名前は name: で日本語にしてください"
    )


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_呼んでいるファイルが存在する(path: Path) -> None:
    """`python tools/xxx.py` と書いてあるのに無い、を防ぐ。"""
    missing: list[str] = []
    for job in load(path)["jobs"].values():
        for step in job.get("steps") or []:
            for line in str(step.get("run") or "").splitlines():
                for token in re.findall(r"(?:tools|scripts)/[A-Za-z0-9_./-]+\.py", line):
                    if not (ROOT / token).is_file():
                        missing.append(token)
    assert not missing, f"{path.name} が呼んでいるファイルがありません: {missing}"


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_書き込み権限を持たせていない(path: Path) -> None:
    """検査だけの場なので、リポジトリを書き換える権限は要らない。"""
    permissions = load(path).get("permissions")
    assert permissions, f"{path.name} に permissions がありません"
    assert permissions == {"contents": "read"}, permissions
