"""管理画面(toolpack_gui)のテスト。

**画面の見た目ではなく、判断のところを確かめる。**
どれを操作させるか(D-15)と、3系統の突き合わせ(D-11)がここの中身であり、
ウィジェットの組み立ては薄く保ってある。

Open WebUI へのHTTPは出さない。**本番の登録には触れない。**
"""

from __future__ import annotations

import time

import pytest

import toolpack_gui as gui


def registry(**tools) -> dict:
    return {"tools": tools}


def added(version="1.0.0", status="unapproved", enabled=True, versions=None) -> dict:
    return {
        "active_version": version,
        "status": status,
        "enabled": enabled,
        "versions": {v: {} for v in (versions or [version])},
    }


# ---------------------------------------------------------------------------
# 一覧の1行 = Open WebUI に登録されているモデル1つ
# ---------------------------------------------------------------------------
def test_登録されているモデルが1行になる() -> None:
    rows = gui.collect_models(
        registry(duty_summary=added()),
        hub_names={"duty_summary"},
        openwebui_models={"duty_summary": "【未承認】当直表集計"},
        core_links={},
    )
    assert [row.model_id for row in rows] == ["duty_summary"]
    assert rows[0].display_name == "【未承認】当直表集計"
    assert rows[0].kind == gui.KIND_ADDED
    assert rows[0].problems == []


def test_モデルが使うツールはその下へ入れる() -> None:
    """excel_read / excel_comment はモデルではなく、Excel分析が使うツール。"""
    rows = gui.collect_models(
        {}, hub_names={"excel_read", "excel_comment"},
        openwebui_models={"excel_analysis": "Excel分析・科別分析シート"},
        core_links={"excel_analysis": {"excel_read", "excel_comment"}},
    )
    assert [row.model_id for row in rows] == ["excel_analysis"]
    assert [tool.name for tool in rows[0].tools] == ["excel_comment", "excel_read"]
    assert all(tool.in_hub for tool in rows[0].tools)


def test_ツールはモデルとして並べない() -> None:
    rows = gui.collect_models(
        {}, hub_names={"excel_read"},
        openwebui_models={"excel_analysis": "Excel分析"},
        core_links={"excel_analysis": {"excel_read"}},
    )
    assert "excel_read" not in [row.model_id for row in rows]


def test_登録されていないリポジトリのファイルはモデルにしない() -> None:
    """並べると「使えるモデル」に見えてしまう。"""
    rows = gui.collect_models(
        {}, hub_names=set(),
        openwebui_models={"text_processing": "文章処理"},
        core_links={"text_processing": set(), "line_count": set()},
    )
    assert [row.model_id for row in rows] == ["text_processing"]


def test_登録されていないファイルは気になることとして出す() -> None:
    rows = gui.collect_models(
        {}, set(), {"text_processing": "文章処理"},
        {"text_processing": set(), "line_count": set()},
    )
    findings = gui.stray_findings(
        set(), {"text_processing": "文章処理"},
        {"text_processing": set(), "line_count": set()}, rows,
    )
    assert any("line_count" in item for item in findings)


def test_どのモデルからも使われないツールを知らせる() -> None:
    rows = gui.collect_models({}, {"迷子"}, {}, {})
    findings = gui.stray_findings({"迷子"}, {}, {}, rows)
    assert any("使われていないツール" in item for item in findings)


def test_管理外のモデルは勝手に消さずそう表示する() -> None:
    rows = gui.collect_models(
        {}, set(), {"transcription_refiner": "文字起こし整形"}, {}
    )
    assert rows[0].kind == gui.KIND_UNMANAGED
    assert rows[0].approval_label == "管理外"
    assert "この画面の外で登録された" in rows[0].problems[0]


def test_無効にした追加ツールも一覧に残す() -> None:
    """登録が無くても、戻せるように出す必要がある。"""
    rows = gui.collect_models(
        registry(duty_summary=added(enabled=False)), set(), {}, {}
    )
    assert [row.model_id for row in rows] == ["duty_summary"]
    assert rows[0].state_label == "無効"
    assert rows[0].problems == []  # 意図して止めているので異常ではない


