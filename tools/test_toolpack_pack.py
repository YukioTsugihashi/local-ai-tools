"""パッケージを固める道具(toolpack_pack)とGitHub側の審査(toolpack_ci)のテスト。

作成例が実際に固められて検査を通ることも、ここで固定する。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

import toolpack_ci
import toolpack_pack
import toolpack_verify

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "tool-packages" / "example_line_count"


def build_package(tmp_path: Path, *, tool_id: str = "sample_tool") -> Path:
    package = tmp_path / tool_id
    (package / "tests").mkdir(parents=True)
    (package / "samples").mkdir()
    meta = json.loads((EXAMPLE / "tool.json").read_text(encoding="utf-8"))
    meta["id"] = tool_id
    (package / "tool.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    for name in ("main.py", "README.md"):
        (package / name).write_text(
            (EXAMPLE / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    (package / "tests" / "test_main.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    (package / "samples" / "make_sample.py").write_text(
        (EXAMPLE / "samples" / "make_sample.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (package / "samples" / "smoke_request.json").write_text(
        (EXAMPLE / "samples" / "smoke_request.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return package


# ---------------------------------------------------------------------------
# 作成例
# ---------------------------------------------------------------------------
def test_作成例が固められて検査を通る(tmp_path) -> None:
    # 版は上がっていくので、宣言されている版から組み立てて確かめる
    version = json.loads((EXAMPLE / "tool.json").read_text(encoding="utf-8"))["version"]
    built = toolpack_pack.pack(EXAMPLE, tmp_path)
    assert built.name == f"example_line_count-{version}.localtool"
    result = toolpack_verify.verify_package(built, tmp_path / "check")
    assert result.ok, [str(error) for error in result.errors]


def test_作成例にmanifestを置いていない() -> None:
    # manifest は固めるときに作る。手で置くと必ず食い違う
    assert not (EXAMPLE / toolpack_verify.MANIFEST_NAME).exists()


def test_作成例の依頼例が判定語に一致する() -> None:
    meta = json.loads((EXAMPLE / "tool.json").read_text(encoding="utf-8"))
    import re

    patterns = [re.compile(p) for p in meta["routing"]["patterns"]]
    texts = list(meta["routing"]["examples"]) + [
        item["content"] for item in meta["routing"]["suggestions"]
    ]
    for text in texts:
        assert any(p.search(text) for p in patterns), text


# ---------------------------------------------------------------------------
# 固める
# ---------------------------------------------------------------------------
def test_manifestを自動生成する(tmp_path) -> None:
    built = toolpack_pack.pack(build_package(tmp_path / "src"), tmp_path / "out")
    with zipfile.ZipFile(built) as archive:
        names = set(archive.namelist())
        manifest = archive.read(toolpack_verify.MANIFEST_NAME).decode("utf-8")
    assert toolpack_verify.MANIFEST_NAME in names
    listed = {line.split("  ", 1)[1] for line in manifest.strip().splitlines()}
    # manifest 自身は載せず、それ以外は全部載る
    assert toolpack_verify.MANIFEST_NAME not in listed
    assert listed == names - {toolpack_verify.MANIFEST_NAME}


def test_作業ゴミを入れない(tmp_path) -> None:
    package = build_package(tmp_path / "src")
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / "main.cpython-312.pyc").write_bytes(b"junk")
    (package / "main.pyc").write_bytes(b"junk")
    built = toolpack_pack.pack(package, tmp_path / "out")
    with zipfile.ZipFile(built) as archive:
        names = archive.namelist()
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)


def test_検査に落ちるものは出力しない(tmp_path) -> None:
    package = build_package(tmp_path / "src")
    (package / "danger.exe").write_bytes(b"MZ")  # 同梱禁止
    out = tmp_path / "out"
    with pytest.raises(toolpack_pack.PackError, match="検査に通らない"):
        toolpack_pack.pack(package, out)
    assert not list(out.glob("*.localtool")) if out.exists() else True


def test_tool_jsonが無ければ断る(tmp_path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(toolpack_pack.PackError, match="tool.json がありません"):
        toolpack_pack.pack(empty, tmp_path / "out")


def test_出力名はidと版から決まる(tmp_path) -> None:
    package = build_package(tmp_path / "src", tool_id="duty_tool")
    version = json.loads((package / "tool.json").read_text(encoding="utf-8"))["version"]
    built = toolpack_pack.pack(package, tmp_path / "out")
    assert built.name == f"duty_tool-{version}.localtool"


# ---------------------------------------------------------------------------
# GitHub側の審査
# ---------------------------------------------------------------------------
def test_変更範囲の判定(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        toolpack_ci, "changed_files",
        lambda base: ["tool-packages/new_tool/main.py", "tool-packages/new_tool/tool.json"],
    )
    assert toolpack_ci.command_scope("origin/main") == 0
    assert "OK" in capsys.readouterr().out


def test_コアを触ったPRは落とす(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        toolpack_ci, "changed_files",
        lambda base: ["tool-packages/new_tool/main.py", "local_tool_bridge/hub.py"],
    )
    assert toolpack_ci.command_scope("origin/main") == 1
    output = capsys.readouterr().out
    assert "local_tool_bridge/hub.py" in output
    assert "既存システムの中核" in output


def test_検査器そのものを触ったPRも落とす(monkeypatch, capsys) -> None:
    # 検査を書き換えて通す、という抜け道を塞ぐ
    monkeypatch.setattr(
        toolpack_ci, "changed_files", lambda base: ["tools/toolpack_verify.py"]
    )
    assert toolpack_ci.command_scope("origin/main") == 1
    assert "既存システムの中核" in capsys.readouterr().out


def test_verifyは作成例を通す(capsys) -> None:
    assert toolpack_ci.command_verify() == 0
    assert "example_line_count" in capsys.readouterr().out
