"""承認表示の見張り(toolpack_name_guard)のテスト。

Open WebUI へのHTTPはすべてモックする。**本番の登録には触れない。**
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import toolpack_name_guard as guard
import toolpack_store as ts

ROOT = Path(__file__).resolve().parents[1]


def tool_json(tool_id: str, display_name: str) -> dict:
    return {
        "schema_version": 1, "id": tool_id, "version": "1.0.0",
        "display_name": display_name, "summary": "説明",
        "inputs": {"accepts": [".txt"], "min": 1, "max": 1},
        "outputs": {"produces": [], "may_be_empty": True},
        "requires": {"core_api": ">=1,<2", "packages": [], "models": []},
        "permissions": {"ollama": False}, "timeout_seconds": 120,
        "routing": {"label": "x", "patterns": ["x"], "examples": ["x"],
                     "suggestions": [{"title": "x", "content": "x"}]},
        "smoke": {"make_sample": "samples/make_sample.py",
                   "request": "samples/smoke_request.json",
                   "expect": {"status": "ok", "files_min": 0}},
    }


@pytest.fixture
def store(tmp_path) -> ts.ToolpackStore:
    instance = ts.ToolpackStore(tmp_path / "additional-tools")
    instance.ensure_layout()
    staged = instance.new_staging_dir()
    package = staged / ts.PACKAGE_DIR
    package.mkdir()
    (package / "tool.json").write_text(
        json.dumps(tool_json("duty_summary", "当直表集計"), ensure_ascii=False),
        encoding="utf-8",
    )
    (staged / ts.GENERATED_DIR).mkdir()
    (staged / ts.GENERATED_DIR / "open_webui_duty_summary_pipe.py").write_text(
        "x", encoding="utf-8"
    )
    instance.promote(staged, "duty_summary", "1.0.0")
    instance.register("duty_summary", "1.0.0", package_hash="h")
    return instance


class FakeClient:
    def __init__(self, function_name: str | None, model_name: str | None) -> None:
        self.function_name = function_name
        self.model_name = model_name
        self.deployed: list[str] = []

    def list_entries(self, plugin_type):
        return [{"id": "duty_summary", "name": self.function_name}]

    def get_model(self, model_id):
        return {"id": model_id, "name": self.model_name} if self.model_name else None


def install_client(monkeypatch, client: FakeClient) -> None:
    import open_webui_deploy as deploy

    monkeypatch.setattr(deploy, "ApiClient", lambda **kwargs: client)
    monkeypatch.setattr(deploy, "resolve_base_url", lambda: "http://localhost:3000")
    monkeypatch.setattr(deploy, "load_api_key", lambda: "dummy")


# ---------------------------------------------------------------------------
# 検知
# ---------------------------------------------------------------------------
def test_台帳どおりなら何も言わない(store, monkeypatch) -> None:
    install_client(monkeypatch, FakeClient("【未承認】当直表集計", "【未承認】当直表集計"))
    assert guard.check(store) == []


def test_印を外されたら見つける(store, monkeypatch) -> None:
    install_client(monkeypatch, FakeClient("【未承認】当直表集計", "当直表集計"))
    drifted = guard.check(store)
    assert len(drifted) == 1
    assert drifted[0].tool_id == "duty_summary"
    assert drifted[0].expected == "【未承認】当直表集計"
    assert drifted[0].actual == {"モデル設定": "当直表集計"}
    assert "当直表集計" in drifted[0].describe()


def test_両方変えられても見つける(store, monkeypatch) -> None:
    install_client(monkeypatch, FakeClient("勝手な名前", "勝手な名前"))
    drifted = guard.check(store)
    assert set(drifted[0].actual) == {"Function", "モデル設定"}


def test_取得できない側は食い違い扱いしない(store, monkeypatch) -> None:
    install_client(monkeypatch, FakeClient("【未承認】当直表集計", None))
    assert guard.check(store) == []


def test_OpenWebUIへ繋がらなければ黙って空を返す(store, monkeypatch) -> None:
    # 見張りが本処理を止めてはいけない(未確認の扱いは呼び出し側の責任)
    import open_webui_deploy as deploy

    def broken(**kwargs):
        raise OSError("接続できません")

    monkeypatch.setattr(deploy, "ApiClient", broken)
    assert guard.check(store) == []


def test_台帳が無い環境では何もしない(tmp_path, monkeypatch) -> None:
    empty = ts.ToolpackStore(tmp_path / "additional-tools")
    empty.ensure_layout()
    assert guard.check(empty) == []


def test_承認済みなら印が無いのが正しい(store, monkeypatch) -> None:
    data = store.load_registry()
    entry = data["tools"]["duty_summary"]
    entry["versions"][entry["active_version"]]["approved"] = True  # 承認は版ごと
    entry["status"] = "approved"
    store._write_registry(data)
    install_client(monkeypatch, FakeClient("当直表集計", "当直表集計"))
    assert guard.check(store) == []
    # 逆に印が付いたままなら食い違い
    install_client(monkeypatch, FakeClient("【未承認】当直表集計", "当直表集計"))
    assert len(guard.check(store)) == 1


# ---------------------------------------------------------------------------
# 戻す
# ---------------------------------------------------------------------------
def test_生成Pipeを配り直して戻す(store, monkeypatch) -> None:
    client = FakeClient("【未承認】当直表集計", "当直表集計")
    install_client(monkeypatch, client)
    import open_webui_deploy as deploy

    def command_deploy(api, path, plugin_id_override, apply):
        client.deployed.append(Path(path).name)
        return 0

    monkeypatch.setattr(deploy, "command_deploy", command_deploy)

    results = guard.restore(guard.check(store), store)
    assert [ok for _, ok, _ in results] == [True]
    assert client.deployed == ["open_webui_duty_summary_pipe.py"]


def test_戻せなかったら理由を返す(store, monkeypatch) -> None:
    client = FakeClient("【未承認】当直表集計", "当直表集計")
    install_client(monkeypatch, client)
    import open_webui_deploy as deploy

    monkeypatch.setattr(deploy, "command_deploy", lambda *a, **k: 1)
    results = guard.restore(guard.check(store), store)
    assert [ok for _, ok, _ in results] == [False]
    assert "失敗" in results[0][2]


def test_生成Pipeが無ければ戻さずに知らせる(store, monkeypatch) -> None:
    (store.generated_dir("duty_summary", "1.0.0") / "open_webui_duty_summary_pipe.py").unlink()
    client = FakeClient("【未承認】当直表集計", "当直表集計")
    install_client(monkeypatch, client)
    results = guard.restore(guard.check(store), store)
    assert results[0][1] is False
    assert "見つかりません" in results[0][2]


def test_子プロセスは黒い窓を出さない(monkeypatch) -> None:
    """自動実行(pythonw)から呼ばれるため、子プロセスは窓を出してはいけない。

    5分ごとの見張りが `docker port` を素で起動しており、そのたびに
    一瞬だけコンソールが開いていた(2026-08-31に利用者が気づいた)。
    """
    import subprocess

    import open_webui_deploy as deploy

    seen: list[int] = []
    original = subprocess.run

    def traced(*args, **kwargs):
        seen.append(kwargs.get("creationflags", 0))
        return original(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", traced)
    monkeypatch.delenv("OPEN_WEBUI_URL", raising=False)
    deploy.resolve_base_url()

    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert seen, "docker の呼び出しが行われていない(検査になっていない)"
    assert all(flags & no_window for flags in seen)


def test_doctorの子プロセスも窓を出さない(monkeypatch) -> None:
    import subprocess

    import doctor

    seen: list[int] = []
    original = subprocess.run
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: (seen.append(k.get("creationflags", 0)), original(*a, **k))[1],
    )
    doctor._run_command(["git", "rev-parse", "--short", "HEAD"])
    assert seen and all(flags & getattr(subprocess, "CREATE_NO_WINDOW", 0) for flags in seen)


def test_承認状態そのものは変えない(store, monkeypatch) -> None:
    # 戻すのは表示名だけ。承認・承認解除は人の操作でしか行わない
    client = FakeClient("【未承認】当直表集計", "当直表集計")
    install_client(monkeypatch, client)
    import open_webui_deploy as deploy

    monkeypatch.setattr(deploy, "command_deploy", lambda *a, **k: 0)
    guard.restore(guard.check(store), store)
    assert store.load_registry()["tools"]["duty_summary"]["status"] == "unapproved"