def test_台帳にあるのにハブが知らなければ気づける() -> None:
    rows = gui.collect_models(
        registry(duty_summary=added()), set(), {"duty_summary": "当直表集計"}, {}
    )
    assert any("ハブが認識していません" in p for p in rows[0].problems)
    assert rows[0].state_label == "要確認"


def test_OpenWebUIに登録が無ければ気づける() -> None:
    rows = gui.collect_models(registry(duty_summary=added()), {"duty_summary"}, {}, {})
    assert any("Open WebUI に登録がありません" in p for p in rows[0].problems)


def test_無効なのに残っていたら気づける() -> None:
    rows = gui.collect_models(
        registry(duty_summary=added(enabled=False)),
        {"duty_summary"}, {"duty_summary": "当直表集計"}, {},
    )
    assert any("無効なのに Open WebUI" in p for p in rows[0].problems)
    assert any("無効なのに稼働中のハブ" in p for p in rows[0].problems)


def test_確認できなかったことを無いと混ぜない() -> None:
    """繋がらないときに「登録されていません」と言うと、
    実際には登録されているものを消させてしまう。"""
    rows = gui.collect_models(registry(duty_summary=added()), None, None, {})
    assert rows[0].in_openwebui is None
    assert rows[0].tools[0].in_hub is None
    assert rows[0].tools[0].state_label == "未確認"
    assert rows[0].problems == []


def test_残っている版が分かる() -> None:
    rows = gui.collect_models(
        registry(duty_summary=added(version="1.1.0", versions=["1.0.0", "1.1.0"])),
        {"duty_summary"}, {"duty_summary": "当直表集計"}, {},
    )
    assert rows[0].versions == ["1.0.0", "1.1.0"]


# ---------------------------------------------------------------------------
# 承認は列。まとめ方には使わない
# ---------------------------------------------------------------------------
def test_承認の状態は列に出す() -> None:
    assert gui.ModelRow("x", "x", gui.KIND_ADDED, approved=False).approval_label == "未承認"
    assert gui.ModelRow("x", "x", gui.KIND_ADDED, approved=True).approval_label == "承認済み"


def test_もとからある本番モデルは承認済みとして出す() -> None:
    """既に運用で使われており、この仕組みの承認手続きの対象ではない。"""
    assert gui.ModelRow("x", "x", gui.KIND_CORE).approval_label == "承認済み"


def test_手を入れる必要があるものを上へ並べる() -> None:
    rows = gui.collect_models(
        registry(zz_added=added(), aa_approved=added(status="approved")),
        hub_names=set(),
        openwebui_models={"zz_added": "zz", "aa_approved": "aa",
                          "mm_core": "mm", "nn_unmanaged": "nn"},
        core_links={"mm_core": set()},
    )
    assert [row.model_id for row in rows] == [
        "zz_added", "aa_approved", "mm_core", "nn_unmanaged",
    ]


def test_承認の判定はコアと同じものを使う() -> None:
    """画面が独自に status を見ると、版ごとの承認へ移ったときにずれる。"""
    entry = added(version="1.1.0", status="approved", versions=["1.0.0", "1.1.0"])
    entry["versions"]["1.1.0"] = {"approved": False}
    rows = gui.collect_models(registry(duty_summary=entry), set(), {}, {})
    assert rows[0].approved is False
    assert gui.operable(rows[0]) is True


# ---------------------------------------------------------------------------
# 誰に何を触らせるか(D-15)
# ---------------------------------------------------------------------------
def test_未承認の追加ツールだけ操作できる() -> None:
    assert gui.operable(gui.ModelRow("x", "x", gui.KIND_ADDED, approved=False)) is True


def test_承認済みは画面から変更させない() -> None:
    assert gui.operable(gui.ModelRow("x", "x", gui.KIND_ADDED, approved=True)) is False


def test_もとからあるモデルは変更させない() -> None:
    assert gui.operable(gui.ModelRow("text_processing", "文章処理", gui.KIND_CORE)) is False


