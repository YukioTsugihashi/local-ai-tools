"""管理操作の安全性(Codexレビュー2026-09-01の4点)。

見るのは次の3つ:

1. **目的の状態になったと確かめられたときだけ成功にする。**
   一部しか消えていない・確かめられなかった、を成功に混ぜない。
   失敗したら台帳を戻すだけでなく、**前のPipeも貼り直す**
2. **承認済みの保護はコア側にも要る。** 画面のボタンを暗くするだけでは足りない
3. **管理操作は1件ずつ。** 画面を2つ開いても片方の変更が消えない

Open WebUI へのHTTPはすべてモックする。**本番の登録には触れない。**
"""

from __future__ import annotations

import contextlib
import threading

import pytest

import toolpack_install as installer
import toolpack_manage as manage
import toolpack_store as ts
from test_toolpack_install import (  # noqa: F401  fixture をそのまま使う
    build_localtool, deploy_stub, store, tool_json,
)
from test_toolpack_update import add_first, build_update  # noqa: F401


def approve(store, tool_id: str) -> None:
    """承認済みにする。**承認は版ごと**(D-14)なので、版の記録へ印を付ける。"""
    data = store.load_registry()
    entry = data["tools"][tool_id]
    entry["versions"][entry["active_version"]]["approved"] = True
    entry["status"] = "approved"   # ツール単位は有効版から導く控え
    store._write_registry(data)


