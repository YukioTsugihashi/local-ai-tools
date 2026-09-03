from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(autouse=True)
def _isolated_oplog(monkeypatch, tmp_path) -> None:
    # テストが実運用のlogs/へ運用ログを書き込まないよう常に隔離する
    monkeypatch.setenv("BRIDGE_LOG_DIR", str(tmp_path / "oplog"))


@pytest.fixture(autouse=True)
def _isolated_archive(monkeypatch, tmp_path) -> None:
    # テストが実運用の output/ と work/imports/ へ控えを書き込まないよう隔離する
    # (隔離を入れる前は、テストを流すたびに本物のフォルダへファイルが増えていた)
    from app import archive

    monkeypatch.setattr(archive, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(archive, "IMPORT_DIR", tmp_path / "imports")


@pytest.fixture
def api_client() -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()