def test_管理外は変更させない() -> None:
    assert gui.operable(gui.ModelRow("x", "x", gui.KIND_UNMANAGED)) is False


def test_出どころは残しておく() -> None:
    """表示の主役ではないが、権限の判定はこちらで行う。"""
    core = gui.ModelRow("text_processing", "文章処理", gui.KIND_CORE)
    assert core.approval_label == "承認済み"
    assert core.kind == gui.KIND_CORE
    assert gui.operable(core) is False


# ---------------------------------------------------------------------------
# 長い処理を画面と切り離す
# ---------------------------------------------------------------------------
def wait_for(worker: gui.Worker, timeout: float = 10.0):
    limit = time.time() + timeout
    while time.time() < limit:
        taken = worker.take()
        if taken is not None:
            return taken
        time.sleep(0.01)
    raise AssertionError("処理が終わりませんでした")


def test_別スレッドで動かし出力をそのまま渡す() -> None:
    worker = gui.Worker()

    def job() -> bool:
        print("コマンド側が出した文章")
        return True

    worker.run("試し", job)
    label, ok, output, _ = wait_for(worker)
    assert (label, ok) == ("試し", True)
    assert "コマンド側が出した文章" in output


def test_失敗しても画面を落とさず理由を渡す() -> None:
    worker = gui.Worker()
    worker.run("試し", lambda: (_ for _ in ()).throw(RuntimeError("わざと失敗")))
    label, ok, output, _ = wait_for(worker)
    assert ok is False
    assert "わざと失敗" in output


def test_戻り値が偽なら失敗として扱う() -> None:
    worker = gui.Worker()
    worker.run("試し", lambda: False)
    _, ok, _, _ = wait_for(worker)
    assert ok is False


def test_実行中はそう分かる() -> None:
    worker = gui.Worker()
    release = []

    def slow() -> bool:
        while not release:
            time.sleep(0.01)
        return True

    worker.run("遅い処理", slow)
    time.sleep(0.05)
    assert worker.busy is True
    release.append(True)
    wait_for(worker)
    assert worker.busy is False


def test_一覧の読み直しが操作の出力を横取りしない() -> None:
    """redirect_stdout はプロセス全体に効く。拾う処理を2つ同時に走らせると、
    片方の出力がもう片方の箱へ入る。一覧側は拾わない設定にしてある。"""
    operation = gui.Worker()
    refresh = gui.Worker()
    started, release = [], []

    def slow_operation() -> bool:
        started.append(True)
        while not release:
            time.sleep(0.01)
        print("操作の出力")
        return True

    operation.run("操作", slow_operation)
    while not started:
        time.sleep(0.01)
    refresh.run("再読込", lambda: ("台帳", set(), set(), set()), capture=False)
    wait_for(refresh)
    release.append(True)

    _, ok, output, _ = wait_for(operation)
    assert ok and "操作の出力" in output


# ---------------------------------------------------------------------------
# 操作はコマンド側をそのまま呼ぶ(画面に判断を持たせない)
# ---------------------------------------------------------------------------
def test_追加はインストーラを呼ぶ(monkeypatch, tmp_path) -> None:
    import toolpack_install as installer

    seen = {}

    def fake_install(path, *, apply, update, store):
        seen.update(path=path, apply=apply, update=update)
        return installer.InstallReport(tool_id="x", ok=True, applied=True)

    monkeypatch.setattr(installer, "install", fake_install)
    assert gui.action_install(tmp_path / "p.localtool", False, None) is True
    assert seen["apply"] is True and seen["update"] is False


def test_入れ替えは更新として呼ぶ(monkeypatch, tmp_path) -> None:
    import toolpack_install as installer

    seen = {}
    monkeypatch.setattr(
        installer, "install",
        lambda path, *, apply, update, store: (
            seen.update(update=update), installer.InstallReport(ok=True))[1],
    )
    gui.action_install(tmp_path / "p.localtool", True, None)
    assert seen["update"] is True


