"""追加ツール領域の外部媒体バックアップ(台帳 D-17)のテスト。

**守りたいのは承認の記録である。** ツール本体は持ち込み元のUSBから
入れ直せるが、誰がいつ承認したかはここにしか無い。

見るのは次:

- 書いたあと読み直して確かめる。**確かめられなければ成功にしない**
- 戻すときに壊さない。壊れた書庫で上書きしない。戻す前に安全用を取る
- 作業中のもの(staging / incoming)は入れない
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

import doctor
import toolpack_backup as backup
import toolpack_store as ts


@pytest.fixture
def store(tmp_path) -> ts.ToolpackStore:
    instance = ts.ToolpackStore(tmp_path / "additional-tools")
    instance.ensure_layout()
    package = instance.package_dir("duty_summary", "1.0.0")
    package.mkdir(parents=True)
    (package / "main.py").write_text("print('x')", encoding="utf-8")
    instance.register("duty_summary", "1.0.0", package_hash="h")
    # 承認の記録(**これが守りたいもの**)
    data = instance.load_registry()
    record = data["tools"]["duty_summary"]["versions"]["1.0.0"]
    record["approved"] = True
    record["approval_history"] = [
        {"event": "approved", "at": "2026-09-04T10:00:00", "by": ["甲野", "乙野"]}
    ]
    instance._write_registry(data)
    return instance


@pytest.fixture
def usb(tmp_path) -> Path:
    target = tmp_path / "usb"
    target.mkdir()
    return target


# ---------------------------------------------------------------------------
# 書き出す
# ---------------------------------------------------------------------------
def test_書き出せる(store, usb) -> None:
    target = backup.save(usb, store)
    assert target.exists() and target.suffix == ".zip"
    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
    assert "registry/registry.json" in names
    assert "installed/duty_summary/1.0.0/package/main.py" in names


def test_承認の記録が入る(store, usb) -> None:
    """ツール本体は入れ直せる。**入れ直せないのはこれ。**"""
    target = backup.save(usb, store)
    with zipfile.ZipFile(target) as archive:
        data = json.loads(archive.read("registry/registry.json").decode("utf-8"))
    history = data["tools"]["duty_summary"]["versions"]["1.0.0"]["approval_history"]
    assert history[0]["by"] == ["甲野", "乙野"]


def test_作業中のものは入れない(store, usb) -> None:
    (store.staging / "途中.txt").write_text("x", encoding="utf-8")
    (store.incoming / "持ち込み.localtool").write_text("x", encoding="utf-8")
    with zipfile.ZipFile(backup.save(usb, store)) as archive:
        names = archive.namelist()
    assert not any(name.startswith(("staging/", "incoming/")) for name in names)


def test_保存先が無ければ断る(store, tmp_path) -> None:
    with pytest.raises(backup.BackupError) as caught:
        backup.save(tmp_path / "挿さっていないUSB", store)
    assert "USBメモリが挿さっている" in str(caught.value)


def test_何も入っていなければ断る(tmp_path, usb) -> None:
    empty = ts.ToolpackStore(tmp_path / "空")
    empty.ensure_layout()
    with pytest.raises(backup.BackupError) as caught:
        backup.save(usb, empty)
    assert "入れるものがありません" in str(caught.value)


def test_守っている中身を見せる(store) -> None:
    assert backup.approval_summary(store) == "ツール 1 件 / 承認の記録 1 件"


# ---------------------------------------------------------------------------
# 確かめる
# ---------------------------------------------------------------------------
def test_書き出した直後は一致している(store, usb) -> None:
    assert backup.verify(backup.save(usb, store)) == []


def test_中身を差し替えられたら気づく(store, usb) -> None:
    target = backup.save(usb, store)
    # 書庫の中の台帳だけを差し替える
    with zipfile.ZipFile(target) as archive:
        items = {name: archive.read(name) for name in archive.namelist()}
    items["registry/registry.json"] = b'{"tools": {}}'
    with zipfile.ZipFile(target, "w") as archive:
        for name, blob in items.items():
            archive.writestr(name, blob)
    problems = backup.verify(target)
    assert any("中身が違います" in item for item in problems)


def test_壊れた書庫は読めないと言う(usb) -> None:
    broken = usb / "こわれ.zip"
    broken.write_bytes("これはZIPではない".encode("utf-8"))
    assert backup.verify(broken)


def test_覚えのないものが増えていたら言う(store, usb) -> None:
    target = backup.save(usb, store)
    with zipfile.ZipFile(target, "a") as archive:
        archive.writestr("installed/勝手なもの.txt", "x")
    assert any("覚えのないもの" in item for item in backup.verify(target))


# ---------------------------------------------------------------------------
# 戻す
# ---------------------------------------------------------------------------
def test_戻せる(store, usb) -> None:
    target = backup.save(usb, store)
    # 消えた状況を作る
    import shutil

    shutil.rmtree(store.installed)
    shutil.rmtree(store.registry_dir)

    backup.restore(target, store)
    assert store.package_dir("duty_summary", "1.0.0").is_dir()
    history = (
        store.load_registry()["tools"]["duty_summary"]["versions"]["1.0.0"]
        ["approval_history"]
    )
    assert history[0]["by"] == ["甲野", "乙野"]


def test_戻す前に安全用を取る(store, usb) -> None:
    """**戻す操作で、いまの状態を失わせない。**"""
    target = backup.save(usb, store)
    safety = backup.restore(target, store)
    assert safety is not None and safety.exists()
    assert backup.verify(safety) == []


def test_壊れた書庫では戻さない(store, usb) -> None:
    broken = usb / "こわれ.zip"
    broken.write_bytes("ZIPではない".encode("utf-8"))
    with pytest.raises(backup.BackupError) as caught:
        backup.restore(broken, store)
    assert "戻しません" in str(caught.value)
    assert store.load_registry()["tools"], "元の台帳が消えている"


def test_台帳の入っていない書庫では戻さない(store, usb) -> None:
    target = usb / "台帳なし.zip"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("installed/なにか.txt", "x")
        archive.writestr(backup.MANIFEST_NAME, json.dumps({"sha256": {}}))
    with pytest.raises(backup.BackupError) as caught:
        backup.restore(target, store)
    assert "覚えのないもの" in str(caught.value) or "registry" in str(caught.value)


def test_台帳が壊れた書庫では戻さない(store, usb) -> None:
    """**壊れたものを上書きしない。**"""
    import hashlib

    target = usb / "台帳こわれ.zip"
    body = "これはJSONではない".encode("utf-8")
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("registry/registry.json", body)
        archive.writestr(backup.MANIFEST_NAME, json.dumps({
            "sha256": {"registry/registry.json": hashlib.sha256(body).hexdigest()}
        }))
    with pytest.raises(backup.BackupError) as caught:
        backup.restore(target, store)
    assert "壊れています" in str(caught.value)
    assert store.load_registry()["tools"], "元の台帳が消えている"


def test_無い書庫は断る(store, usb) -> None:
    with pytest.raises(backup.BackupError):
        backup.restore(usb / "無い.zip", store)


# ---------------------------------------------------------------------------
# 自己診断
# ---------------------------------------------------------------------------
def test_取っていなければ注意(store, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_toolpack_store", lambda root: store)
    findings = doctor.check_toolpack_backup(tmp_path)
    assert [f.level for f in findings] == ["注意"]
    assert "取っていません" in findings[0].title
    assert "承認の記録はここにしかなく" in findings[0].detail


def test_取ったあとは日数を出す(store, usb, tmp_path, monkeypatch) -> None:
    backup.save(usb, store)
    monkeypatch.setattr(doctor, "_toolpack_store", lambda root: store)
    findings = doctor.check_toolpack_backup(tmp_path)
    assert [f.level for f in findings] == ["OK"]
    assert "0 日前" in findings[0].title


def test_古くなったら注意(store, usb, tmp_path, monkeypatch) -> None:
    backup.save(usb, store)
    record = json.loads(
        (store.registry_dir / backup.LAST_BACKUP_FILE).read_text(encoding="utf-8")
    )
    record["at"] = "2020-01-01T00:00:00"
    (store.registry_dir / backup.LAST_BACKUP_FILE).write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(doctor, "_toolpack_store", lambda root: store)
    findings = doctor.check_toolpack_backup(tmp_path)
    assert [f.level for f in findings] == ["注意"]
    assert "日前です" in findings[0].title


def test_何も入っていなければ何も言わない(tmp_path, monkeypatch) -> None:
    empty = ts.ToolpackStore(tmp_path / "空")
    empty.ensure_layout()
    monkeypatch.setattr(doctor, "_toolpack_store", lambda root: empty)
    assert doctor.check_toolpack_backup(tmp_path) == []


def test_自己診断に入っている() -> None:
    import inspect

    assert "check_toolpack_backup" in inspect.getsource(doctor.collect_findings)
