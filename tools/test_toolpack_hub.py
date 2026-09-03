"""ハブへの registry 供給源(installed_specs)とプロセス間ロック(job_lock)のテスト。

すべて tmp_path 注入で行い、実際の additional-tools/ には触れない。
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import toolpack_store as ts
from local_tool_bridge import hub, installed_specs
from local_tool_bridge.job_lock import JobLock

ROOT = Path(__file__).resolve().parents[1]

BENIGN_MAIN = '''\
import argparse, json, os
p = argparse.ArgumentParser(); p.add_argument("--request"); a = p.parse_args()
req = json.loads(open(a.request, encoding="utf-8").read())
out = os.path.join(os.path.dirname(a.request), req["outdir"], "result.txt")
open(out, "w", encoding="utf-8").write("done:" + req["instruction"])
print(json.dumps({"status":"ok","message":"できました","files":["result.txt"],
                  "skipped":["未処理1件"],"notes":["補足1件"]}))
'''

USER_ERROR_MAIN = '''\
import argparse, json
p = argparse.ArgumentParser(); p.add_argument("--request"); p.parse_args()
print(json.dumps({"status":"user_error","message":"シート名が見つかりません",
                  "files":[],"skipped":[],"notes":[]}))
'''


def tool_json(tool_id: str, **over) -> dict:
    data = {
        "schema_version": 1, "id": tool_id, "version": "1.0.0",
        "display_name": tool_id, "summary": f"{tool_id} の説明",
        "inputs": {"accepts": [".txt"], "min": 1, "max": 1},
        "outputs": {"produces": [".txt"], "may_be_empty": True},
        "requires": {"core_api": ">=1,<2", "packages": [], "models": []},
        "permissions": {"ollama": False},
        "timeout_seconds": 120,
        "routing": {"label": "実行", "patterns": ["実行"], "examples": ["実行して"],
                     "suggestions": [{"title": "実行", "content": "実行して"}]},
        "smoke": {"make_sample": "samples/make_sample.py",
                   "request": "samples/smoke_request.json",
                   "expect": {"status": "ok", "files_min": 0}},
    }
    data.update(over)
    return data


def install(store: ts.ToolpackStore, tool_id: str, *, main_src: str = BENIGN_MAIN,
            version: str = "1.0.0", meta: dict | None = None, register: bool = True) -> None:
    """検査済みパッケージが配置・登録された状態を作る。"""
    staged = store.new_staging_dir()
    package = staged / ts.PACKAGE_DIR
    package.mkdir()
    (package / "tool.json").write_text(
        json.dumps(meta or tool_json(tool_id), ensure_ascii=False), encoding="utf-8"
    )
    (package / "main.py").write_text(main_src, encoding="utf-8")
    (staged / ts.SOURCE_DIR).mkdir()
    (staged / ts.SOURCE_DIR / ts.SOURCE_NAME).write_bytes(b"zip")
    (staged / ts.GENERATED_DIR).mkdir()
    store.promote(staged, tool_id, version)
    if register:
        store.register(tool_id, version, package_hash="hash")


@pytest.fixture
def store(tmp_path, monkeypatch) -> ts.ToolpackStore:
    instance = ts.ToolpackStore(tmp_path / "additional-tools")
    instance.ensure_layout()
    monkeypatch.setattr(installed_specs, "STORE_ROOT", instance.root)
    return instance


# ---------------------------------------------------------------------------
# registry 供給源
# ---------------------------------------------------------------------------
def test_registryが空なら何も供給しない(store) -> None:
    specs, errors = installed_specs.load()
    assert specs == {} and errors == []


def test_登録済みツールがToolSpecとして出る(store) -> None:
    install(store, "sample_tool")
    specs, errors = installed_specs.load()
    assert errors == []
    spec = specs["sample_tool"]
    assert spec.name == "sample_tool"
    assert spec.summary == "sample_tool の説明"
    assert spec.accepts == (".txt",)
    assert spec.max_files == 1


def test_サービス再起動なしで反映される(store) -> None:
    # 同じプロセスのまま、置く→出る→台帳から外す→消える、が成り立つこと
    assert "late_tool" not in hub.load_specs()
    install(store, "late_tool")
    assert "late_tool" in hub.load_specs()
    store.unregister("late_tool")
    assert "late_tool" not in hub.load_specs()


def test_壊れた項目はそれだけ隔離される(store) -> None:
    install(store, "good_tool")
    install(store, "broken_tool")
    # tool.json を壊す(1枚壊れても他が消えないこと)
    (store.package_dir("broken_tool", "1.0.0") / "tool.json").write_text(
        "{壊れたJSON", encoding="utf-8"
    )
    specs, errors = installed_specs.load()
    assert "good_tool" in specs
    assert "broken_tool" not in specs
    assert any("broken_tool" in message for message in errors)


def test_台帳とtool_jsonのidが食い違えば繋がない(store) -> None:
    install(store, "declared_tool", meta=tool_json("другое_id"))
    specs, errors = installed_specs.load()
    assert specs == {}
    assert any("食い違" in message for message in errors)


def test_名前衝突では既存connectorを残し追加側を拒否する(store) -> None:
    # 既存の接続役と同じ名前で持ち込まれた場合
    install(store, "excel_read")
    specs, errors = hub.discover()
    assert "excel_read" in specs
    # 中身が既存(connectors/excel_read.py)のままであること。
    # 追加ツール側は .txt / オプション無しで宣言しているので、そちらに置き換わっていない
    assert specs["excel_read"].accepts == (".xlsx", ".xlsm")
    assert specs["excel_read"].options == ("operation", "query", "sheet", "range")
    assert any("名前が衝突" in message for message in errors)


def test_登録前に配置しただけでは繋がらない(store) -> None:
    install(store, "staged_only", register=False)
    specs, _ = installed_specs.load()
    assert specs == {}


# ---------------------------------------------------------------------------
# 実行(隔離ランナー経由)
# ---------------------------------------------------------------------------
def test_追加ツールが隔離実行されて結果を返す(store, tmp_path) -> None:
    install(store, "run_tool")
    spec = installed_specs.load()[0]["run_tool"]

    work = tmp_path / "work"
    work.mkdir()
    source = work / "input.txt"
    source.write_text("中身", encoding="utf-8")
    context = hub.RunContext(
        root=ROOT, python=Path(sys.executable), work_dir=work,
        inputs=[source], instruction="やって",
    )
    result = spec.run(context)
    assert result.message == "できました"
    assert result.skipped == ["未処理1件"]  # 対処が要るもの
    assert result.notes == ["補足1件"]      # 対処が要らないもの
    assert [p.name for p in result.files] == ["result.txt"]
    assert result.files[0].read_text(encoding="utf-8") == "done:やって"


def test_user_errorは利用者向けエラーになる(store, tmp_path) -> None:
    install(store, "ue_tool", main_src=USER_ERROR_MAIN)
    spec = installed_specs.load()[0]["ue_tool"]
    work = tmp_path / "work"
    work.mkdir()
    source = work / "input.txt"
    source.write_text("x", encoding="utf-8")
    context = hub.RunContext(
        root=ROOT, python=Path(sys.executable), work_dir=work,
        inputs=[source], instruction="やって",
    )
    # ValueError は app.py の既存経路で 422 になる(台帳 D-5)
    with pytest.raises(ValueError, match="シート名"):
        spec.run(context)


def test_失敗時に生の出力を利用者へ返さない(store, tmp_path) -> None:
    install(store, "bad_tool", main_src="import sys; sys.stderr.write('秘密の痕跡'); sys.exit(3)")
    spec = installed_specs.load()[0]["bad_tool"]
    work = tmp_path / "work"
    work.mkdir()
    source = work / "input.txt"
    source.write_text("x", encoding="utf-8")
    context = hub.RunContext(
        root=ROOT, python=Path(sys.executable), work_dir=work,
        inputs=[source], instruction="やって",
    )
    with pytest.raises(RuntimeError) as caught:
        spec.run(context)
    assert "秘密の痕跡" not in str(caught.value)
    assert "コード:" in str(caught.value)
    # 診断は1行だけ残り、本文も生の出力も含まない
    logs = list(store.logs.glob("run_*.log"))
    assert logs and "秘密の痕跡" not in logs[0].read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# プロセス間ロック
# ---------------------------------------------------------------------------
def test_ロックはプロセス間で効く() -> None:
    lock = JobLock("Local\\minutes-pipeline-test-lock-1")
    assert lock.cross_process, "名前付きミューテックスを作れていない"
    lock.close()


def test_取得中は他プロセスから取れない() -> None:
    name = "Local\\minutes-pipeline-test-lock-2"
    lock = JobLock(name)
    assert lock.acquire(timeout=1)
    try:
        # 別プロセスから同じ名前のロックを取ろうとして失敗すること
        code = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, r"{ROOT}")
            from local_tool_bridge.job_lock import JobLock
            print("taken" if JobLock(r"{name}").acquire(timeout=0) else "busy")
        """)
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
        )
        assert result.stdout.strip() == "busy", result.stderr
    finally:
        lock.release()

    # 解放後は別プロセスから取れる
    code = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, r"{ROOT}")
        from local_tool_bridge.job_lock import JobLock
        print("taken" if JobLock(r"{name}").acquire(timeout=2) else "busy")
    """)
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.stdout.strip() == "taken", result.stderr
    lock.close()


def test_is_busyが実行中を映す() -> None:
    lock = JobLock("Local\\minutes-pipeline-test-lock-3")
    assert lock.is_busy() is False
    assert lock.acquire(timeout=1)
    try:
        # 同じプロセスの同じハンドルは再帰的に取れてしまうため、
        # 実行中かどうかは別プロセスから見る
        code = textwrap.dedent("""
            import sys
            sys.path.insert(0, r"%s")
            from local_tool_bridge.job_lock import JobLock
            print(JobLock(r"Local\\minutes-pipeline-test-lock-3").is_busy())
        """ % ROOT)
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
        )
        assert result.stdout.strip() == "True", result.stderr
    finally:
        lock.release()
    lock.close()


def test_ハブが使うロックはプロセス間ロックである() -> None:
    from local_tool_bridge import app

    assert app._RUN_LOCK.cross_process