def test_管理操作はtoolpack_manageを呼ぶ(monkeypatch) -> None:
    import toolpack_manage as manage

    calls = []
    monkeypatch.setattr(manage, "command_disable",
                        lambda store, tool_id: calls.append(("disable", tool_id)) or 0)
    assert gui.action_manage("disable", None, "duty_summary") is True
    assert calls == [("disable", "duty_summary")]


def test_管理操作の失敗は理由を出して偽を返す(monkeypatch, capsys) -> None:
    import toolpack_manage as manage

    monkeypatch.setattr(
        manage, "command_rollback",
        lambda *args: (_ for _ in ()).throw(manage.ManageError("その版はありません")),
    )
    assert gui.action_manage("rollback", None, "duty_summary", "9.9.9") is False
    assert "その版はありません" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 材料集め
# ---------------------------------------------------------------------------
def test_ハブは稼働中のサービスへ問い合わせる(tmp_path, monkeypatch) -> None:
    """同じプロセスで hub.discover() を呼ぶと、**サービスが止まっていても
    「認識している」と表示されてしまう**(Codex指摘・2026-09-01)。"""
    import toolpack_state
    import toolpack_store as ts

    store = ts.ToolpackStore(tmp_path / "additional-tools")
    store.ensure_layout()
    asked = []
    monkeypatch.setattr(
        toolpack_state, "hub_tool_names",
        lambda timeout=10: asked.append(True) or {"duty_summary"},
    )
    monkeypatch.setattr(toolpack_state, "openwebui_models", lambda: {})
    hub_names = gui.gather_sources(store).hub_names
    assert asked, "稼働中のハブへ問い合わせていない"
    assert hub_names == {"duty_summary"}


def test_ハブへ繋がらなければ未確認として扱う(tmp_path, monkeypatch) -> None:
    import toolpack_state
    import toolpack_store as ts

    store = ts.ToolpackStore(tmp_path / "additional-tools")
    store.ensure_layout()
    monkeypatch.setattr(toolpack_state, "hub_tool_names", lambda timeout=10: None)
    monkeypatch.setattr(toolpack_state, "openwebui_models", lambda: {})
    hub_names = gui.gather_sources(store).hub_names
    assert hub_names is None
def test_OpenWebUIへ繋がらなくても一覧は作れる(tmp_path, monkeypatch) -> None:
    import toolpack_store as ts

    store = ts.ToolpackStore(tmp_path / "additional-tools")
    store.ensure_layout()

    import open_webui_deploy as deploy

    monkeypatch.setattr(
        deploy, "ApiClient", lambda **kwargs: (_ for _ in ()).throw(OSError("繋がらない"))
    )
    sources = gui.gather_sources(store)
    registry_data, openwebui_ids = sources.registry, sources.models
    assert openwebui_ids is None  # 未確認として返る
    assert registry_data == {"tools": {}} or "tools" in registry_data


def test_もとからあるPipeのidを拾える() -> None:
    ids, _ = gui.core_plugin_links()
    assert ids, "tools/ の本番Pipeを1つも拾えていない"
    assert all(isinstance(name, str) and name for name in ids)


@pytest.mark.skipif(
    __import__("os").environ.get("TOOLPACK_GUI_SMOKE") != "1",
    reason="画面を実際に開く確認は明示したときだけ",
)
def test_画面が組み立てられる(tmp_path) -> None:
    import tkinter as tk

    import toolpack_store as ts

    store = ts.ToolpackStore(tmp_path / "additional-tools")
    store.ensure_layout()
    root = tk.Tk()
    app = None
    try:
        app = gui.App(root, store=store)
        root.update()
    finally:
        if app is not None:
            app.stop()
        root.destroy()


# ---------------------------------------------------------------------------
# 正式モデルは明示して持つ(Codexレビュー2026-09-01)
# ---------------------------------------------------------------------------
def test_正式モデルは明示した3件だけ() -> None:
    """`tools/open*webui*.py` を全部拾うと、登録していないものや
    **廃止したもの**まで承認済みとして扱ってしまう。"""
    links, paths = gui.core_plugin_links()
    assert set(links) == set(gui.CORE_MODEL_IDS)
    assert set(paths) == set(gui.CORE_MODEL_IDS)
    assert "transcription_refiner" not in links
    assert "dify_bridge" not in links


