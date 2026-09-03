"""toolpack_verify(追加ツールパッケージ検証器)のテスト。

正常・異常な .localtool はすべてこのテストが tmp_path 内へ生成する。
バイナリのフィクスチャはコミットしない。実データ・実秘密ファイルには触れない。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import zipfile
from pathlib import Path

import toolpack_verify as tv


# ---------------------------------------------------------------------------
# パッケージ生成ヘルパ
# ---------------------------------------------------------------------------
def default_tool() -> dict:
    return {
        "schema_version": 1,
        "id": "sample_tool",
        "version": "1.0.0",
        "display_name": "サンプルツール",
        "summary": "テスト用の最小ツール",
        "inputs": {"accepts": [".txt"], "min": 1, "max": 1},
        "outputs": {"produces": [], "may_be_empty": True},
        "requires": {"core_api": ">=1,<2", "packages": [], "models": []},
        "permissions": {"ollama": False},
        "timeout_seconds": 300,
        "routing": {
            "label": "サンプル実行",
            "patterns": ["サンプル"],
            "examples": ["サンプルを実行して"],
            "suggestions": [{"title": "サンプル", "content": "サンプルを実行して"}],
        },
        "smoke": {
            "make_sample": "samples/make_sample.py",
            "request": "samples/smoke_request.json",
            "expect": {"status": "ok", "files_min": 0},
        },
    }


BENIGN_MAIN = (
    "import json\n"
    "from pathlib import Path\n"
    "\n"
    "def run() -> str:\n"
    "    return json.dumps({'status': 'ok', 'message': 'sample'})\n"
)


def make_files(tool: dict | None = None, main_py: str = BENIGN_MAIN) -> dict[str, str]:
    return {
        "tool.json": json.dumps(tool if tool is not None else default_tool(), ensure_ascii=False),
        "main.py": main_py,
        "README.md": "# サンプルツール\n",
        "tests/test_main.py": "def test_ok():\n    assert True\n",
        "samples/make_sample.py": "print('synthetic sample')\n",
        "samples/smoke_request.json": json.dumps(
            {"contract": 1, "instruction": "サンプルを実行して", "inputs": [], "outdir": "out", "config": {}}
        ),
    }


def manifest_for(files: dict[str, str]) -> str:
    lines = [
        f"{hashlib.sha256(data.encode('utf-8')).hexdigest()}  {name}"
        for name, data in sorted(files.items())
    ]
    return "\n".join(lines) + "\n"


def build_zip(
    tmp_path: Path,
    files: dict[str, str] | None = None,
    *,
    manifest: str | None = None,
    raw_entries: list[tuple[zipfile.ZipInfo | str, bytes]] | None = None,
) -> Path:
    files = make_files() if files is None else files
    package = tmp_path / "pkg.localtool"
    with zipfile.ZipFile(package, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        zf.writestr(
            tv.MANIFEST_NAME, manifest if manifest is not None else manifest_for(files)
        )
        for info, data in raw_entries or []:
            zf.writestr(info, data)
    return package


def verify(tmp_path: Path, package: Path) -> tv.VerifyResult:
    # 1テスト内で複数回検証しても展開先が衝突しないよう、毎回新しい空ディレクトリを使う
    dest = Path(tempfile.mkdtemp(prefix="dest_", dir=tmp_path))
    return tv.verify_package(package, dest)


def problems(result: tv.VerifyResult, stage: str) -> str:
    return "\n".join(e.problem for e in result.errors if e.stage == stage)


# ---------------------------------------------------------------------------
# 正常系と上限定数
# ---------------------------------------------------------------------------
def test_正常な最小パッケージが合格する(tmp_path) -> None:
    result = verify(tmp_path, build_zip(tmp_path))
    assert result.ok, [str(e) for e in result.errors]
    assert result.tool_json["id"] == "sample_tool"
    assert (result.package_dir / "tool.json").is_file()
    assert (result.package_dir / "samples" / "make_sample.py").is_file()


def test_上限は名前付き定数である() -> None:
    assert tv.MAX_ENTRIES == 500
    assert tv.MAX_FILE_BYTES == 100 * 1024 * 1024
    assert tv.MAX_TOTAL_BYTES == 200 * 1024 * 1024


def test_zipでないファイルを拒否する(tmp_path) -> None:
    bogus = tmp_path / "bogus.localtool"
    bogus.write_bytes(b"not a zip")
    result = verify(tmp_path, bogus)
    assert not result.ok
    assert "ZIPとして読めません" in problems(result, tv.STAGE_ZIP)


def test_展開先が空でなければ拒否する(tmp_path) -> None:
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "leftover.txt").write_text("x", encoding="utf-8")
    result = tv.verify_package(build_zip(tmp_path), dest)
    assert not result.ok
    assert "空ではありません" in problems(result, tv.STAGE_EXTRACT)


# ---------------------------------------------------------------------------
# ZIPエントリ検査
# ---------------------------------------------------------------------------
def test_トラバーサル名を拒否する(tmp_path) -> None:
    result = verify(tmp_path, build_zip(tmp_path, raw_entries=[("../evil.txt", b"x")]))
    assert not result.ok
    assert "「..」" in problems(result, tv.STAGE_ZIP)


def test_絶対パスを拒否する(tmp_path) -> None:
    result = verify(tmp_path, build_zip(tmp_path, raw_entries=[("/abs.txt", b"x")]))
    assert not result.ok
    assert "絶対パス" in problems(result, tv.STAGE_ZIP)


def test_コロンを含むパスを拒否する(tmp_path) -> None:
    result = verify(
        tmp_path,
        build_zip(tmp_path, raw_entries=[("c:/x.txt", b"x"), ("a.txt:ads", b"x")]),
    )
    assert not result.ok
    assert problems(result, tv.STAGE_ZIP).count("コロン") == 2


def test_バックスラッシュを拒否する() -> None:
    # Windowsのzipfileは書き込み時も読み取り時も \ を / へ正規化するため、
    # ZIP経由では再現できない。名前検査の関数を直接確かめる
    # (検査自体は、別の実装で作られたZIPへの多重防御として残す)
    errors = tv._entry_name_errors("a\\b.txt")
    assert any("バックスラッシュ" in e.problem for e in errors)


def test_Windows予約名を拒否する(tmp_path) -> None:
    result = verify(
        tmp_path,
        build_zip(tmp_path, raw_entries=[("CON.txt", b"x"), ("sub/com1", b"x")]),
    )
    assert not result.ok
    assert problems(result, tv.STAGE_ZIP).count("予約名") == 2


def test_末尾の空白とピリオドを拒否する(tmp_path) -> None:
    result = verify(
        tmp_path,
        build_zip(tmp_path, raw_entries=[("bad.txt ", b"x"), ("dir./f.txt", b"x")]),
    )
    assert not result.ok
    text = problems(result, tv.STAGE_ZIP)
    assert "空白" in text
    assert "末尾" in text


def test_非ASCIIパスを拒否する(tmp_path) -> None:
    result = verify(tmp_path, build_zip(tmp_path, raw_entries=[("データ.txt", b"x")]))
    assert not result.ok
    assert "非ASCII" in problems(result, tv.STAGE_ZIP)


def test_重複エントリを拒否する(tmp_path) -> None:
    result = verify(
        tmp_path,
        build_zip(tmp_path, raw_entries=[("dup.txt", b"x"), ("dup.txt", b"y")]),
    )
    assert not result.ok
    assert "重複" in problems(result, tv.STAGE_ZIP)


def test_大文字小文字だけ違う衝突を拒否する(tmp_path) -> None:
    result = verify(
        tmp_path,
        build_zip(tmp_path, raw_entries=[("Extra.txt", b"x"), ("extra.txt", b"y")]),
    )
    assert not result.ok
    assert "大文字小文字" in problems(result, tv.STAGE_ZIP)


def test_正規化されていないパスを拒否する(tmp_path) -> None:
    result = verify(
        tmp_path,
        build_zip(tmp_path, raw_entries=[("./x.txt", b"x"), ("a//b.txt", b"x")]),
    )
    assert not result.ok
    assert "正規化" in problems(result, tv.STAGE_ZIP)


def test_暗号化エントリを拒否する() -> None:
    # writestr は書き込み時に暗号化フラグを保持しないため、
    # 属性検査の関数を暗号化フラグ付きの ZipInfo で直接確かめる
    info = zipfile.ZipInfo("secret.txt")
    info.flag_bits |= 0x1
    errors = tv.check_zip_info(info)
    assert any("暗号化" in e.problem for e in errors)


def test_シンボリックリンクを拒否する(tmp_path) -> None:
    info = zipfile.ZipInfo("link.txt")
    info.create_system = 3
    info.external_attr = 0o120777 << 16
    result = verify(tmp_path, build_zip(tmp_path, raw_entries=[(info, b"target")]))
    assert not result.ok
    assert "シンボリックリンク" in problems(result, tv.STAGE_ZIP)


def test_禁止された実行可能ファイルを拒否する(tmp_path) -> None:
    result = verify(
        tmp_path,
        build_zip(tmp_path, raw_entries=[("tool.exe", b"x"), ("run.ps1", b"x")]),
    )
    assert not result.ok
    assert problems(result, tv.STAGE_ZIP).count("同梱が禁止") == 2


def test_エントリ数の上限を超えたら拒否する(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(tv, "MAX_ENTRIES", 3)
    result = verify(tmp_path, build_zip(tmp_path))
    assert not result.ok
    assert "エントリ数が多すぎます" in problems(result, tv.STAGE_ZIP)


def test_1ファイルの上限を超えたら拒否する(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(tv, "MAX_FILE_BYTES", 10)
    files = make_files()
    files["resources/big.txt"] = "x" * 40
    result = verify(tmp_path, build_zip(tmp_path, files))
    assert not result.ok
    assert "1ファイルが大きすぎます" in problems(result, tv.STAGE_ZIP)


def test_非圧縮合計の上限を超えたら拒否する(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(tv, "MAX_TOTAL_BYTES", 100)
    result = verify(tmp_path, build_zip(tmp_path))
    assert not result.ok
    assert "合計が大きすぎます" in problems(result, tv.STAGE_ZIP)


# ---------------------------------------------------------------------------
# 構成・manifest
# ---------------------------------------------------------------------------
def test_必須ファイルの欠けを報告する(tmp_path) -> None:
    files = make_files()
    del files["main.py"]
    result = verify(tmp_path, build_zip(tmp_path, files))
    assert not result.ok
    assert "main.py" in problems(result, tv.STAGE_LAYOUT)


def test_テストが無ければ拒否する(tmp_path) -> None:
    files = make_files()
    del files["tests/test_main.py"]
    result = verify(tmp_path, build_zip(tmp_path, files))
    assert not result.ok
    assert "test_*.py" in problems(result, tv.STAGE_LAYOUT)


def test_manifestの欠けを報告する(tmp_path) -> None:
    files = make_files()
    manifest = manifest_for(files) + ("0" * 64) + "  missing.txt\n"
    result = verify(tmp_path, build_zip(tmp_path, files, manifest=manifest))
    assert not result.ok
    assert "欠け" in problems(result, tv.STAGE_MANIFEST)


def test_manifestの余りを報告する(tmp_path) -> None:
    files = make_files()
    lines = [line for line in manifest_for(files).splitlines() if "README.md" not in line]
    result = verify(tmp_path, build_zip(tmp_path, files, manifest="\n".join(lines) + "\n"))
    assert not result.ok
    assert "余り" in problems(result, tv.STAGE_MANIFEST)


def test_manifestのハッシュ不一致を報告する(tmp_path) -> None:
    files = make_files()
    manifest = manifest_for(files)
    first = manifest.splitlines()[0]
    flipped = ("0" if first[0] != "0" else "1") + first[1:]
    manifest = manifest.replace(first, flipped, 1)
    result = verify(tmp_path, build_zip(tmp_path, files, manifest=manifest))
    assert not result.ok
    assert "ハッシュが一致しません" in problems(result, tv.STAGE_MANIFEST)


def test_manifestが自分自身を列挙したら拒否する(tmp_path) -> None:
    files = make_files()
    manifest = manifest_for(files) + ("0" * 64) + f"  {tv.MANIFEST_NAME}\n"
    result = verify(tmp_path, build_zip(tmp_path, files, manifest=manifest))
    assert not result.ok
    assert "自分自身" in problems(result, tv.STAGE_MANIFEST)


def test_manifestの形式不正を報告する(tmp_path) -> None:
    files = make_files()
    manifest = manifest_for(files) + "garbage line\n"
    result = verify(tmp_path, build_zip(tmp_path, files, manifest=manifest))
    assert not result.ok
    assert "形式が不正" in problems(result, tv.STAGE_MANIFEST)


# ---------------------------------------------------------------------------
# tool.json
# ---------------------------------------------------------------------------
def build_with_tool(tmp_path: Path, mutate) -> tv.VerifyResult:
    tool = default_tool()
    mutate(tool)
    return verify(tmp_path, build_zip(tmp_path, make_files(tool)))


def test_idの規約違反を拒否する(tmp_path) -> None:
    result = build_with_tool(tmp_path, lambda t: t.update(id="AB"))
    assert not result.ok
    assert "id が規約に合いません" in problems(result, tv.STAGE_TOOLJSON)


def test_timeoutの範囲外を拒否する(tmp_path) -> None:
    result = build_with_tool(tmp_path, lambda t: t.update(timeout_seconds=1501))
    assert not result.ok
    assert "timeout_seconds" in problems(result, tv.STAGE_TOOLJSON)


def test_未知の項目を拒否する(tmp_path) -> None:
    result = build_with_tool(tmp_path, lambda t: t.update(certified=True))
    assert not result.ok
    assert "未知の項目" in problems(result, tv.STAGE_TOOLJSON)


def test_必須項目の欠けを報告する(tmp_path) -> None:
    result = build_with_tool(tmp_path, lambda t: t.pop("routing"))
    assert not result.ok
    assert "必須項目がありません: routing" in problems(result, tv.STAGE_TOOLJSON)


def test_承認状態の書き込みを拒否する(tmp_path) -> None:
    result = build_with_tool(
        tmp_path, lambda t: t.update(display_name="【未承認】サンプル")
    )
    assert not result.ok
    assert "承認状態" in problems(result, tv.STAGE_TOOLJSON)


def test_許可リストにある依存は通る(tmp_path) -> None:
    result = build_with_tool(
        tmp_path, lambda t: t["requires"].update(packages=["openpyxl==3.1.5"])
    )
    assert result.ok, [str(e) for e in result.errors]


def test_許可されていない依存を拒否する(tmp_path) -> None:
    result = build_with_tool(
        tmp_path,
        lambda t: t["requires"].update(packages=["openpyxl==9.9.9", "nonexistent-lib==1.0.0"]),
    )
    assert not result.ok
    assert problems(result, tv.STAGE_TOOLJSON).count("許可されていない依存") == 2


def test_ollamaとモデル宣言の食い違いを拒否する(tmp_path) -> None:
    result = build_with_tool(tmp_path, lambda t: t["permissions"].update(ollama=True))
    assert not result.ok
    assert "requires.models が空" in problems(result, tv.STAGE_TOOLJSON)

    result = build_with_tool(
        tmp_path, lambda t: t["requires"].update(models=["gemma4:26b"])
    )
    assert not result.ok
    assert "ollama が false なのに" in problems(result, tv.STAGE_TOOLJSON)


def test_チップがどの判定語にも一致しなければ拒否する(tmp_path) -> None:
    result = build_with_tool(
        tmp_path,
        lambda t: t["routing"].update(
            suggestions=[{"title": "別物", "content": "全く関係ない依頼"}]
        ),
    )
    assert not result.ok
    assert "どの patterns にも一致しません" in problems(result, tv.STAGE_TOOLJSON)


def test_不正な正規表現を拒否する(tmp_path) -> None:
    result = build_with_tool(tmp_path, lambda t: t["routing"].update(patterns=["("]))
    assert not result.ok
    assert "正規表現が不正" in problems(result, tv.STAGE_TOOLJSON)


def test_smokeの参照ファイルが無ければ拒否する(tmp_path) -> None:
    result = build_with_tool(
        tmp_path, lambda t: t["smoke"].update(make_sample="samples/no_such.py")
    )
    assert not result.ok
    assert "make_sample のファイルがありません" in problems(result, tv.STAGE_TOOLJSON)


# ---------------------------------------------------------------------------
# 静的検査(AST)
# ---------------------------------------------------------------------------
def test_禁止importを拒否する(tmp_path) -> None:
    files = make_files(main_py="import subprocess\nimport ctypes\n")
    result = verify(tmp_path, build_zip(tmp_path, files))
    assert not result.ok
    assert problems(result, tv.STAGE_STATIC).count("禁止されたimport") == 2


def test_通信importはollama宣言が無ければ拒否する(tmp_path) -> None:
    files = make_files(main_py="from urllib.request import urlopen\n")
    result = verify(tmp_path, build_zip(tmp_path, files))
    assert not result.ok
    assert "通信系のimport" in problems(result, tv.STAGE_STATIC)


def test_ollama宣言があれば通信importを許す(tmp_path) -> None:
    tool = default_tool()
    tool["permissions"]["ollama"] = True
    tool["requires"]["models"] = ["gemma4:26b"]
    files = make_files(tool, main_py="from urllib.request import urlopen\n")
    result = verify(tmp_path, build_zip(tmp_path, files))
    assert result.ok, [str(e) for e in result.errors]


def test_動的実行と子プロセス呼び出しを拒否する(tmp_path) -> None:
    files = make_files(main_py="import os\nos.system('dir')\neval('1')\n")
    result = verify(tmp_path, build_zip(tmp_path, files))
    assert not result.ok
    text = problems(result, tv.STAGE_STATIC)
    assert "os.system" in text
    assert "eval" in text


def test_機密パスらしき文字列を拒否する(tmp_path) -> None:
    files = make_files(main_py="SECRET_PATH = '.env'\n")
    result = verify(tmp_path, build_zip(tmp_path, files))
    assert not result.ok
    assert "機密パスらしき文字列" in problems(result, tv.STAGE_STATIC)


def test_構文エラーを報告する(tmp_path) -> None:
    files = make_files(main_py="def broken(:\n")
    result = verify(tmp_path, build_zip(tmp_path, files))
    assert not result.ok
    assert "構文エラー" in problems(result, tv.STAGE_STATIC)


def test_samplesやtestsのPythonも検査対象になる(tmp_path) -> None:
    files = make_files()
    files["samples/make_sample.py"] = "import subprocess\n"
    result = verify(tmp_path, build_zip(tmp_path, files))
    assert not result.ok
    assert "samples/make_sample.py" in problems(result, tv.STAGE_STATIC)


def test_エラーには段階と対処が含まれる(tmp_path) -> None:
    result = verify(tmp_path, build_zip(tmp_path, raw_entries=[("../evil.txt", b"x")]))
    assert not result.ok
    error = result.errors[0]
    assert error.stage
    assert error.problem
    assert error.fix
    assert "対処" in str(error)
