r"""追加ツール領域(additional-tools/)の外部媒体バックアップ。

設計の正本は TOOL_PACKAGE_DECISIONS.md(D-17)。

使い方:
  ...python.exe tools\toolpack_backup.py save E:\backup
  ...python.exe tools\toolpack_backup.py restore E:\backup\additional-tools-....zip
  ...python.exe tools\toolpack_backup.py verify  E:\backup\additional-tools-....zip

**なぜ要るか**

`additional-tools/` は Git 管理外なので `push local` のバックアップに乗らない。
しかもその `push local` の宛先は同じドライブにあるため、
ディスクが壊れると両方まとめて消える。

ツール本体は持ち込み元のUSBにある原本から入れ直せる。
**入れ直せないのは registry の承認の記録である。**
誰がいつ承認したかは決裁の記録であり、ここにしか無い。

**何を入れるか**

| 入れる | 理由 |
|---|---|
| `installed/` | ツール本体・版ごとの原本・生成Pipe |
| `registry/` | どの版が有効か、承認の記録、承認の決まり |
| `rejected/` | 失敗の診断(個人情報を含まない規約。D-9) |

`staging/` と `incoming/` は作業中のもので、戻す意味が無いので入れない。
`logs/` は14日で消える運用ログなので入れない。

**確かめてから成功と言う**

書いたあと必ず読み直し、1件ずつ内容が一致することを確かめる。
確かめられなければ成功にしない(台帳 D-10 と同じ考え方)。

**戻すときは壊さない**

戻す前に、いまの状態を安全用として書き出す。
registry が読める形かを先に検査し、**壊れたものを上書きしない**。
管理操作が動いている間は触らない(プロセス間ロックを取る)。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
import zipfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import toolpack_store  # noqa: E402

# 入れるもの。**作業中のものと自然に消えるものは入れない**
BACKUP_DIRS = ("installed", "registry", "rejected")
NAME_PREFIX = "additional-tools-"
MANIFEST_NAME = "backup.json"
# 最後に取った日時の記録。registry と一緒に置く(次のバックアップにも入る)
LAST_BACKUP_FILE = "last_backup.json"


class BackupError(RuntimeError):
    """利用者が直せる失敗。理由と対処を持つ。"""


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            sha.update(chunk)
    return sha.hexdigest()


def _collect(store: toolpack_store.ToolpackStore) -> list[tuple[Path, str]]:
    """入れるファイルを (実体, 書庫内の名前) で集める。"""
    items: list[tuple[Path, str]] = []
    for name in BACKUP_DIRS:
        base = store.root / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file():
                items.append((path, path.relative_to(store.root).as_posix()))
    return items


def approval_summary(store: toolpack_store.ToolpackStore) -> str:
    """承認の記録が何件入るか(**守っている中身を見せる**)。"""
    try:
        tools = store.load_registry().get("tools") or {}
    except Exception:
        return "台帳を読めませんでした"
    records = 0
    for entry in tools.values():
        for record in (entry.get("versions") or {}).values():
            records += len(record.get("approval_history") or [])
    return f"ツール {len(tools)} 件 / 承認の記録 {records} 件"


def save(target_dir: Path, store: toolpack_store.ToolpackStore | None = None) -> Path:
    """USB等へ書き出す。**書いたあと読み直して確かめる。**"""
    store = store or toolpack_store.ToolpackStore()
    target_dir = Path(target_dir)
    if not target_dir.is_dir():
        raise BackupError(
            f"保存先がありません: {target_dir}\n"
            "USBメモリが挿さっているか、ドライブ文字が合っているか確かめてください"
        )
    if not store.root.is_dir():
        raise BackupError(f"追加ツール領域がありません: {store.root}")

    items = _collect(store)
    if not items:
        raise BackupError("入れるものがありません(まだ何も入っていません)")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"{NAME_PREFIX}{stamp}.zip"
    expected = {name: _digest(path) for path, name in items}

    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for path, name in items:
                archive.write(path, name)
            archive.writestr(MANIFEST_NAME, json.dumps({
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "source": str(store.root),
                "files": len(items),
                "sha256": expected,
            }, ensure_ascii=False, indent=2))
    except OSError as error:
        target.unlink(missing_ok=True)
        raise BackupError(f"書き出せませんでした: {error}") from error

    problems = verify(target, expected)
    if problems:
        raise BackupError(
            "書き出しましたが、中身を確かめられませんでした。\n"
            + "\n".join(f"  - {item}" for item in problems)
            + f"\n{target} は残してあります。別の媒体で取り直してください"
        )
    _record_last_backup(store, target)
    return target


def verify(archive_path: Path, expected: dict[str, str] | None = None) -> list[str]:
    """書庫を読み直して確かめる。合っていない点だけ返す。"""
    problems: list[str] = []
    try:
        with zipfile.ZipFile(archive_path) as archive:
            broken = archive.testzip()
            if broken is not None:
                return [f"壊れています: {broken}"]
            recorded = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
            wanted = expected if expected is not None else (recorded.get("sha256") or {})
            names = set(archive.namelist()) - {MANIFEST_NAME}
            for name, digest in wanted.items():
                if name not in names:
                    problems.append(f"入っていません: {name}")
                    continue
                if hashlib.sha256(archive.read(name)).hexdigest() != digest:
                    problems.append(f"中身が違います: {name}")
            for extra in sorted(names - set(wanted)):
                problems.append(f"覚えのないものが入っています: {extra}")
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        return [f"読めません({type(error).__name__}: {error})"]
    return problems


def _record_last_backup(store: toolpack_store.ToolpackStore, target: Path) -> None:
    """最後に取った日時を残す(doctor が「N日前」と言えるように)。"""
    try:
        store.registry_dir.mkdir(parents=True, exist_ok=True)
        (store.registry_dir / LAST_BACKUP_FILE).write_text(
            json.dumps({
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "target": str(target),
            }, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass   # 記録できなくてもバックアップ自体は成功している


def last_backup(store: toolpack_store.ToolpackStore) -> dict | None:
    """最後に取ったバックアップの記録。無ければ None。"""
    path = store.registry_dir / LAST_BACKUP_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return data if isinstance(data, dict) and data.get("at") else None


def restore(archive_path: Path, store: toolpack_store.ToolpackStore | None = None,
            *, safety_dir: Path | None = None) -> Path | None:
    """書庫から戻す。**戻す前に、いまの状態を安全用として書き出す。**"""
    store = store or toolpack_store.ToolpackStore()
    archive_path = Path(archive_path)
    if not archive_path.is_file():
        raise BackupError(f"その書庫がありません: {archive_path}")

    problems = verify(archive_path)
    if problems:
        raise BackupError(
            "書庫を確かめられなかったので戻しません。\n"
            + "\n".join(f"  - {item}" for item in problems)
        )

    # **壊れた台帳を上書きしない。** 戻す前に形を見る
    try:
        with zipfile.ZipFile(archive_path) as archive:
            raw = archive.read("registry/registry.json").decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("tools"), dict):
            raise ValueError("tools がありません")
    except KeyError:
        raise BackupError("書庫に registry/registry.json が入っていません") from None
    except (ValueError, UnicodeDecodeError) as error:
        raise BackupError(f"書庫の台帳が壊れています: {error}") from error

    safety = None
    # **失うものがあるときだけ**安全用を取る。
    # 空のフォルダがあるかどうかではなく、中身があるかで見る
    if _collect(store):
        try:
            safety = save(safety_dir or archive_path.parent, store)
        except BackupError as error:
            raise BackupError(
                f"戻す前の安全用を書き出せませんでした({error})。\n"
                "戻す操作は行いません"
            ) from error

    for name in BACKUP_DIRS:
        shutil.rmtree(store.root / name, ignore_errors=True)
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.namelist():
            if member != MANIFEST_NAME:
                archive.extract(member, store.root)
    store.ensure_layout()
    return safety


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="追加ツール領域を外部媒体へ書き出す・戻す"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    out = sub.add_parser("save", help="USB等へ書き出す")
    out.add_argument("target", help="保存先フォルダ")
    back = sub.add_parser("restore", help="書庫から戻す")
    back.add_argument("archive", help="戻す元の .zip")
    check = sub.add_parser("verify", help="書庫を確かめるだけ")
    check.add_argument("archive")

    args = parser.parse_args(argv)
    store = toolpack_store.ToolpackStore()
    try:
        if args.command == "verify":
            problems = verify(Path(args.archive))
            if problems:
                print("確かめられませんでした:")
                for item in problems:
                    print(f"  - {item}")
                return 1
            print("中身は書き出したときと一致しています。")
            return 0

        # 台帳を変える・読む操作は1件ずつ(台帳 D-10)
        with toolpack_store.admin_lock():
            if args.command == "save":
                print(f"入るもの: {approval_summary(store)}")
                target = save(Path(args.target), store)
                print(f"書き出しました: {target}")
                print(f"  大きさ: {target.stat().st_size / 1024:.0f} KB")
                print("  読み直して中身が一致することを確かめました。")
                return 0
            if args.command == "restore":
                safety = restore(Path(args.archive), store)
                print(f"戻しました: {args.archive}")
                if safety:
                    print(f"  戻す前の状態: {safety}")
                print("  ローカルサービスを再起動すると反映されます。")
                return 0
    except (BackupError, toolpack_store.StoreError) as error:
        print(str(error))
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