def test_廃止済みが登録されていたら承認済みにしない() -> None:
    """再登録しないと決めたものが出てきたら、管理外として扱う。"""
    rows = gui.collect_models(
        {}, set(), {"transcription_refiner": "文字起こし整形"}, {}
    )
    assert rows[0].kind == gui.KIND_UNMANAGED
    assert rows[0].approval_label == "管理外"


def test_廃止済みが登録されていたら理由つきで知らせる() -> None:
    models = {"transcription_refiner": "文字起こし整形"}
    rows = gui.collect_models({}, set(), models, {})
    findings = gui.stray_findings(set(), models, {}, rows)
    assert any("廃止したはずの transcription_refiner" in item for item in findings)
    assert any("2026-08-14" in item for item in findings)


def test_未登録の余計なファイルで毎回注意が出ない() -> None:
    """dify_bridge 等は正式モデルではないので、そもそも候補に入らない。"""
    links, _ = gui.core_plugin_links()
    findings = gui.stray_findings(
        set(), {name: name for name in links}, links, []
    )
    assert not any("登録されていません" in item for item in findings)


# ---------------------------------------------------------------------------
# 表示名の控え(Open WebUI から取れないとき)
# ---------------------------------------------------------------------------
def test_登録が無いときはパッケージの表示名を出す() -> None:
    """無効にしている間や繋がらないときに、内部IDを見せない。"""
    rows = gui.collect_models(
        registry(duty_summary=added(enabled=False)), set(), {}, {},
        fallback_names={"duty_summary": "【未承認】当直表集計"},
    )
    assert rows[0].display_name == "【未承認】当直表集計"


def test_OpenWebUIの表示名を第一候補にする() -> None:
    rows = gui.collect_models(
        registry(duty_summary=added()), set(), {"duty_summary": "画面での名前"}, {},
        fallback_names={"duty_summary": "控えの名前"},
    )
    assert rows[0].display_name == "画面での名前"


def test_控えも無ければ内部IDのまま() -> None:
    rows = gui.collect_models(registry(duty_summary=added()), set(), {}, {})
    assert rows[0].display_name == "duty_summary"


def test_控えの表示名は承認状態から作る(tmp_path) -> None:
    import json

    import toolpack_store as ts

    store = ts.ToolpackStore(tmp_path / "additional-tools")
    store.ensure_layout()
    package = store.package_dir("duty_summary", "1.0.0")
    package.mkdir(parents=True)
    (package / "tool.json").write_text(
        json.dumps({"display_name": "当直表集計"}, ensure_ascii=False), encoding="utf-8"
    )
    names = gui.package_display_names(store, registry(duty_summary=added()))
    assert names["duty_summary"] == "【未承認】当直表集計"


# ---------------------------------------------------------------------------
# どの行を選んだかでできることを変える(Codexレビュー2026-09-01)
#
# **画面を作らずに確かめる。** この環境では Tk の起動が時々失敗する
# (tk.tcl を読めないことがある)ため、判断だけを切り出して試験する。
# ---------------------------------------------------------------------------
def model(enabled=True, versions=("1.0.0", "1.1.0"), **over) -> gui.ModelRow:
    return gui.ModelRow(
        "duty_summary", "【未承認】当直表集計", gui.KIND_ADDED,
        version="1.1.0", enabled=enabled, versions=list(versions), **over,
    )


def test_モデルの行なら無効化も削除もできる() -> None:
    assert gui.button_states(model(), "model") == {
        "disable": True, "enable": False, "rollback": True, "remove": True,
        "approve": True, "unapprove": False,
    }


def test_版の行では戻すことしかできない() -> None:
    """戻すつもりで版を選んだまま「登録を外す」を押すと、モデルごと消える。"""
    assert gui.button_states(model(), "version") == {
        "disable": False, "enable": False, "rollback": True, "remove": False,
        "approve": False, "unapprove": False,
    }
    assert "版 1.0.0 を選んでいます" in gui.selection_hint(model(), "version", "1.0.0")


