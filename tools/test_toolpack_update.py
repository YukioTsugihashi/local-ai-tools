"""更新・無効化・削除・前の版へ戻す(台帳 D-10)のテスト。

**一番重く見るのは「失敗しても、動いていたものが壊れないこと」。**
更新の取り消しで既存を消してしまうと、直そうとして本番を落とすことになる。

Open WebUI へのHTTPはすべてモックする。**本番の登録には触れない。**
"""

from __future__ import annotations

import json

import pytest

import toolpack_install as installer
import toolpack_manage as manage
import toolpack_store as ts
from test_toolpack_install import (  # noqa: F401  fixture をそのまま使う
    FakeDeploy, build_localtool, deploy_stub, store, tool_json,
)

MAIN_V2 = (
    "import argparse, json, os\n"
    "p = argparse.ArgumentParser(); p.add_argument('--request'); a = p.parse_args()\n"
    "req = json.loads(open(a.request, encoding='utf-8').read())\n"
    "out = os.path.join(os.path.dirname(a.request), req['outdir'], '結果.txt')\n"
    "open(out, 'w', encoding='utf-8').write('第2版')\n"
    "print(json.dumps({'status':'ok','message':'第2版で集計しました',"
    "'files':['結果.txt'],'skipped':[],'notes':[]}, ensure_ascii=False))\n"
)


def add_first(tmp_path, store, version="1.0.0"):
    """まず1件入れておく。"""
    package = build_localtool(
        tmp_path, meta=tool_json(version=version), name=f"v{version}.localtool"
    )
    report = installer.install(package, apply=True, store=store)
    assert report.ok, report.reason
    return report


def build_update(tmp_path, version="1.1.0", main_src=MAIN_V2):
    return build_localtool(
        tmp_path, meta=tool_json(version=version), main_src=main_src,
        name=f"v{version}.localtool",
    )


