"""受入CLI(検査→配置→登録→Pipe生成→登録)の端から端までのテスト。

Open WebUI へのHTTPはモックし、**本番登録も実モデル削除も行わない**。
パッケージはテストが tmp_path 内に生成する(バイナリのフィクスチャを持たない)。
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

import toolpack_install as installer
import toolpack_store as ts

MAIN_OK = '''\
import argparse, json, os
p = argparse.ArgumentParser(); p.add_argument("--request"); a = p.parse_args()
req = json.loads(open(a.request, encoding="utf-8").read())
out = os.path.join(os.path.dirname(a.request), req["outdir"], "result.txt")
open(out, "w", encoding="utf-8").write("ok")
print(json.dumps({"status":"ok","message":"できました","files":["result.txt"],
                  "skipped":[],"notes":[]}))
'''

MAKE_SAMPLE = '''\
import argparse, json, os
p = argparse.ArgumentParser(); p.add_argument("--request"); a = p.parse_args()
req = json.loads(open(a.request, encoding="utf-8").read())
out = os.path.join(os.path.dirname(a.request), req["outdir"], "sample.txt")
open(out, "w", encoding="utf-8").write("合成データ")
print(json.dumps({"status":"ok","message":"作りました","files":["sample.txt"],
                  "skipped":[],"notes":[]}))
'''

TEST_OK = "def test_ok():\n    assert True\n"
TEST_NG = "def test_ng():\n    assert False, 'わざと失敗'\n"


def tool_json(tool_id: str = "duty_summary", **over) -> dict:
    data = {
        "schema_version": 1, "id": tool_id, "version": "1.0.0",
        "display_name": "当直表集計", "summary": "当直回数を集計する",
        "inputs": {"accepts": [".txt"], "min": 1, "max": 1},
        "outputs": {"produces": [".txt"], "may_be_empty": True},
        "requires": {"core_api": ">=1,<2", "packages": [], "models": []},
        "permissions": {"ollama": False},
        "timeout_seconds": 300,
        "routing": {"label": "当直表集計", "patterns": ["当直"],
                     "examples": ["当直表を集計して"],
                     "suggestions": [{"title": "当直表集計", "subtitle": "回数",
                                       "content": "当直表を集計して"}]},
        "smoke": {"make_sample": "samples/make_sample.py",
                   "request": "samples/smoke_request.json",
                   "expect": {"status": "ok", "files_min": 1, "suffixes": [".txt"]}},
    }
    data.update(over)
    return data


def build_localtool(tmp_path: Path, *, meta: dict | None = None,
                    main_src: str = MAIN_OK, test_src: str = TEST_OK,
                    sample_src: str = MAKE_SAMPLE, name: str = "pkg.localtool") -> Path:
    files = {
        "tool.json": json.dumps(meta or tool_json(), ensure_ascii=False),
        "main.py": main_src,
        "README.md": "# 当直表集計\n",
        "tests/test_main.py": test_src,
        "samples/make_sample.py": sample_src,
        "samples/smoke_request.json": json.dumps(
            {"contract": 1, "instruction": "当直表を集計して", "inputs": [],
             "outdir": "out", "config": {}}
        ),
    }
    manifest = "\n".join(
        f"{hashlib.sha256(text.encode('utf-8')).hexdigest()}  {rel}"
        for rel, text in sorted(files.items())
    ) + "\n"
    target = tmp_path / name
    with zipfile.ZipFile(target, "w") as archive:
        for rel, text in files.items():
            archive.writestr(rel, text)
        archive.writestr("manifest.sha256", manifest)
    return target


class FakeDeploy:
    """open_webui_deploy の代役。HTTPを一切出さない。"""

    def __init__(self) -> None:
        self.deployed: list[str] = []
        self.removed: list[str] = []
        self.should_fail = False
        self.existing_ids: list[str] = []   # Open WebUI に既にある Function
        self.existing_models: list[str] = []  # Function は無いがモデル設定だけ残っている場合
        self.undeletable = False            # 消せない状況を模す
        # いま Open WebUI にあるもの / 稼働中のハブが配っているもの。
        # 配置・削除に合わせて動かし、**確認の段階が本当に効いているか**を試せるようにする
        self.present: set[str] = set()
        self.hub_ids: set[str] = set()
        self.openwebui_unreachable = False  # 問い合わせられない状況を模す
        self.hub_unreachable = False

    @staticmethod
    def _id_from_path(path) -> str:
        name = Path(path).name
        if name.startswith("open_webui_") and name.endswith("_pipe.py"):
            return name[len("open_webui_"):-len("_pipe.py")]
        return ""

    def install(self, monkeypatch) -> None:
        import open_webui_deploy as deploy

        outer = self

        class Client:
            def list_entries(self, plugin_type):
                return [{"id": name} for name in outer.existing_ids]

            def get_model(self, model_id):
                return {"id": model_id} if model_id in outer.existing_models else None

            def remove_pipe_completely(self, plugin_id):
                outer.removed.append(plugin_id)
                if outer.undeletable:
                    return {"model": True, "function": False}
                outer.present.discard(plugin_id)
                outer.hub_ids.discard(plugin_id)
                return {"model": True, "function": True}

        monkeypatch.setattr(deploy, "ApiClient", lambda **kwargs: Client())
        monkeypatch.setattr(deploy, "resolve_base_url", lambda: "http://localhost:3000")
        monkeypatch.setattr(deploy, "load_api_key", lambda: "dummy")

        def command_deploy(client, path, plugin_id_override, apply):
            if outer.should_fail:
                return 1
            outer.deployed.append(Path(path).name)
            tool_id = outer._id_from_path(path)
            if tool_id:
                outer.present.add(tool_id)
                outer.hub_ids.add(tool_id)
            return 0

        monkeypatch.setattr(deploy, "command_deploy", command_deploy)

        # 3か所の確認も同じ模型の上で行う(HTTPは出さない)
        import toolpack_state

        monkeypatch.setattr(
            toolpack_state, "openwebui_has",
            lambda tool_id: None if outer.openwebui_unreachable else tool_id in outer.present,
        )
        monkeypatch.setattr(
            toolpack_state, "openwebui_ids",
            lambda: None if outer.openwebui_unreachable else set(outer.present),
        )
        monkeypatch.setattr(
            toolpack_state, "hub_recognizes",
            lambda tool_id, timeout=10: (
                None if outer.hub_unreachable else tool_id in outer.hub_ids
            ),
        )
        monkeypatch.setattr(
            toolpack_state, "hub_tool_names",
            lambda timeout=10: None if outer.hub_unreachable else set(outer.hub_ids),
        )
        # 受入検査の疎通確認は、この一式の対象外(専用テストで別に見る)
        monkeypatch.setattr(installer, "_stage_reachable", lambda tool_id: None)


@pytest.fixture
def store(tmp_path, monkeypatch) -> ts.ToolpackStore:
    instance = ts.ToolpackStore(tmp_path / "additional-tools")
    instance.ensure_layout()
    from local_tool_bridge import installed_specs

    monkeypatch.setattr(installed_specs, "STORE_ROOT", instance.root)
    return instance


@pytest.fixture
def deploy_stub(monkeypatch) -> FakeDeploy:
    stub = FakeDeploy()
    stub.install(monkeypatch)
    return stub


# ---------------------------------------------------------------------------
# 正常系
# ---------------------------------------------------------------------------
def test_検査だけなら何も取り込まない(tmp_path, store, deploy_stub) -> None:
    report = installer.install(build_localtool(tmp_path), apply=False, store=store)
    assert report.ok, report.reason
    assert installer.STAGE_SMOKE in report.passed
    assert store.load_registry()["tools"] == {}  # 登録していない
    assert not list(store.installed.iterdir())    # 配置もしていない
    assert deploy_stub.deployed == []
    # 検査だけでも持ち込みの複製と展開物は残さない
    assert list(store.incoming.iterdir()) == []
    assert list(store.staging.iterdir()) == []


def test_applyで配置から登録まで通る(tmp_path, store, deploy_stub) -> None:
    report = installer.install(build_localtool(tmp_path), apply=True, store=store)
    assert report.ok, f"{report.failed_stage}: {report.reason}"
    assert report.passed[-1] == installer.STAGE_REACH

    entry = store.load_registry()["tools"]["duty_summary"]
    assert entry["status"] == "unapproved"      # 新規は常に未承認
    assert entry["active_version"] == "1.0.0"

    version_dir = store.version_dir("duty_summary", "1.0.0")
    assert (version_dir / ts.PACKAGE_DIR / "main.py").is_file()
    assert (version_dir / ts.SOURCE_DIR / ts.SOURCE_NAME).is_file()  # 原本を保存
    assert (version_dir / ts.INSTALL_JSON).is_file()
    assert (version_dir / ts.GENERATED_DIR / "open_webui_duty_summary_pipe.py").is_file()
    assert deploy_stub.deployed == ["open_webui_duty_summary_pipe.py"]

    # 成功後は incoming と staging を残さない。検査用の作業領域も持ち込まない
    assert list(store.incoming.iterdir()) == []
    assert list(store.staging.iterdir()) == []
    assert not (version_dir / "_work").exists()


def test_成功表示に呼び方と限界が出る(tmp_path, store, deploy_stub) -> None:
    report = installer.install(build_localtool(tmp_path), apply=True, store=store)
    text = installer.format_report(report)
    assert "【未承認】当直表集計" in text
    assert "当直表を集計して" in text          # 依頼の例
    assert "モデル一覧" in text
    assert "防ぎきれません" in text            # 限界を隠さない


def test_取り込んだツールがハブから見える(tmp_path, store, deploy_stub) -> None:
    installer.install(build_localtool(tmp_path), apply=True, store=store)
    from local_tool_bridge import hub

    assert "duty_summary" in hub.discover()[0]


# ---------------------------------------------------------------------------
# 失敗と巻き戻し
# ---------------------------------------------------------------------------
def _assert_clean(store: ts.ToolpackStore, report) -> None:
    """どの段階で落ちても、既存環境に何も残っていないこと。"""
    assert not report.ok
    assert store.load_registry()["tools"] == {}
    assert list(store.installed.glob("*/*")) == []
    assert list(store.staging.iterdir()) == []
    assert list(store.incoming.iterdir()) == []      # 失敗パッケージを残さない
    assert report.diagnosis_path and report.diagnosis_path.is_file()
    assert list(store.rejected.iterdir()) == [report.diagnosis_path]  # 診断だけ


def test_形式検査で落ちる(tmp_path, store, deploy_stub) -> None:
    broken = build_localtool(tmp_path, meta=tool_json(timeout_seconds=99999))
    report = installer.install(broken, apply=True, store=store)
    assert report.failed_stage == installer.STAGE_VERIFY
    _assert_clean(store, report)


def test_単体テストで落ちる(tmp_path, store, deploy_stub) -> None:
    report = installer.install(
        build_localtool(tmp_path, test_src=TEST_NG), apply=True, store=store
    )
    assert report.failed_stage == installer.STAGE_TESTS
    assert "失敗" in report.reason
    _assert_clean(store, report)


def test_サンプル実行で落ちる(tmp_path, store, deploy_stub) -> None:
    # 成果物を返さない本体。smoke.expect の files_min を満たせない
    report = installer.install(
        build_localtool(tmp_path, main_src=(
            'import argparse, json\n'
            'p = argparse.ArgumentParser(); p.add_argument("--request"); p.parse_args()\n'
            'print(json.dumps({"status":"ok","message":"何もしない","files":[],'
            '"skipped":[],"notes":[]}))\n'
        )),
        apply=True, store=store,
    )
    assert report.failed_stage == installer.STAGE_SMOKE
    _assert_clean(store, report)


def test_禁止操作をするツールはサンプル実行で止まる(tmp_path, store, deploy_stub) -> None:
    escape = (tmp_path / "escape.txt").resolve()
    report = installer.install(
        build_localtool(tmp_path, main_src=(
            'import argparse, json\n'
            'p = argparse.ArgumentParser(); p.add_argument("--request"); p.parse_args()\n'
            f'open(r"{escape}", "w").write("x")\n'
            'print(json.dumps({"status":"ok","message":"抜けた","files":[],'
            '"skipped":[],"notes":[]}))\n'
        )),
        apply=True, store=store,
    )
    assert report.failed_stage == installer.STAGE_SMOKE
    assert not escape.exists()  # 隔離が効いている
    _assert_clean(store, report)


def test_名前が衝突したら既存を残して拒否する(tmp_path, store, deploy_stub) -> None:
    report = installer.install(
        build_localtool(tmp_path, meta=tool_json("excel_read")), apply=True, store=store
    )
    assert report.failed_stage == installer.STAGE_NAME
    _assert_clean(store, report)
    from local_tool_bridge import hub

    assert "excel_read" in hub.discover()[0]  # 既存はそのまま


def test_登録に失敗したら配置と台帳を戻す(tmp_path, store, deploy_stub) -> None:
    deploy_stub.should_fail = True
    report = installer.install(build_localtool(tmp_path), apply=True, store=store)
    assert report.failed_stage == installer.STAGE_DEPLOY
    assert installer.STAGE_REGISTER in report.passed  # 一度は登録まで進んでいる
    _assert_clean(store, report)                       # それでも全部戻っている


def test_OpenWebUIに同じIDがあれば拒否する(tmp_path, store, deploy_stub) -> None:
    # 管理アプリの外で登録された同名Pipeを「更新」してしまわないこと
    deploy_stub.existing_ids = ["duty_summary"]
    report = installer.install(build_localtool(tmp_path), apply=True, store=store)
    assert report.failed_stage == installer.STAGE_NAME
    assert "同じIDの登録があります" in report.reason
    assert deploy_stub.deployed == []  # 触っていない
    _assert_clean(store, report)


def test_モデル設定だけ残っていても拒否する(tmp_path, store, deploy_stub) -> None:
    # Function は消えたがモデル設定が残っている状態。ここを見ないと既存を更新してしまう
    deploy_stub.existing_models = ["duty_summary"]
    report = installer.install(build_localtool(tmp_path), apply=True, store=store)
    assert report.failed_stage == installer.STAGE_NAME
    assert deploy_stub.deployed == []
    _assert_clean(store, report)


def test_登録一覧を確認できなければ取り込まない(tmp_path, store, monkeypatch, deploy_stub) -> None:
    monkeypatch.setattr(installer, "_openwebui_has_id", lambda tool_id: None)
    report = installer.install(build_localtool(tmp_path), apply=True, store=store)
    assert report.failed_stage == installer.STAGE_NAME
    assert "確認できませんでした" in report.reason
    _assert_clean(store, report)


def test_登録の途中で落ちても取り消しを試みる(tmp_path, store, deploy_stub) -> None:
    # command_deploy が失敗しても「試みた」以上は削除を実行すること
    deploy_stub.should_fail = True
    report = installer.install(build_localtool(tmp_path), apply=True, store=store)
    assert report.failed_stage == installer.STAGE_DEPLOY
    assert deploy_stub.removed == ["duty_summary"]  # 取り消しを実行している
    _assert_clean(store, report)


def test_取り消せなかったら黙らずに知らせる(tmp_path, store, deploy_stub) -> None:
    deploy_stub.should_fail = True
    deploy_stub.undeletable = True
    report = installer.install(build_localtool(tmp_path), apply=True, store=store)
    assert not report.ok
    assert any("消せませんでした" in warning for warning in report.warnings)
    assert "手動で削除" in "".join(report.warnings)

    # 表示は「戻しました」と言わず、戻せなかったものを並べる
    text = installer.format_report(report)
    assert "完全には戻せませんでした" in text
    assert "既存の環境は変更前の状態へ戻しました" not in text
    assert "手動で削除" in text

    # 診断ファイルにも残る(表示だけで消えない)
    diagnosis = report.diagnosis_path.read_text(encoding="utf-8")
    assert "巻き戻し: 未完了" in diagnosis
    assert "戻せなかったもの" in diagnosis


def test_全部戻せたときだけ戻したと表示する(tmp_path, store, deploy_stub) -> None:
    deploy_stub.should_fail = True
    report = installer.install(build_localtool(tmp_path), apply=True, store=store)
    text = installer.format_report(report)
    assert "既存の環境は変更前の状態へ戻しました" in text
    assert "完全には戻せませんでした" not in text
    assert "巻き戻し: 完了" in report.diagnosis_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 疎通確認(稼働中のハブへ実際に問い合わせる)
# ---------------------------------------------------------------------------
def _fake_urlopen(payload: dict):
    import contextlib
    import io
    import json as _json

    @contextlib.contextmanager
    def opener(url, timeout=None):
        yield io.BytesIO(_json.dumps(payload).encode("utf-8"))

    return opener


def test_稼働中のハブが認識していれば通る(monkeypatch) -> None:
    import urllib.request

    monkeypatch.setattr(
        urllib.request, "urlopen",
        _fake_urlopen({"tools": [{"name": "duty_summary"}], "errors": []}),
    )
    assert installer._stage_reachable("duty_summary") is None  # 例外が出なければ通過


def test_稼働中のハブが認識しなければ失敗にする(monkeypatch) -> None:
    import urllib.request

    monkeypatch.setattr(
        urllib.request, "urlopen",
        _fake_urlopen({"tools": [], "errors": ["読み込みに失敗"]}),
    )
    with pytest.raises(installer.InstallError) as caught:
        installer._stage_reachable("duty_summary")
    assert caught.value.stage == installer.STAGE_REACH
    assert "読み込みに失敗" in caught.value.reason


def test_ハブへ問い合わせできなければ失敗にする(monkeypatch) -> None:
    # 「使えるか分からない」ものを「接続しました」と言わない
    import urllib.error
    import urllib.request

    def refuse(url, timeout=None):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(installer.InstallError) as caught:
        installer._stage_reachable("duty_summary")
    assert caught.value.stage == installer.STAGE_REACH


def test_疎通できなければ取り込みを取り消す(tmp_path, store, deploy_stub, monkeypatch) -> None:
    def unreachable(tool_id):
        raise installer.InstallError(installer.STAGE_REACH, "届きません", "サービスを確認")

    monkeypatch.setattr(installer, "_stage_reachable", unreachable)
    report = installer.install(build_localtool(tmp_path), apply=True, store=store)
    assert report.failed_stage == installer.STAGE_REACH
    assert deploy_stub.removed == ["duty_summary"]  # 登録も取り消す
    _assert_clean(store, report)


def test_診断に本文や元ファイル名を残さない(tmp_path, store, deploy_stub) -> None:
    report = installer.install(
        build_localtool(tmp_path, test_src=TEST_NG, name="実在しそうな名前.localtool"),
        apply=True, store=store,
    )
    text = report.diagnosis_path.read_text(encoding="utf-8")
    assert "実在しそうな名前" not in text
    assert "duty_summary" in text  # ツールIDと段階は残す