# ---------------------------------------------------------------------------
# 1. 確かめられたときだけ成功にする
# ---------------------------------------------------------------------------
def test_無効化で一部しか消えなければ成功にせず元へ戻す(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    deploy_stub.undeletable = True   # Function が消えない
    deploy_stub.deployed.clear()

    with pytest.raises(manage.ManageError) as caught:
        manage.command_disable(store, "duty_summary")
    assert "完全には外せませんでした" in str(caught.value)
    # 台帳は有効へ戻し、Pipeも貼り直す(片側だけ止まった状態にしない)
    assert "duty_summary" in store.active_tools()
    assert deploy_stub.deployed, "元のPipeを貼り直していない"


def test_無効化を確かめられなければ成功にしない(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    deploy_stub.openwebui_unreachable = True
    with pytest.raises(manage.Unconfirmed) as caught:
        manage.command_disable(store, "duty_summary")
    assert "確かめられませんでした" in str(caught.value)


def test_無効化後にハブがまだ配っていたら成功にしない(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    # Open WebUI からは消えたが、稼働中のハブがまだ配っている状況
    deploy_stub.present.discard("duty_summary")
    deploy_stub.hub_ids.add("duty_summary")

    original = deploy_stub.hub_ids

    class Sticky(set):
        def discard(self, value):  # 消しても配り続ける
            pass

    deploy_stub.hub_ids = Sticky(original)
    try:
        with pytest.raises(manage.Unconfirmed) as caught:
            manage.command_disable(store, "duty_summary")
        assert "まだ配っています" in str(caught.value)
    finally:
        deploy_stub.hub_ids = set()


def test_削除で一部しか消えなければ台帳から外さない(tmp_path, store, deploy_stub) -> None:
    """外してしまうと、消し残しを管理する手掛かりが無くなる。"""
    add_first(tmp_path, store)
    deploy_stub.undeletable = True
    with pytest.raises(manage.ManageError) as caught:
        manage.command_remove(store, "duty_summary")
    assert "台帳は変えていません" in str(caught.value)
    assert "duty_summary" in store.load_registry()["tools"]


def test_削除でOpenWebUIへ繋がらなければ台帳を変えない(
    tmp_path, store, deploy_stub, monkeypatch
) -> None:
    add_first(tmp_path, store)

    def broken(tool_id):
        raise OSError("繋がらない")

    monkeypatch.setattr(manage, "_remove_from_openwebui", broken)
    with pytest.raises(manage.ManageError):
        manage.command_remove(store, "duty_summary")
    assert "duty_summary" in store.load_registry()["tools"]


def test_版を戻せなければ前のPipeを貼り直す(tmp_path, store, deploy_stub) -> None:
    """台帳だけ戻すと、Open WebUI には切り替え先のPipeが残る。"""
    add_first(tmp_path, store, version="1.0.0")
    installer.install(build_update(tmp_path), apply=True, update=True, store=store)
    deploy_stub.should_fail = True

    with pytest.raises(manage.ManageError):
        manage.command_rollback(store, "duty_summary", "1.0.0")
    assert store.load_registry()["tools"]["duty_summary"]["active_version"] == "1.1.0"


def test_戻したあと確かめられなければ元へ戻す(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store, version="1.0.0")
    installer.install(build_update(tmp_path), apply=True, update=True, store=store)
    deploy_stub.deployed.clear()
    deploy_stub.hub_unreachable = True

    with pytest.raises(manage.Unconfirmed):
        manage.command_rollback(store, "duty_summary", "1.0.0")
    assert store.load_registry()["tools"]["duty_summary"]["active_version"] == "1.1.0"
    assert deploy_stub.deployed, "前の版のPipeを貼り直していない"


def test_有効化に失敗したらOpenWebUIの残骸を掃除する(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    manage.command_disable(store, "duty_summary")
    deploy_stub.removed.clear()
    deploy_stub.should_fail = True

    with pytest.raises(manage.ManageError):
        manage.command_enable(store, "duty_summary")
    assert store.active_tools() == {}, "無効のままへ戻っていない"
    assert deploy_stub.removed == ["duty_summary"], "残骸を掃除していない"


def test_有効化を確かめられなければ無効へ戻す(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    manage.command_disable(store, "duty_summary")
    deploy_stub.hub_unreachable = True

    with pytest.raises(manage.Unconfirmed):
        manage.command_enable(store, "duty_summary")
    assert store.active_tools() == {}
    assert "duty_summary" in deploy_stub.removed


# ---------------------------------------------------------------------------
# 2. 承認済みの保護はコア側にも要る
# ---------------------------------------------------------------------------
def test_承認済みは無効化できない(tmp_path, store, deploy_stub) -> None:
    """画面のボタンを暗くするだけでは足りない。コマンドからも呼ばれる。"""
    add_first(tmp_path, store)
    approve(store, "duty_summary")
    with pytest.raises(manage.ManageError) as caught:
        manage.command_disable(store, "duty_summary")
    assert "承認済み" in str(caught.value)
    assert "duty_summary" in store.active_tools()


def test_承認済みは削除できない(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    approve(store, "duty_summary")
    with pytest.raises(manage.ManageError):
        manage.command_remove(store, "duty_summary")
    assert "duty_summary" in store.load_registry()["tools"]


def test_承認済みは版を戻せない(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store, version="1.0.0")
    installer.install(build_update(tmp_path), apply=True, update=True, store=store)
    approve(store, "duty_summary")
    with pytest.raises(manage.ManageError):
        manage.command_rollback(store, "duty_summary", "1.0.0")
    assert store.load_registry()["tools"]["duty_summary"]["active_version"] == "1.1.0"


def test_承認済みは入れ替えられない(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store, version="1.0.0")
    approve(store, "duty_summary")
    report = installer.install(
        build_update(tmp_path, version="1.2.0"), apply=True, update=True, store=store
    )
    assert not report.ok
    assert report.failed_stage == installer.STAGE_TARGET
    assert "承認済み" in report.reason
    assert set(store.load_registry()["tools"]["duty_summary"]["versions"]) == {"1.0.0"}


def test_承認は版ごとに見る() -> None:
    """設計では承認は版ごと(D-14)。版ごとの記録があればそちらを優先する。"""
    entry = {
        "active_version": "1.1.0", "status": "approved",
        "versions": {"1.0.0": {"approved": True}, "1.1.0": {"approved": False}},
    }
    assert ts.is_approved(entry) is False          # いま動いている 1.1.0 は未承認
    assert ts.is_approved(entry, "1.0.0") is True  # 1.0.0 は承認済み
    # 版ごとの記録が無ければツール単位の status を見る(移行前の形)
    assert ts.is_approved(
        {"active_version": "1.0.0", "status": "approved", "versions": {"1.0.0": {}}}
    ) is True


# ---------------------------------------------------------------------------
# 3. 管理操作は1件ずつ
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def held_elsewhere():
    """別スレッドでロックを掴んだままにする。

    Windows の名前付きミューテックスは**同じスレッドなら再入できる**ため、
    同一スレッドで取り直しても待たされない。ふさがっている状況を作るには
    別のスレッド(実運用では別プロセス)から掴む必要がある。
    """
    taken = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with ts.admin_lock():
            taken.set()
            release.wait(30)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert taken.wait(10), "ロックを掴めなかった"
    try:
        yield
    finally:
        release.set()
        thread.join(10)


def test_管理操作は同時に動かない(tmp_path, store, deploy_stub, monkeypatch) -> None:
    add_first(tmp_path, store)
    monkeypatch.setattr(ts, "ADMIN_LOCK_WAIT_SECONDS", 1)  # 待ち時間を短くして試す
    with held_elsewhere():
        with pytest.raises(ts.AdminBusy):
            manage.command_disable(store, "duty_summary")
    # 抜けたら普通に通る
    assert manage.command_disable(store, "duty_summary") == 0


def test_取り込みも同時に動かない(tmp_path, store, deploy_stub, monkeypatch) -> None:
    monkeypatch.setattr(ts, "ADMIN_LOCK_WAIT_SECONDS", 1)
    with held_elsewhere():
        report = installer.install(build_update(tmp_path), apply=True, store=store)
    assert not report.ok
    assert report.failed_stage == "他の管理操作"
    assert "管理画面を2つ" in report.fix


def test_検査だけならロックを待たない(tmp_path, store, deploy_stub, monkeypatch) -> None:
    """台帳を変えないので、他の操作を待たせる理由が無い。"""
    monkeypatch.setattr(ts, "ADMIN_LOCK_WAIT_SECONDS", 1)
    with held_elsewhere():
        report = installer.install(build_update(tmp_path), apply=False, store=store)
    assert report.ok, report.reason


def test_ロックはプロセスをまたいで効く() -> None:
    """プロセス内ロックへ落ちていると、画面を2つ開いた場合に効かない。"""
    from local_tool_bridge.job_lock import JobLock

    lock = JobLock(ts.ADMIN_MUTEX_NAME)
    try:
        assert lock.cross_process, "名前付きミューテックスを取れていない"
    finally:
        lock.close()


def test_ツール実行のロックとは別にする() -> None:
    """管理操作が利用者のジョブの終わりを待つ必要はない。"""
    from local_tool_bridge import job_lock

    assert ts.ADMIN_MUTEX_NAME != job_lock.MUTEX_NAME
