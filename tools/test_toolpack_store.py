"""toolpack_store(追加ツールの保存領域とregistry)のテスト。

すべて tmp_path 注入で実行し、実際の additional-tools/ やユーザープロファイルには触れない。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import toolpack_store as ts


@pytest.fixture
def store(tmp_path) -> ts.ToolpackStore:
    instance = ts.ToolpackStore(tmp_path / "additional-tools")
    instance.ensure_layout()
    return instance


def make_staged(store: ts.ToolpackStore) -> Path:
    """installed へ移せる形の staging ディレクトリを作る。"""
    staged = store.new_staging_dir()
    (staged / ts.SOURCE_DIR).mkdir()
    (staged / ts.SOURCE_DIR / ts.SOURCE_NAME).write_bytes(b"zip-bytes")
    (staged / ts.PACKAGE_DIR).mkdir()
    (staged / ts.PACKAGE_DIR / "tool.json").write_text("{}", encoding="utf-8")
    (staged / ts.GENERATED_DIR).mkdir()
    (staged / ts.INSTALL_JSON).write_text("{}", encoding="utf-8")
    return staged


# ---------------------------------------------------------------------------
# レイアウトと定数
# ---------------------------------------------------------------------------
def test_レイアウト生成は何度でも安全(store) -> None:
    store.ensure_layout()
    for directory in (
        store.incoming, store.staging, store.installed,
        store.rejected, store.logs, store.registry_dir,
    ):
        assert directory.is_dir()


def test_保持期限は名前付き定数である() -> None:
    assert ts.LOG_RETENTION_DAYS == 14


def test_不正なIDや版はパスに使わせない(store) -> None:
    with pytest.raises(ts.StoreError):
        store.version_dir("../escape", "1.0.0")
    with pytest.raises(ts.StoreError):
        store.version_dir("valid_id", "../1.0.0")
    with pytest.raises(ts.StoreError):
        store.version_dir("AB", "1.0.0")


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------
def test_registryが無ければ空の骨組みを返す(store) -> None:
    data = store.load_registry()
    assert data["tools"] == {}
    assert not store.registry_path.exists()  # 読むだけでは作らない


def test_登録は未承認として原子的に書かれる(store) -> None:
    store.register("sample_tool", "1.0.0", package_hash="abc123")
    data = json.loads(store.registry_path.read_text(encoding="utf-8"))
    entry = data["tools"]["sample_tool"]
    assert entry["active_version"] == "1.0.0"
    assert entry["status"] == "unapproved"
    assert entry["versions"]["1.0.0"]["package_hash"] == "abc123"
    # 一時ファイルが残っていない(temp + os.replace)
    assert not list(store.registry_dir.glob("*.tmp"))


def test_同じIDの重複登録を拒否する(store) -> None:
    store.register("sample_tool", "1.0.0", package_hash="a")
    with pytest.raises(ts.StoreError, match="登録済み"):
        store.register("sample_tool", "2.0.0", package_hash="b")


def test_書き込み失敗ではregistryが変わらない(store, monkeypatch) -> None:
    store.register("sample_tool", "1.0.0", package_hash="a")
    before = store.registry_path.read_text(encoding="utf-8")

    def broken_replace(src, dst):
        raise OSError("simulated crash")

    monkeypatch.setattr(ts.os, "replace", broken_replace)
    with pytest.raises(OSError):
        store.register("other_tool", "1.0.0", package_hash="b")
    monkeypatch.undo()
    assert store.registry_path.read_text(encoding="utf-8") == before
    assert not list(store.registry_dir.glob("*.tmp"))  # 一時ファイルも残さない


def test_ロールバック用のunregister(store) -> None:
    store.register("sample_tool", "1.0.0", package_hash="a")
    store.unregister("sample_tool")
    assert store.active_tools() == {}
    store.unregister("no_such_tool")  # 無くてもエラーにしない


def test_有効ツール一覧はregistryだけを見る(store) -> None:
    # ディレクトリを置いただけでは有効にならない(registryが唯一のスイッチ)
    staged = make_staged(store)
    store.promote(staged, "sample_tool", "1.0.0")
    assert store.active_tools() == {}
    store.register("sample_tool", "1.0.0", package_hash="a")
    assert store.active_tools()["sample_tool"]["version"] == "1.0.0"


# ---------------------------------------------------------------------------
# staging → installed
# ---------------------------------------------------------------------------
def test_promoteは版ディレクトリへ原子的に移す(store) -> None:
    staged = make_staged(store)
    target = store.promote(staged, "sample_tool", "1.0.0")
    assert target == store.version_dir("sample_tool", "1.0.0")
    assert (target / ts.PACKAGE_DIR / "tool.json").is_file()
    assert (target / ts.SOURCE_DIR / ts.SOURCE_NAME).is_file()
    assert not staged.exists()  # 移動なのでコピーが残らない


def test_同じ版への二重配置を拒否する(store) -> None:
    store.promote(make_staged(store), "sample_tool", "1.0.0")
    with pytest.raises(ts.StoreError, match="配置済み"):
        store.promote(make_staged(store), "sample_tool", "1.0.0")


def test_promote後にregistry未更新で落ちても不活性で無害(store) -> None:
    # クラッシュ模擬: promote まで済んで register 前に中断した状態
    store.promote(make_staged(store), "sample_tool", "1.0.0")
    assert store.active_tools() == {}  # 有効にはなっていない
    orphans = store.orphan_versions()
    assert [(o.tool_id, o.version) for o in orphans] == [("sample_tool", "1.0.0")]
    # 孤立版は自動削除されない
    store.sweep_staging(older_than_seconds=0)
    assert store.version_dir("sample_tool", "1.0.0").is_dir()


# ---------------------------------------------------------------------------
# 後始末(成功・失敗)
# ---------------------------------------------------------------------------
def test_成功後はincomingとstagingを削除する(store) -> None:
    incoming = store.incoming / "pkg.localtool"
    incoming.write_bytes(b"zip")
    staged = make_staged(store)
    store.finish_success(incoming, staged)
    assert not incoming.exists()
    assert not staged.exists()


def test_rejectは診断だけ残しコピーと展開物を消す(store) -> None:
    incoming = store.incoming / "pkg.localtool"
    incoming.write_bytes(b"zip")
    staged = make_staged(store)
    diagnosis = store.reject(
        incoming, staged, tool_hint="sample_tool",
        diagnosis="[ZIP検査] エントリ名が不正です",
    )
    assert diagnosis.is_file()
    assert "ZIP検査" in diagnosis.read_text(encoding="utf-8")
    assert not incoming.exists()  # 院内コピーは削除(原本はUSB側)
    assert not staged.exists()  # 展開ツリーも残さない
    assert list(store.rejected.iterdir()) == [diagnosis]  # 残るのは診断だけ


def test_rejectのヒントは安全な文字へ丸める(store) -> None:
    diagnosis = store.reject(None, None, tool_hint="../攻撃/../x", diagnosis="d")
    assert diagnosis.parent == store.rejected
    assert "/" not in diagnosis.name and ".." not in diagnosis.name


# ---------------------------------------------------------------------------
# 掃除と報告
# ---------------------------------------------------------------------------
def test_sweep_stagingは古い未完了物だけ消す(store) -> None:
    old = store.new_staging_dir()
    fresh = store.new_staging_dir()
    past = time.time() - 2 * ts.STAGING_MAX_AGE_SECONDS
    os.utime(old, (past, past))
    removed = store.sweep_staging()
    assert old in removed
    assert not old.exists()
    assert fresh.exists()  # 実行中かもしれない新しいものは残す


def test_sweep_logsは保持期限を過ぎたものだけ消す(store) -> None:
    old_log = store.logs / "old.txt"
    new_log = store.logs / "new.txt"
    old_log.write_text("x", encoding="utf-8")
    new_log.write_text("x", encoding="utf-8")
    past = time.time() - (ts.LOG_RETENTION_DAYS + 1) * 24 * 3600
    os.utime(old_log, (past, past))
    removed = store.sweep_logs()
    assert old_log in removed
    assert not old_log.exists()
    assert new_log.exists()


def test_孤立版の列挙は削除しない(store) -> None:
    store.promote(make_staged(store), "tool_one", "1.0.0")
    store.register("tool_one", "1.0.0", package_hash="a")
    store.promote(make_staged(store), "tool_two", "1.0.0")  # registry未登録
    orphans = store.orphan_versions()
    assert [(o.tool_id, o.version) for o in orphans] == [("tool_two", "1.0.0")]
    assert store.version_dir("tool_two", "1.0.0").is_dir()  # 列挙しても消えない
