"""控え(output/ と work/imports/)の保存。"""

from __future__ import annotations

from datetime import datetime

import pytest

from app import archive


@pytest.fixture(autouse=True)
def _dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(archive, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(archive, "IMPORT_DIR", tmp_path / "imports")
    return tmp_path


def test_成果物は日時付きでoutputへ残る(_dirs) -> None:
    path = archive.save_output("議事録.docx", b"xyz", when=datetime(2026, 8, 18, 9, 5, 0))
    assert path is not None
    assert path.name == "20260818-090500_議事録.docx"
    assert path.read_bytes() == b"xyz"


def test_受け取った中身はimportsへ残る(_dirs) -> None:
    path = archive.save_import("議事録_名簿", "出席者：山田太郎", when=datetime(2026, 8, 18, 9, 5, 0))
    assert path is not None
    assert path.parent.name == "imports"
    assert path.read_text(encoding="utf-8") == "出席者：山田太郎"


def test_中身が空なら残さない(_dirs) -> None:
    """次第や名簿を渡していないときに、空ファイルを増やさない。"""
    assert archive.save_import("議事録_次第", "   ") is None


def test_名前にパス記号があっても外へ出ない(_dirs) -> None:
    """利用者由来の名前をそのまま使うため、フォルダを飛び越えられては困る。"""
    path = archive.save_output("../../逃げ出す:名前.docx", b"x")
    assert path is not None
    assert path.parent == archive.OUTPUT_DIR
    assert "逃げ出す_名前" in path.name


def test_保存できなくても処理を止めない(monkeypatch, _dirs) -> None:
    """控えが取れないことは、利用者の作業を止める理由にならない。"""

    def _boom(*args, **kwargs):
        raise OSError("書き込めません")

    monkeypatch.setattr(archive.Path, "write_bytes", _boom)
    assert archive.save_output("議事録.docx", b"x") is None