def test_ツールの行では何もできない() -> None:
    assert set(gui.button_states(model(), "tool").values()) == {False}
    assert "表示のみ" in gui.selection_hint(model(), "tool")


def test_無効なモデルは有効化だけできる() -> None:
    states = gui.button_states(model(enabled=False), "model")
    assert states["enable"] is True and states["disable"] is False


def test_版が1つなら戻せない() -> None:
    assert gui.button_states(model(versions=["1.0.0"]), "model")["rollback"] is False


def test_操作できない対象はどの行でも押せない() -> None:
    core = gui.ModelRow("text_processing", "文章処理", gui.KIND_CORE)
    assert set(gui.button_states(core, "model").values()) == {False}
    assert "もとからある" in gui.selection_hint(core, "model")

    unmanaged = gui.ModelRow("x", "x", gui.KIND_UNMANAGED)
    assert "管理外" in gui.selection_hint(unmanaged, "model")

    approved = gui.ModelRow("x", "x", gui.KIND_ADDED, approved=True)
    assert "承認済み" in gui.selection_hint(approved, "model")


def test_何も選んでいなければ何も押せない() -> None:
    assert set(gui.button_states(None, "").values()) == {False}
    assert gui.selection_hint(None, "") == ""


# ---------------------------------------------------------------------------
# どのファイルが動いているかを画面で引ける
# ---------------------------------------------------------------------------
def test_もとからあるモデルはリポジトリのファイルを示す() -> None:
    _, paths = gui.core_plugin_links()
    rows = gui.collect_models(
        {}, set(), {"text_processing": "文章処理"},
        {"text_processing": set()}, source_paths=paths,
    )
    assert rows[0].source_path == "tools/open_webui_text_processing_pipe.py"


def test_追加ツールは生成Pipeの場所を示す(tmp_path) -> None:
    import toolpack_store as ts

    store = ts.ToolpackStore(tmp_path / "additional-tools")
    store.ensure_layout()
    paths = gui.generated_pipe_paths(store, registry(duty_summary=added()), tmp_path)
    assert paths["duty_summary"].endswith(
        "installed/duty_summary/1.0.0/generated/open_webui_duty_summary_pipe.py"
    )


def test_管理外はファイルが無いことが分かる() -> None:
    rows = gui.collect_models({}, set(), {"nazo": "謎のモデル"}, {})
    assert rows[0].source_path == ""  # 画面では「リポジトリに実体がありません」と出る


def test_全モデルに場所が付く() -> None:
    """開いても何も無いモデルがあると、開けるのか無いのか分からない。"""
    links, paths = gui.core_plugin_links()
    rows = gui.collect_models(
        {}, set(), {name: name for name in links}, links, source_paths=paths,
    )
    assert rows and all(row.source_path for row in rows)


# ---------------------------------------------------------------------------
# 呼び方が実物と合っているか
#
# ボタンから呼ぶ関数を「モックだけ」で試験すると、**呼び方の取り違えを
# 見逃す**。実際、自己診断は doctor.main([]) と呼んでおり、
# main() は引数を取らないため押すたびに落ちていた(2026-09-01)。
# ---------------------------------------------------------------------------
def test_自己診断が実際に動く(monkeypatch, capsys) -> None:
    """doctor の本物の関数を通す(検査の中身だけ差し替える)。"""
    import doctor

    monkeypatch.setattr(
        doctor, "collect_findings",
        lambda: [doctor.Finding("OK", "架空の検査")],
    )
    assert gui.action_doctor() is True
    assert "架空の検査" in capsys.readouterr().out


def test_異常があれば自己診断は失敗として返す(monkeypatch) -> None:
    import doctor

    monkeypatch.setattr(
        doctor, "collect_findings",
        lambda: [doctor.Finding("異常", "架空の異常")],
    )
    assert gui.action_doctor() is False


