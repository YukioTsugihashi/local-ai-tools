"""生成Pipe(ひな型からの差し込み)と、deploy・doctor の追加ツール対応のテスト。

Open WebUI へのHTTPはすべてモックする。**本番登録も実モデル削除も行わない。**
実機での削除API確認はテスト専用IDで人の立ち会いのもと別途行う。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import doctor
import open_webui_deploy as deploy
import toolpack_pipegen as pipegen
import toolpack_store as ts

ROOT = Path(__file__).resolve().parents[1]


def tool_json(tool_id: str = "duty_summary", **over) -> dict:
    data = {
        "schema_version": 1, "id": tool_id, "version": "1.0.0",
        "display_name": "当直表集計", "summary": "当直回数を集計する",
        "inputs": {"accepts": [".xlsx"], "min": 1, "max": 1},
        "outputs": {"produces": [".xlsx"], "may_be_empty": False},
        "requires": {"core_api": ">=1,<2", "packages": [], "models": []},
        "permissions": {"ollama": False},
        "timeout_seconds": 300,
        "routing": {
            "label": "当直表集計",
            "patterns": ["当直", "集計"],
            "examples": ["当直表を集計して"],
            "suggestions": [{"title": "当直表集計", "subtitle": "回数を数える",
                              "content": "当直表を集計して"}],
        },
        "smoke": {"make_sample": "samples/make_sample.py",
                   "request": "samples/smoke_request.json",
                   "expect": {"status": "ok", "files_min": 1}},
    }
    data.update(over)
    return data


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------
def test_ひな型から生成したPipeが規約を満たす() -> None:
    source = pipegen.render(tool_json())

    # deploy の検査群がそのまま通ること
    assert deploy.detect_plugin_type(source) == "pipe"
    deploy.validate_container_imports(source)  # 例外が出ないこと
    fields = deploy.parse_frontmatter(source)
    suggestions = deploy.parse_suggestions(source)
    deploy.validate_pipe_model_requirements("pipe", fields, suggestions)
    assert deploy.resolve_plugin_id(fields, None) == "duty_summary"

    # チップが自分の判定語へ届くこと(どれにも一致しないチップは作らない)
    mapping, uncovered = deploy.check_suggestion_routing(source, suggestions)
    assert uncovered == []
    assert all(matched for _, matched in mapping)


def test_新規モデルは常に未承認として作られる() -> None:
    fields = deploy.parse_frontmatter(pipegen.render(tool_json()))
    assert fields["title"].startswith(pipegen.UNAPPROVED_PREFIX)
    # パッケージ側の display_name には承認状態を書かせない(付けるのは生成器)
    assert pipegen.UNAPPROVED_PREFIX not in tool_json()["display_name"]


def test_承認済みの表示は初期版では使われない() -> None:
    # 引数としては存在するが、呼び出し側(工程6・7)は常に未承認で作る
    approved = deploy.parse_frontmatter(pipegen.render(tool_json(), approved=True))
    assert not approved["title"].startswith(pipegen.UNAPPROVED_PREFIX)


def test_宣言値が差し込まれる() -> None:
    source = pipegen.render(tool_json())
    assert 'TOOL_NAME = "duty_summary"' in source
    assert 'ACCEPTED_SUFFIXES = (".xlsx",)' in source
    assert "MAX_FILES = 1" in source
    assert "__TOOL_" not in source  # 置換漏れが無いこと


def test_複数の判定語がまとめられる() -> None:
    source = pipegen.render(tool_json())
    patterns = dict(deploy.extract_operation_patterns(source))
    assert "当直" in patterns["run"] and "集計" in patterns["run"]


def test_不正なidを拒否する() -> None:
    with pytest.raises(pipegen.PipeGenError):
        pipegen.render(tool_json("Bad-ID"))


def test_generateがgenerated配下へ書く(tmp_path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "tool.json").write_text(
        json.dumps(tool_json(), ensure_ascii=False), encoding="utf-8"
    )
    target = pipegen.generate(package, tmp_path / "generated")
    assert target.name == "open_webui_duty_summary_pipe.py"
    assert target.parent.name == "generated"


# ---------------------------------------------------------------------------
# deploy の許可パス
# ---------------------------------------------------------------------------
@pytest.fixture
def repo(tmp_path) -> tuple[Path, ts.ToolpackStore]:
    """tools/ と additional-tools/ を持つ疑似リポジトリ。"""
    (tmp_path / "tools").mkdir()
    store = ts.ToolpackStore(tmp_path / "additional-tools")
    store.ensure_layout()
    return tmp_path, store


def install(store: ts.ToolpackStore, tool_id: str, version: str = "1.0.0",
            register: bool = True) -> Path:
    staged = store.new_staging_dir()
    (staged / ts.PACKAGE_DIR).mkdir()
    generated = staged / ts.GENERATED_DIR
    generated.mkdir()
    (generated / pipegen.pipe_filename(tool_id)).write_text("x", encoding="utf-8")
    store.promote(staged, tool_id, version)
    if register:
        store.register(tool_id, version, package_hash="h")
    return store.generated_dir(tool_id, version)


def test_tools直下は従来どおり許可される(repo) -> None:
    root, _ = repo
    target = root / "tools" / "open_webui_sample_pipe.py"
    target.write_text("x", encoding="utf-8")
    assert deploy.validate_target_path(str(target), repo_root=root) == target.resolve()


def test_有効版のgenerated配下を許可する(repo) -> None:
    root, store = repo
    generated = install(store, "duty_summary")
    target = generated / pipegen.pipe_filename("duty_summary")
    assert deploy.validate_target_path(str(target), repo_root=root) == target.resolve()


def test_registryに無い版のgeneratedは拒否する(repo) -> None:
    root, store = repo
    generated = install(store, "unregistered_tool", register=False)
    target = generated / pipegen.pipe_filename("unregistered_tool")
    with pytest.raises(deploy.DeployError, match="限られます"):
        deploy.validate_target_path(str(target), repo_root=root)


def test_無関係な場所は拒否する(repo) -> None:
    root, _ = repo
    stray = root / "stray" / "open_webui_x_pipe.py"
    stray.parent.mkdir()
    stray.write_text("x", encoding="utf-8")
    with pytest.raises(deploy.DeployError):
        deploy.validate_target_path(str(stray), repo_root=root)


# ---------------------------------------------------------------------------
# 削除・無効化(HTTPはモック)
# ---------------------------------------------------------------------------
class FakeClient(deploy.ApiClient):
    """Open WebUI の登録状態を持つ代役。削除で本当に消えるかを再現する。"""

    def __init__(self, *, functions=("duty_summary",), models=("duty_summary",)) -> None:
        self.base_url = "http://localhost:3000"
        self.api_key = "dummy"
        self.calls: list[tuple[str, str, dict | None]] = []
        self.functions = set(functions)
        self.models = set(models)
        self.undeletable: set[str] = set()  # 消えないものを模す

    def request(self, method, path, payload=None, none_on=()):  # type: ignore[override]
        self.calls.append((method, path, payload))
        if method == "POST" and path == "/api/v1/models/model/delete":
            model_id = (payload or {}).get("id")
            if model_id in self.models and "model" not in self.undeletable:
                self.models.discard(model_id)
            return {"ok": True}
        if method == "DELETE" and path.startswith("/api/v1/functions/id/"):
            plugin_id = path.split("/")[-2]
            if plugin_id in self.functions and "function" not in self.undeletable:
                self.functions.discard(plugin_id)
            return {"ok": True}
        if method == "GET" and path.startswith("/api/v1/models/model?id="):
            model_id = path.split("=", 1)[1]
            return {"id": model_id} if model_id in self.models else None
        if method == "GET" and path == "/api/v1/functions/":
            return [{"id": name} for name in sorted(self.functions)]
        if method == "GET" and path.startswith("/api/v1/functions/id/"):
            plugin_id = path.rstrip("/").split("/")[-1]
            return {"id": plugin_id, "is_active": True} if plugin_id in self.functions else None
        return {"ok": True}


def test_モデル削除は実機で確認した呼び方を使う() -> None:
    # 台帳 5-3: POST /api/v1/models/model/delete、本文は {"id": ...}
    client = FakeClient()
    client.delete_model("duty_summary")
    assert ("POST", "/api/v1/models/model/delete", {"id": "duty_summary"}) in client.calls


def test_Pipeとモデル設定を対で消す() -> None:
    client = FakeClient()
    result = client.remove_pipe_completely("duty_summary")
    assert result == {"model": True, "function": True}
    assert client.models == set() and client.functions == set()


def test_消えたかどうかを再取得で確かめる() -> None:
    # APIが成功を返しても実際に残っていれば False(「戻した」と言わせない)
    client = FakeClient()
    client.undeletable.add("function")
    result = client.remove_pipe_completely("duty_summary")
    assert result == {"model": True, "function": False}
    assert not all(result.values())


def test_無効化は有効なときだけtoggleする() -> None:
    client = FakeClient()
    client.deactivate_function("duty_summary")
    assert any(
        method == "POST" and path.endswith("/toggle") for method, path, _ in client.calls
    )

    quiet = FakeClient(functions=())  # 登録が無い=触らない
    quiet.deactivate_function("duty_summary")
    assert not any(method == "POST" for method, _, _ in quiet.calls)


# ---------------------------------------------------------------------------
# doctor の三面照合
# ---------------------------------------------------------------------------
def test_三面が揃っていればOK() -> None:
    findings = doctor.toolpack_consistency_findings(
        active={"duty_summary": {"version": "1.0.0"}},
        on_disk={("duty_summary", "1.0.0")},
        registered={"duty_summary"},
        orphans=[],
    )
    assert [f.level for f in findings] == ["OK"]


def test_実体が無ければ異常として報告する() -> None:
    findings = doctor.toolpack_consistency_findings(
        active={"duty_summary": {"version": "1.0.0"}},
        on_disk=set(), registered={"duty_summary"}, orphans=[],
    )
    assert findings[0].level == "異常"


def test_未登録なら注意として報告する() -> None:
    findings = doctor.toolpack_consistency_findings(
        active={"duty_summary": {"version": "1.0.0"}},
        on_disk={("duty_summary", "1.0.0")}, registered=set(), orphans=[],
    )
    assert findings[0].level == "注意"
    assert "登録されていません" in findings[0].title


def test_孤立版は情報として報告し削除しない() -> None:
    findings = doctor.toolpack_consistency_findings(
        active={}, on_disk=set(), registered=set(),
        orphans=[("duty_summary", "0.9.0")],
    )
    assert findings[0].level == "情報"
    assert "自動では消しません" in (findings[0].advice or "")


def test_追加ツール基盤が無ければ何も言わない(tmp_path) -> None:
    # 既存環境(additional-tools/ が無い)で doctor が余計な指摘を出さないこと
    assert doctor.check_toolpack_consistency(root=tmp_path) == []
    assert doctor.check_toolpack_runner(root=tmp_path) == []
    assert doctor.check_toolpack_names(root=tmp_path) == []


# ---------------------------------------------------------------------------
# 承認表示の書き換え検知
# ---------------------------------------------------------------------------
def test_承認状態から表示名を決める() -> None:
    assert doctor.expected_display_name("当直表集計", False) == "【未承認】当直表集計"
    assert doctor.expected_display_name("当直表集計", True) == "当直表集計"
    # すでに接頭辞が付いていても二重にしない
    assert doctor.expected_display_name("【未承認】当直表集計", False) == "【未承認】当直表集計"


def test_表示名が台帳どおりなら何も言わない() -> None:
    findings = doctor.toolpack_name_findings(
        {"duty": "【未承認】当直表集計"},
        {"duty": {"Function": "【未承認】当直表集計", "モデル設定": "【未承認】当直表集計"}},
    )
    assert findings == []


def test_承認の印を消されたら気づく() -> None:
    # 管理画面から【未承認】を外されると、承認を経ていないものが正式に見える
    findings = doctor.toolpack_name_findings(
        {"duty": "【未承認】当直表集計"},
        {"duty": {"Function": "【未承認】当直表集計", "モデル設定": "当直表集計"}},
    )
    assert len(findings) == 1
    assert findings[0].level == "注意"
    assert "モデル設定" in findings[0].detail
    assert "Function" not in findings[0].detail  # 変わっていない側は挙げない


def test_確認できなかった側は食い違い扱いしない() -> None:
    # 取得できずNoneだったものを「変えられた」と報告しない
    findings = doctor.toolpack_name_findings(
        {"duty": "【未承認】当直表集計"},
        {"duty": {"Function": "【未承認】当直表集計", "モデル設定": None}},
    )
    assert findings == []