# ---------------------------------------------------------------------------
# 追加と更新は別の操作
# ---------------------------------------------------------------------------
def test_更新なのに追加として実行したら断る(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    report = installer.install(build_update(tmp_path), apply=True, store=store)
    assert not report.ok
    assert report.failed_stage == installer.STAGE_NAME
    assert "--update" in report.fix  # 何をすればよいか示す


def test_入っていないのに更新として実行したら断る(tmp_path, store, deploy_stub) -> None:
    report = installer.install(build_update(tmp_path), apply=True, update=True, store=store)
    assert not report.ok
    assert report.failed_stage == installer.STAGE_TARGET
    assert "追加" in report.fix


def test_同じ版で中身が違うものは断る(tmp_path, store, deploy_stub) -> None:
    """同じ版名で中身が変わると、どちらが動いているか分からなくなる。"""
    add_first(tmp_path, store, version="1.0.0")
    same_version = build_update(tmp_path, version="1.0.0", main_src=MAIN_V2)
    report = installer.install(same_version, apply=True, update=True, store=store)
    assert not report.ok
    assert report.failed_stage == installer.STAGE_TARGET
    assert "版を上げて" in report.fix


def test_まったく同じものを入れ直したらそう言う(tmp_path, store, deploy_stub) -> None:
    """USBから同じファイルをもう一度持ち込んだとき。**同じ中身だと分かるので断る。**"""
    package = build_localtool(tmp_path, meta=tool_json(version="1.0.0"), name="same.localtool")
    assert installer.install(package, apply=True, store=store).ok
    report = installer.install(package, apply=True, update=True, store=store)
    assert not report.ok
    assert "入れ直す必要はありません" in report.fix


# ---------------------------------------------------------------------------
# 更新が通ったとき
# ---------------------------------------------------------------------------
def test_更新すると有効版が切り替わり旧版は残る(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store, version="1.0.0")
    report = installer.install(build_update(tmp_path), apply=True, update=True, store=store)
    assert report.ok, report.reason
    assert report.previous_version == "1.0.0"

    entry = store.load_registry()["tools"]["duty_summary"]
    assert entry["active_version"] == "1.1.0"
    assert set(entry["versions"]) == {"1.0.0", "1.1.0"}
    # 旧版の実体を消していない(戻せるようにしておく)
    assert store.package_dir("duty_summary", "1.0.0").is_dir()
    assert store.package_dir("duty_summary", "1.1.0").is_dir()


def test_更新では既存を消さずに貼り替える(tmp_path, store, deploy_stub) -> None:
    """更新で remove_pipe_completely を呼ぶと、途中で落ちたとき本番が消える。"""
    add_first(tmp_path, store, version="1.0.0")
    deploy_stub.removed.clear()
    report = installer.install(build_update(tmp_path), apply=True, update=True, store=store)
    assert report.ok
    assert deploy_stub.removed == [], "更新なのに削除APIを呼んでいる"


def test_更新の成功表示に前の版が出る(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store, version="1.0.0")
    report = installer.install(build_update(tmp_path), apply=True, update=True, store=store)
    text = installer.format_report(report)
    assert "1.1.0 へ更新しました" in text
    assert "前の版 1.0.0 は残してあります" in text
    assert "【未承認】" in text  # 承認は版ごと


def test_更新後はハブが新しい版を配る(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store, version="1.0.0")
    installer.install(build_update(tmp_path), apply=True, update=True, store=store)
    from local_tool_bridge import installed_specs

    specs, errors = installed_specs.load()
    assert errors == [], errors
    assert "duty_summary" in specs


# ---------------------------------------------------------------------------
# 更新が失敗したとき(**ここが一番大事**)
# ---------------------------------------------------------------------------
def test_更新に失敗しても旧版が動いたまま残る(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store, version="1.0.0")
    deploy_stub.should_fail = True   # Open WebUI への登録で落ちる
    deploy_stub.removed.clear()

    report = installer.install(build_update(tmp_path), apply=True, update=True, store=store)
    assert not report.ok

    entry = store.load_registry()["tools"]["duty_summary"]
    assert entry["active_version"] == "1.0.0", "有効版が戻っていない"
    assert set(entry["versions"]) == {"1.0.0"}, "失敗した版が台帳に残っている"
    assert store.package_dir("duty_summary", "1.0.0").is_dir(), "旧版の実体が消えた"
    assert not store.version_dir("duty_summary", "1.1.0").exists()
    assert deploy_stub.removed == [], "更新の失敗で既存を削除している"


def test_更新に失敗したら旧版のPipeを貼り直す(tmp_path, store, deploy_stub, monkeypatch) -> None:
    add_first(tmp_path, store, version="1.0.0")
    deploy_stub.deployed.clear()

    # 1回目(新版)は失敗し、取り消しの貼り直しは成功する状況を作る
    calls = {"n": 0}
    import open_webui_deploy as deploy

    def flaky(client, path, plugin_id_override, apply):
        calls["n"] += 1
        if calls["n"] == 1:
            return 1
        deploy_stub.deployed.append(str(path))
        return 0

    monkeypatch.setattr(deploy, "command_deploy", flaky)
    report = installer.install(build_update(tmp_path), apply=True, update=True, store=store)
    assert not report.ok
    assert deploy_stub.deployed, "旧版のPipeを貼り直していない"
    assert "1.0.0" in deploy_stub.deployed[-1], "貼り直したのが旧版ではない"
    assert not report.warnings, report.warnings


def test_旧版のPipeが無ければ黙って成功と言わない(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store, version="1.0.0")
    (store.generated_dir("duty_summary", "1.0.0")
     / "open_webui_duty_summary_pipe.py").unlink()
    deploy_stub.should_fail = True

    report = installer.install(build_update(tmp_path), apply=True, update=True, store=store)
    assert not report.ok
    assert any("前の版のPipeが見つかりません" in w for w in report.warnings)
    assert "完全には戻せませんでした" in installer.format_report(report)


def test_更新のテストが落ちたら何も変えない(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store, version="1.0.0")
    broken = build_localtool(
        tmp_path, meta=tool_json(version="1.1.0"),
        test_src="def test_ng():\n    assert False\n", name="broken.localtool",
    )
    report = installer.install(broken, apply=True, update=True, store=store)
    assert not report.ok
    entry = store.load_registry()["tools"]["duty_summary"]
    assert entry["active_version"] == "1.0.0"
    assert set(entry["versions"]) == {"1.0.0"}


# ---------------------------------------------------------------------------
# 無効化・有効化
# ---------------------------------------------------------------------------
def test_無効にするとハブから消えるがファイルは残る(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    assert manage.command_disable(store, "duty_summary") == 0

    assert store.active_tools() == {}
    assert "duty_summary" in store.load_registry()["tools"]
    assert store.package_dir("duty_summary", "1.0.0").is_dir()
    assert deploy_stub.removed == ["duty_summary"]


def test_無効化に失敗したら台帳を元へ戻す(tmp_path, store, deploy_stub, monkeypatch) -> None:
    """片側だけ止まった状態を作らない。"""
    add_first(tmp_path, store)
    monkeypatch.setattr(
        manage, "_remove_from_openwebui",
        lambda tool_id: (_ for _ in ()).throw(OSError("繋がらない")),
    )
    with pytest.raises(manage.ManageError):
        manage.command_disable(store, "duty_summary")
    assert "duty_summary" in store.active_tools()


def test_有効に戻すと配り直される(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    manage.command_disable(store, "duty_summary")
    deploy_stub.deployed.clear()
    assert manage.command_enable(store, "duty_summary") == 0
    assert "duty_summary" in store.active_tools()
    assert deploy_stub.deployed == ["open_webui_duty_summary_pipe.py"]


# ---------------------------------------------------------------------------
# 前の版へ戻す
# ---------------------------------------------------------------------------
def test_前の版へ戻せる(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store, version="1.0.0")
    installer.install(build_update(tmp_path), apply=True, update=True, store=store)
    deploy_stub.deployed.clear()

    assert manage.command_rollback(store, "duty_summary", "1.0.0") == 0
    entry = store.load_registry()["tools"]["duty_summary"]
    assert entry["active_version"] == "1.0.0"
    assert set(entry["versions"]) == {"1.0.0", "1.1.0"}, "戻しても新しい版は残す"
    assert deploy_stub.deployed, "戻した版のPipeを貼っていない"


def test_無い版へは戻せない(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    with pytest.raises(manage.ManageError) as caught:
        manage.command_rollback(store, "duty_summary", "9.9.9")
    assert "ある版" in str(caught.value)


def test_戻すのに失敗したら切り替えも戻す(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store, version="1.0.0")
    installer.install(build_update(tmp_path), apply=True, update=True, store=store)
    deploy_stub.should_fail = True

    with pytest.raises(manage.ManageError):
        manage.command_rollback(store, "duty_summary", "1.0.0")
    entry = store.load_registry()["tools"]["duty_summary"]
    assert entry["active_version"] == "1.1.0", "切り替えだけ進んで止まっている"


# ---------------------------------------------------------------------------
# 削除と実体の削除
# ---------------------------------------------------------------------------
def test_削除は登録を外すだけでファイルを残す(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    assert manage.command_remove(store, "duty_summary") == 0
    assert "duty_summary" not in store.load_registry()["tools"]
    assert store.package_dir("duty_summary", "1.0.0").is_dir()
    assert deploy_stub.removed == ["duty_summary"]


def test_残置はdoctorが拾える形で残る(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    manage.command_remove(store, "duty_summary")
    orphans = store.orphan_versions()
    assert [(o.tool_id, o.version) for o in orphans] == [("duty_summary", "1.0.0")]


def test_実体の削除はyesが要る(tmp_path, store, deploy_stub, capsys) -> None:
    add_first(tmp_path, store)
    manage.command_remove(store, "duty_summary")
    assert manage.command_purge(store, "duty_summary", None, yes=False) == 1
    assert store.package_dir("duty_summary", "1.0.0").is_dir()
    assert "元に戻せません" in capsys.readouterr().out


def test_登録が残っているものは消させない(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    with pytest.raises(manage.ManageError) as caught:
        manage.command_purge(store, "duty_summary", None, yes=True)
    assert "登録が残っています" in str(caught.value)
    assert store.package_dir("duty_summary", "1.0.0").is_dir()


def test_yesを付ければ消える(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    manage.command_remove(store, "duty_summary")
    assert manage.command_purge(store, "duty_summary", None, yes=True) == 0
    assert not (store.installed / "duty_summary").exists()


# ---------------------------------------------------------------------------
# 一覧
# ---------------------------------------------------------------------------
def test_一覧に版と状態が出る(tmp_path, store, deploy_stub, capsys) -> None:
    add_first(tmp_path, store, version="1.0.0")
    installer.install(build_update(tmp_path), apply=True, update=True, store=store)
    manage.command_list(store)
    out = capsys.readouterr().out
    assert "duty_summary  版 1.1.0" in out
    assert "未承認" in out
    assert "残っている版: 1.0.0" in out


def test_無効なものは一覧でそう分かる(tmp_path, store, deploy_stub, capsys) -> None:
    add_first(tmp_path, store)
    manage.command_disable(store, "duty_summary")
    manage.command_list(store)
    assert "**無効**" in capsys.readouterr().out


def test_台帳の書き換えは原子的に行う(tmp_path, store, deploy_stub) -> None:
    """切り替えの途中で落ちても、半端な registry.json が残らない。"""
    add_first(tmp_path, store, version="1.0.0")
    installer.install(build_update(tmp_path), apply=True, update=True, store=store)
    raw = store.registry_path.read_text(encoding="utf-8")
    json.loads(raw)  # 壊れていない
    assert not list(store.registry_dir.glob("*.tmp")), "一時ファイルが残っている"