def test_自己診断はSystemExitを投げる入口を使わない() -> None:
    """doctor.main() は sys.argv を読み、最後に SystemExit を投げる。
    画面から呼ぶと、画面の起動引数を診断の指定として読んでしまう。"""
    # 説明文ではなく、実際に触っている名前で見る
    assert "main" not in gui.action_doctor.__code__.co_names
    assert "collect_findings" in gui.action_doctor.__code__.co_names


def test_ボタンから呼ぶ関数の引数が実物と合っている() -> None:
    """モックで固めた試験だけだと、呼び方の取り違えが残る。"""
    import inspect

    import doctor
    import toolpack_install as installer
    import toolpack_manage as manage

    # 画面が実際に渡している形で束縛できるか
    inspect.signature(installer.install).bind(
        "p.localtool", apply=True, update=False, store=None
    )
    inspect.signature(manage.command_disable).bind(None, "duty_summary")
    inspect.signature(manage.command_enable).bind(None, "duty_summary")
    inspect.signature(manage.command_remove).bind(None, "duty_summary")
    inspect.signature(manage.command_rollback).bind(None, "duty_summary", "1.0.0")
    inspect.signature(doctor.collect_findings).bind()
    inspect.signature(doctor.format_report).bind([])
    inspect.signature(doctor.exit_code).bind([])


def test_捕まえている最中にモジュールを読み込める() -> None:
    """`tools/` の多くは import 時に `sys.stdout.reconfigure` を呼ぶ。
    素の StringIO へ差し替えていると、そこで落ちる(自己診断で実際に起きた)。"""
    worker = gui.Worker()

    def job() -> bool:
        import sys

        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print("読み込めた")
        return True

    worker.run("試し", job)
    _, ok, output, _ = wait_for(worker)
    assert ok, output
    assert "読み込めた" in output


def test_捕まえ先はStringIOとして使える() -> None:
    buffer = gui._Capture()
    buffer.reconfigure(encoding="utf-8", errors="replace")
    buffer.write("本文")
    assert buffer.getvalue() == "本文"


# ---------------------------------------------------------------------------
# 承認(D-14)を画面から
# ---------------------------------------------------------------------------
def test_承認の途中経過が承認の列に出る() -> None:
    row = gui.ModelRow(
        "x", "x", gui.KIND_ADDED, approvals=["甲野"], approvals_required=2,
    )
    assert row.approval_label == "未承認(1/2)"


def test_取消の途中経過も出る() -> None:
    row = gui.ModelRow(
        "x", "x", gui.KIND_ADDED, approved=True,
        revocations=["甲野"], revocations_required=2,
    )
    assert row.approval_label == "承認済み(取消 1/2)"


def test_未承認なら承認ボタンだけ押せる() -> None:
    states = gui.button_states(model(), "model")
    assert states["approve"] is True and states["unapprove"] is False


def test_承認済みは取り消しだけできる() -> None:
    """変更するには、まず承認を取り消す(D-15)。"""
    approved_row = model(approved=True)
    states = gui.button_states(approved_row, "model")
    assert states == {
        "disable": False, "enable": False, "rollback": False, "remove": False,
        "approve": False, "unapprove": True,
    }
    assert "取り消して" in gui.selection_hint(approved_row, "model")


def test_承認済みでも版の行からは何もできない() -> None:
    assert set(gui.button_states(model(approved=True), "version").values()) == {False}


def test_もとからあるモデルは承認も取り消しもできない() -> None:
    core = gui.ModelRow("text_processing", "文章処理", gui.KIND_CORE)
    assert set(gui.button_states(core, "model").values()) == {False}


def test_承認の様子を材料から受け取る() -> None:
    rows = gui.collect_models(
        registry(duty_summary=added()), set(), {}, {},
        approvals={"duty_summary": {
            "approvals": ["甲野"], "revocations": [],
            "approvals_required": 2, "revocations_required": 2,
        }},
    )
    assert rows[0].approvals == ["甲野"]
    assert rows[0].approval_label == "未承認(1/2)"
