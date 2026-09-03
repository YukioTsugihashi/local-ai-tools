"""toolpack_contract(起動・返却契約)のテスト。すべて tmp_path 内で完結する。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import toolpack_contract as c


def make_out(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    out.mkdir()
    return out


def result_json(**over) -> str:
    data = {"status": "ok", "message": "done", "files": [], "skipped": [], "notes": []}
    data.update(over)
    return json.dumps(data, ensure_ascii=False)


# ---------------------------------------------------------------------------
# request.json 生成
# ---------------------------------------------------------------------------
def test_上限は名前付き定数(tmp_path) -> None:
    assert c.MAX_OUTPUT_FILES == 20
    assert c.MAX_OUTPUT_TOTAL_BYTES == 200 * 1024 * 1024


def test_build_requestは入力を複製し相対パスで書く(tmp_path) -> None:
    src = tmp_path / "源" / "実績.xlsx"
    src.parent.mkdir()
    src.write_bytes(b"data")
    job = tmp_path / "job"
    request_path = c.build_request(
        job, "集計して", [("in-1", "base", src, "実績.xlsx")], {"k": "v"}
    )
    data = json.loads(request_path.read_text(encoding="utf-8"))
    assert data["contract"] == c.CONTRACT_VERSION
    assert data["outdir"] == "out"
    assert data["config"] == {"k": "v"}
    entry = data["inputs"][0]
    assert entry["id"] == "in-1"
    assert entry["role"] == "base"
    assert not Path(entry["path"]).is_absolute()  # ジョブ基準の相対パス
    assert entry["path"].startswith("in/")
    # 複製されており、元ファイルは残っている(変更しない)
    assert (job / entry["path"]).read_bytes() == b"data"
    assert src.read_bytes() == b"data"


def test_同名の入力でも内部名が衝突しない(tmp_path) -> None:
    a = tmp_path / "a" / "同名.txt"
    b = tmp_path / "b" / "同名.txt"
    for path in (a, b):
        path.parent.mkdir(parents=True)
        path.write_text(path.parent.name, encoding="utf-8")
    job = tmp_path / "job"
    c.build_request(
        job, "x",
        [("in-1", None, a, "同名.txt"), ("in-2", None, b, "同名.txt")],
        None,
    )
    stored = sorted(p.name for p in (job / "in").iterdir())
    assert len(stored) == 2 and stored[0] != stored[1]


def test_入力IDの重複を拒否する(tmp_path) -> None:
    src = tmp_path / "x.txt"
    src.write_text("x", encoding="utf-8")
    with pytest.raises(c.ContractError, match="重複"):
        c.build_request(
            tmp_path / "job", "x",
            [("dup", None, src, "x.txt"), ("dup", None, src, "y.txt")], None,
        )


# ---------------------------------------------------------------------------
# 結果JSON検証
# ---------------------------------------------------------------------------
def test_正常な結果を受け取る(tmp_path) -> None:
    out = make_out(tmp_path)
    (out / "result.xlsx").write_bytes(b"x")
    result = c.validate_result(
        result_json(files=["result.xlsx"]), out, allowed_suffixes=(".xlsx",)
    )
    assert result.status == "ok"
    assert [p.name for p in result.files] == ["result.xlsx"]


def test_空の標準出力を拒否する(tmp_path) -> None:
    with pytest.raises(c.ContractError, match="E-EMPTY"):
        c.validate_result("   ", make_out(tmp_path))


def test_複数行の標準出力を拒否する(tmp_path) -> None:
    with pytest.raises(c.ContractError, match="E-MULTILINE"):
        c.validate_result("本文の途中経過\n" + result_json(), make_out(tmp_path))


def test_JSONでない出力を拒否する(tmp_path) -> None:
    with pytest.raises(c.ContractError, match="E-JSON"):
        c.validate_result("not json", make_out(tmp_path))


def test_不正なstatusを拒否する(tmp_path) -> None:
    with pytest.raises(c.ContractError, match="E-STATUS"):
        c.validate_result(result_json(status="success"), make_out(tmp_path))


def test_out外を指すファイルを拒否する(tmp_path) -> None:
    out = make_out(tmp_path)
    with pytest.raises(c.ContractError, match="E-PATH"):
        c.validate_result(result_json(files=["../escape.txt"]), out)


def test_絶対パスのファイルを拒否する(tmp_path) -> None:
    out = make_out(tmp_path)
    with pytest.raises(c.ContractError, match="E-PATH"):
        c.validate_result(result_json(files=[str(tmp_path / "x.txt")]), out)


def test_存在しないファイルを拒否する(tmp_path) -> None:
    out = make_out(tmp_path)
    with pytest.raises(c.ContractError, match="E-MISSING"):
        c.validate_result(result_json(files=["ghost.txt"]), out)


def test_ハードリンクの返却を拒否する(tmp_path) -> None:
    out = make_out(tmp_path)
    real = tmp_path / "secret.txt"  # out の外の実体
    real.write_text("secret", encoding="utf-8")
    link = out / "looks_ok.txt"
    try:
        os.link(real, link)  # out 内に見えるが実体は外(ハードリンク)
    except OSError:
        pytest.skip("この環境ではハードリンクを作成できない")
    with pytest.raises(c.ContractError, match="E-HARDLINK"):
        c.validate_result(result_json(files=["looks_ok.txt"]), out)


def test_ファイル数の上限を超えたら拒否する(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(c, "MAX_OUTPUT_FILES", 2)
    out = make_out(tmp_path)
    names = []
    for i in range(3):
        (out / f"f{i}.txt").write_text("x", encoding="utf-8")
        names.append(f"f{i}.txt")
    with pytest.raises(c.ContractError, match="E-COUNT"):
        c.validate_result(result_json(files=names), out)


def test_宣言外の拡張子を拒否する(tmp_path) -> None:
    out = make_out(tmp_path)
    (out / "sneaky.exe").write_bytes(b"x")
    with pytest.raises(c.ContractError, match="E-SUFFIX"):
        c.validate_result(result_json(files=["sneaky.exe"]), out, allowed_suffixes=(".xlsx",))


def test_skippedとnotesは別々に保たれる(tmp_path) -> None:
    result = c.validate_result(
        result_json(skipped=["A"], notes=["B"]), make_out(tmp_path)
    )
    assert result.skipped == ["A"]
    assert result.notes == ["B"]


def test_合計サイズの上限を超えたら拒否する(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(c, "MAX_OUTPUT_TOTAL_BYTES", 10)
    out = make_out(tmp_path)
    (out / "big.txt").write_text("x" * 40, encoding="utf-8")
    with pytest.raises(c.ContractError, match="E-SIZE"):
        c.validate_result(result_json(files=["big.txt"]), out)
