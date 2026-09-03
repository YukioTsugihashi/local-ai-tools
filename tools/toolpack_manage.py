r"""追加ツールの管理 — 一覧・無効化・有効化・削除・前の版へ戻す。

使い方:
  ...python.exe tools\toolpack_manage.py list
  ...python.exe tools\toolpack_manage.py disable <id>
  ...python.exe tools\toolpack_manage.py enable  <id>
  ...python.exe tools\toolpack_manage.py rollback <id> <版>
  ...python.exe tools\toolpack_manage.py approve <id> --by 甲野   (承認へ1票)
  ...python.exe tools\toolpack_manage.py unapprove <id> --by 甲野 (取り消しへ1票)
  ...python.exe tools\toolpack_manage.py remove  <id>            (登録を外す。ファイルは残る)
  ...python.exe tools\toolpack_manage.py purge   <id> --yes      (ファイル実体を消す)

追加と更新は `tools/toolpack_install.py`。設計の正本は TOOL_PACKAGE_DECISIONS.md(D-10)。

**方針(台帳 D-10)**

- 無効化と削除は同じ道の長さ違い。どちらも Open WebUI からは消し、
  **ファイル実体は残す**。消したら戻せないため(外部バックアップ D-17 が未決)
- 前の版へ戻すのは「有効版の差し替え + その版の Pipe を貼り直す」だけ。
  版ごとの `generated/` が残っているので、貼り直す Pipe は既に手元にある
- **承認は版ごと。** 戻した先が未承認なら表示は `【未承認】` へ戻る(D-14)
- 実体の削除だけは `--yes` を要求する。**登録が残っているものは消さない**

**成功と言ってよい条件**

**台帳・Open WebUI・稼働中のハブ(8010)の3か所が目的の状態になったと
確かめられたときだけ成功にする。** 一部しか消えていない・確かめられなかった、を
成功に混ぜると、直っていないのに直ったと思って先へ進んでしまう。
確かめられなかったときは `Unconfirmed` で、何を確認できなかったかを示す。
途中で失敗したら、**台帳を戻すだけでなく、前のPipeを貼り直して**元の状態へ戻す。

**承認済みは変えさせない(D-15)。** 画面のボタンを暗くするだけでは足りない。
コマンドから直接呼ばれることもあり、画面を出したあとに承認状態が変わることもある。

**管理操作は1件ずつ。** registry の「読む→変える→書く」の全体をプロセス間ロックで
囲む(`toolpack_store.admin_lock`)。画面を2つ開いた場合や、
画面とコマンドを同時に使った場合に、一方の変更が消えるのを防ぐ。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import toolpack_pipegen  # noqa: E402
import toolpack_state  # noqa: E402
import toolpack_store  # noqa: E402


class ManageError(RuntimeError):
    """利用者が直せる失敗。理由と対処を持つ。"""


class Unconfirmed(ManageError):
    """やったつもりだが、そうなったことを確かめられなかった。

    **成功として扱わない。** 確かめられていないものを「できました」と言うと、
    直っていないのに直ったと思って先へ進んでしまう。
    """


def _client():
    import open_webui_deploy as deploy

    return deploy, deploy.ApiClient(
        base_url=deploy.resolve_base_url(), api_key=deploy.load_api_key()
    )


def _entry(store: toolpack_store.ToolpackStore, tool_id: str) -> dict:
    entry = (store.load_registry()["tools"]).get(tool_id)
    if entry is None:
        known = "、".join(sorted(store.load_registry()["tools"])) or "なし"
        raise ManageError(f"登録されていないツールです: {tool_id}(登録済み: {known})")
    return entry


def _refuse_if_approved(entry: dict, tool_id: str, what: str) -> None:
    """承認済みは変えさせない(台帳 D-15)。

    **画面のボタンを暗くするだけでは足りない。** コマンドから直接呼ばれることもあり、
    画面を出したあとに承認状態が変わることもある。実際に処理する側で断る。
    """
    if toolpack_store.is_approved(entry):
        raise ManageError(
            f"{tool_id} は承認済みのため、{what}はできません。\n"
            "承認を外してからにしてください(承認の操作は企画課の担当です)"
        )


def _verify(tool_id: str, *, in_openwebui: bool, in_hub: bool) -> list[str]:
    """3か所が目的の状態になったかを確かめ、**合っていない点だけ**返す。

    未確認は「合っている」に数えない(台帳 D-10)。
    """
    problems: list[str] = []
    actual_owui = toolpack_state.openwebui_has(tool_id)
    if actual_owui is None:
        problems.append("Open WebUI へ問い合わせられませんでした")
    elif actual_owui is not in_openwebui:
        problems.append(
            "Open WebUI の登録が"
            + ("残っています" if actual_owui else "ありません")
        )
    actual_hub = toolpack_state.hub_recognizes(tool_id)
    if actual_hub is None:
        problems.append("稼働中のハブ(8010)へ問い合わせられませんでした")
    elif actual_hub is not in_hub:
        if actual_hub:
            # ハブは呼ばれるたびに台帳を読み直すので、普通はここへ来ない。
            # 来るのは、稼働中のサービスが**古いコードのまま**動いている場合
            problems.append(
                "稼働中のハブがまだ配っています"
                "(ローカルサービスが古いコードのままの可能性があります。"
                "タスク minutes-pipeline-local-services を再起動してください)"
            )
        else:
            problems.append("稼働中のハブが認識していません")
    return problems


def _deploy_version(store: toolpack_store.ToolpackStore, tool_id: str, version: str) -> None:
    """その版の生成Pipeを Open WebUI へ貼る。"""
    pipe = store.generated_dir(tool_id, version) / toolpack_pipegen.pipe_filename(tool_id)
    if not pipe.is_file():
        raise ManageError(
            f"その版の生成Pipeがありません: {pipe}\n"
            "版の置き場が消えている可能性があります。もう一度取り込み直してください"
        )
    deploy, client = _client()
    if deploy.command_deploy(client, pipe, None, True) != 0:
        raise ManageError(
            f"Open WebUI へ登録できませんでした({tool_id})。"
            "表示された差分と理由を確認してください"
        )


def _remove_from_openwebui(tool_id: str) -> list[str]:
    """Function とモデル設定の両方を消す。消せなかったものを返す。"""
    _, client = _client()
    removed = client.remove_pipe_completely(tool_id)
    return [name for name, gone in removed.items() if not gone]


# ---------------------------------------------------------------------------
# 一覧
# ---------------------------------------------------------------------------
def command_list(store: toolpack_store.ToolpackStore) -> int:
    tools = store.load_registry()["tools"]
    if not tools:
        print("登録されている追加ツールはありません。")
        return 0
    for tool_id, entry in sorted(tools.items()):
        active = entry.get("active_version", "?")
        enabled = entry.get("enabled", True)
        mark = "有効" if enabled else "**無効**"
        # **承認は版ごと**(D-14)。`status` を直接読むと、版ごとの承認へ移ったとき
        # 画面と一覧で表示がずれる。判定は is_approved に集める
        label = "承認済み" if toolpack_store.is_approved(entry, active) else "未承認"
        state = store.approval_state(tool_id, active)
        # 途中まで集まっている票があれば見えるようにする(あと何人か分かるように)
        if not state["approved"] and state["approvals"]:
            label += f"({len(state['approvals'])}/{state['approvals_required']}人)"
        elif state["approved"] and state["revocations"]:
            label += f"(取消 {len(state['revocations'])}/{state['revocations_required']}人)"
        print(f"{tool_id}  版 {active}  {mark}  {label}")
        # 承認済みなら「取消に賛成した人」、未承認なら「承認した人」。
        # 決め手になった票は残してあるので、取り違えると**承認した人が
        # 取消側の人として出てしまう**
        if state["approved"]:
            for who in state["revocations"]:
                print(f"    取消: {who}")
        else:
            for who in state["approvals"]:
                print(f"    承認: {who}")
        others = [v for v in sorted(entry.get("versions", {})) if v != active]
        if others:
            print(f"    残っている版: {'、'.join(others)}")
    orphans = store.orphan_versions()
    if orphans:
        print("")
        print("登録の無い残置(消したいときは purge):")
        for orphan in orphans:
            print(f"    {orphan.tool_id} {orphan.version}")
    return 0


# ---------------------------------------------------------------------------
# 無効化・有効化
# ---------------------------------------------------------------------------
def command_disable(store: toolpack_store.ToolpackStore, tool_id: str) -> int:
    with toolpack_store.admin_lock():
        entry = _entry(store, tool_id)
        _refuse_if_approved(entry, tool_id, "無効化")
        if entry.get("enabled", True) is False:
            print(f"{tool_id} は既に無効です。")
            return 0
        version = str(entry.get("active_version") or "")

        # 先に台帳を止める。ハブは discover() のたびに読み直すので即座に外れる
        store.set_enabled(tool_id, False)
        try:
            remaining = _remove_from_openwebui(tool_id)
        except Exception as error:
            store.set_enabled(tool_id, True)  # 片側だけ止まった状態にしない
            raise ManageError(
                f"Open WebUI から外せませんでした({type(error).__name__})。"
                "台帳は元へ戻しました"
            ) from error

        if remaining:
            # 一部だけ消えた状態は**成功ではない**。元へ戻す
            problems = _restore_enabled(store, tool_id, version)
            raise ManageError(
                f"Open WebUI から完全には外せませんでした({'、'.join(remaining)})。\n"
                + _restore_message(tool_id, problems)
            )

        unconfirmed = _verify(tool_id, in_openwebui=False, in_hub=False)
        if unconfirmed:
            raise Unconfirmed(
                f"{tool_id} を無効にしましたが、そうなったことを確かめられませんでした。\n"
                + "\n".join(f"  - {item}" for item in unconfirmed)
                + "\n台帳とファイルはそのままです。"
                "状態を確かめてから、必要なら enable で戻してください"
            )
        print(f"{tool_id} を無効にしました。ファイルと版は残してあります。")
        print("モデル一覧からは消えています。enable で戻せます。")
        return 0


def _restore_enabled(store, tool_id: str, version: str) -> list[str]:
    """無効化の取り消し。台帳を戻し、その版のPipeを貼り直す。戻せなかった点を返す。"""
    problems: list[str] = []
    try:
        store.set_enabled(tool_id, True)
    except Exception as error:
        problems.append(f"台帳を有効へ戻せませんでした({type(error).__name__})")
    if version:
        try:
            _deploy_version(store, tool_id, version)
        except Exception as error:
            problems.append(f"Pipeを貼り直せませんでした({error})")
    return problems


def _restore_message(tool_id: str, problems: list[str]) -> str:
    if not problems:
        return "元の状態へ戻しました。"
    return (
        "**完全には戻せませんでした。** 次を手で確認してください:\n"
        + "\n".join(f"  - {item}" for item in problems)
        + f"\n  管理画面で {tool_id} の登録を確かめてください。"
    )


def command_enable(store: toolpack_store.ToolpackStore, tool_id: str) -> int:
    with toolpack_store.admin_lock():
        entry = _entry(store, tool_id)
        _refuse_if_approved(entry, tool_id, "有効化")
        version = str(entry.get("active_version") or "")
        if not version:
            raise ManageError(
                f"{tool_id} に有効版がありません。registry.json を確認してください"
            )
        try:
            _deploy_version(store, tool_id, version)
        except Exception:
            # 途中まで登録されている可能性がある。無効のままにするので残骸を掃除する
            _sweep_openwebui(tool_id)
            raise

        store.set_enabled(tool_id, True)
        unconfirmed = _verify(tool_id, in_openwebui=True, in_hub=True)
        if unconfirmed:
            store.set_enabled(tool_id, False)
            _sweep_openwebui(tool_id)
            raise Unconfirmed(
                f"{tool_id} を有効にできたことを確かめられませんでした。\n"
                + "\n".join(f"  - {item}" for item in unconfirmed)
                + "\n無効のままへ戻しました。"
            )
        print(f"{tool_id}(版 {version})を有効にしました。")
        return 0


def _sweep_openwebui(tool_id: str) -> None:
    """無効へ戻すときに、途中まで登録されたものを消す。消せなければそう伝える。"""
    try:
        remaining = _remove_from_openwebui(tool_id)
    except Exception as error:
        print(
            f"Open WebUI の残りを消せませんでした({type(error).__name__})。"
            f"管理画面で {tool_id} を確認してください。"
        )
        return
    if remaining:
        print(
            f"Open WebUI に残りがあります({'、'.join(remaining)})。"
            f"管理画面で {tool_id} を確認してください。"
        )


# ---------------------------------------------------------------------------
# 前の版へ戻す
# ---------------------------------------------------------------------------
def command_rollback(store: toolpack_store.ToolpackStore, tool_id: str, version: str) -> int:
    with toolpack_store.admin_lock():
        entry = _entry(store, tool_id)
        _refuse_if_approved(entry, tool_id, "版の切り替え")
        known = entry.get("versions", {})
        if version not in known:
            raise ManageError(
                f"その版は登録されていません: {version}\n"
                f"ある版: {'、'.join(sorted(known)) or 'なし'}"
            )
        current = str(entry.get("active_version") or "")
        if version == current:
            print(f"{tool_id} は既に版 {version} です。")
            return 0
        if not store.package_dir(tool_id, version).is_dir():
            raise ManageError(
                f"版 {version} の置き場がありません: {store.version_dir(tool_id, version)}\n"
                "実体が消えています。もう一度取り込み直してください"
            )

        # 追加・更新と同じ順序:先に台帳、後に Pipe(台帳 D-10)
        previous = store.set_active_version(tool_id, version)
        try:
            _deploy_version(store, tool_id, version)
        except Exception as error:
            problems = _restore_version(store, tool_id, previous)
            raise ManageError(
                f"版 {version} のPipeを配れませんでした({error})\n"
                + _restore_message(tool_id, problems)
            ) from error

        unconfirmed = _verify(tool_id, in_openwebui=True, in_hub=True)
        if unconfirmed:
            problems = _restore_version(store, tool_id, previous)
            raise Unconfirmed(
                f"{tool_id} を版 {version} へ戻せたことを確かめられませんでした。\n"
                + "\n".join(f"  - {item}" for item in unconfirmed)
                + "\n" + _restore_message(tool_id, problems)
            )
        print(f"{tool_id} を版 {version} へ戻しました(前は {previous})。")
        if not toolpack_store.is_approved(entry, version):
            print("この版は【未承認】です。承認は版ごとに記録されます。")
        return 0


def _restore_version(store, tool_id: str, previous: str) -> list[str]:
    """版の切り替えを戻し、**前の版のPipeも貼り直す**。戻せなかった点を返す。

    台帳だけ戻すと、Open WebUI には切り替え先のPipeが残る。
    そうなると台帳と実物が食い違い、どちらが動いているか分からなくなる。
    """
    problems: list[str] = []
    if not previous:
        return ["戻す先の版が分かりません"]
    try:
        store.set_active_version(tool_id, previous)
    except Exception as error:
        problems.append(f"有効版を {previous} へ戻せませんでした({type(error).__name__})")
    try:
        _deploy_version(store, tool_id, previous)
    except Exception as error:
        problems.append(f"前の版({previous})のPipeを貼り直せませんでした({error})")
    return problems


# ---------------------------------------------------------------------------
# 承認・取消(台帳 D-14)
# ---------------------------------------------------------------------------
def _redeploy_with_approval(store, tool_id: str, version: str, approved: bool) -> None:
    """承認の状態に合わせて生成Pipeを作り直し、貼り直す。

    表示名(`【未承認】` の有無)は生成Pipeが持っているので、
    **承認しただけでは画面の名前は変わらない。** ここまでやって初めて変わる。
    """
    toolpack_pipegen.generate(
        store.package_dir(tool_id, version),
        store.generated_dir(tool_id, version),
        approved=approved,
    )
    _deploy_version(store, tool_id, version)


def command_approve(store: toolpack_store.ToolpackStore, tool_id: str,
                    name: str, *, revoke: bool = False) -> int:
    word = "取り消し" if revoke else "承認"
    with toolpack_store.admin_lock():
        entry = _entry(store, tool_id)
        version = str(entry.get("active_version") or "")
        if not version:
            raise ManageError(f"{tool_id} に有効版がありません")
        policy = store.load_policy()
        if not policy.usable:
            # **壊れた設定を「自由入力」に落とさない。** 落とすと、名前を絞って
            # いたはずが誰でも承認できる状態になる(Codex指摘・2026-09-02)
            raise ManageError(
                f"承認の設定({toolpack_store.POLICY_FILE})が読めないため、"
                f"{word}はできません。\n"
                + "\n".join(f"  - {item}" for item in policy.problems)
                + "\n設定を直すか、ファイルごと消してください"
                "(消せば2人/2人・自由入力に戻ります)"
            )

        before = toolpack_store.is_approved(entry, version)

        # **もう人数に届いている票**があれば、新しい票を入れずに確定させる。
        # 人数を途中で下げたときに起きる(入れた本人はもう押せない)
        settled = store.settle_pending(tool_id, version)
        if settled is not None:
            return _finish_decision(
                store, tool_id, version, settled, before,
                f"人数に届いていたので確定しました({name} さんの操作)",
            )
        try:
            state = store.add_vote(tool_id, version, name, revoke=revoke)
        except toolpack_store.StoreError as error:
            # 呼ぶ側が扱う例外を1種類に保つ(画面もコマンドも同じ扱いにできる)
            raise ManageError(str(error)) from error
        got, need = (
            (len(state["revocations"]), state["revocations_required"]) if revoke
            else (len(state["approvals"]), state["approvals_required"])
        )
        if state["approved"] == before:
            # まだ人数に届いていない。**台帳だけ進んだ状態でよい**
            print(f"{name} さんの{word}を受け付けました({got}/{need} 人)。")
            print(f"あと {need - got} 人で{word}になります。")
            return 0

        # 人数に届いた。表示名が変わるので Pipe を作り直して貼り直す
        return _finish_decision(
            store, tool_id, version, state, before,
            f"{name} さんの{word}を受け付けました",
        )


def _finish_decision(store, tool_id: str, version: str, state: dict,
                     before: bool, headline: str) -> int:
    """決まった状態を Open WebUI まで反映する。失敗したら投票前へ丸ごと戻す。"""
    word = "取り消し" if before else "承認"
    try:
        _redeploy_with_approval(store, tool_id, version, state["approved"])
    except Exception as error:
        problems = _undo_vote(store, tool_id, version, state["snapshot"], before)
        raise ManageError(
            f"{word}の反映に失敗しました({error})\n"
            + _restore_message(tool_id, problems)
        ) from error

    unconfirmed = _verify(tool_id, in_openwebui=True, in_hub=True)
    if unconfirmed:
        problems = _undo_vote(store, tool_id, version, state["snapshot"], before)
        raise Unconfirmed(
            f"{word}を反映できたことを確かめられませんでした。\n"
            + "\n".join(f"  - {item}" for item in unconfirmed)
            + "\n" + _restore_message(tool_id, problems)
        )
    label = "承認済み" if state["approved"] else "未承認"
    print(f"{headline}。{tool_id} {version} は「{label}」になりました。")
    if state["approved"]:
        print("**この画面からの無効化・削除・入れ替えはできなくなります**(D-15)。")
    return 0


def _undo_vote(store, tool_id: str, version: str, snapshot: dict,
               before: bool) -> list[str]:
    """版の記録を投票前へ丸ごと戻し、戻せなかった点を返す。

    **「入れた1票だけ消す」では戻しきれない。** 決まったときに反対側の票を
    片付けているので、それも一緒に復元する必要がある(Codex指摘・2026-09-02)。
    """
    problems: list[str] = []
    try:
        store.restore_record(tool_id, version, snapshot)
    except Exception as error:
        problems.append(f"承認の記録を戻せませんでした({type(error).__name__})")
    try:
        _redeploy_with_approval(store, tool_id, version, before)
    except Exception as error:
        problems.append(f"元の表示名でPipeを貼り直せませんでした({error})")
    return problems


# ---------------------------------------------------------------------------
# 削除
# ---------------------------------------------------------------------------
def command_remove(store: toolpack_store.ToolpackStore, tool_id: str) -> int:
    with toolpack_store.admin_lock():
        entry = _entry(store, tool_id)
        _refuse_if_approved(entry, tool_id, "登録の取り消し")
        version = str(entry.get("active_version") or "")

        try:
            remaining = _remove_from_openwebui(tool_id)
        except Exception as error:
            raise ManageError(
                f"Open WebUI から外せませんでした({type(error).__name__})。\n"
                "**台帳は変えていません。** 接続を確かめてからやり直してください"
            ) from error
        if remaining:
            # **一部しか消えていないのに台帳から外さない。**
            # 外すと、消し残しを管理する手掛かりが無くなる
            problems = _restore_enabled(store, tool_id, version) if version else []
            raise ManageError(
                f"Open WebUI から完全には外せませんでした({'、'.join(remaining)})。\n"
                "**台帳は変えていません。**\n" + _restore_message(tool_id, problems)
            )

        store.unregister(tool_id)
        unconfirmed = _verify(tool_id, in_openwebui=False, in_hub=False)
        if unconfirmed:
            raise Unconfirmed(
                f"{tool_id} の登録を外しましたが、消えたことを確かめられませんでした。\n"
                + "\n".join(f"  - {item}" for item in unconfirmed)
                + f"\n台帳からは外れています。管理画面で {tool_id} を確認してください。"
                "\nファイルは残っているので、必要なら入れ直せます。"
            )
        print(f"{tool_id} の登録を外しました。")
        print("**ファイルは残しています。** 実体を消すには purge を使ってください。")
        print(f"  {store.installed / tool_id}")
        return 0


def command_purge(store: toolpack_store.ToolpackStore, tool_id: str,
                  version: str | None, yes: bool) -> int:
    target = store.installed / tool_id if version is None else store.version_dir(tool_id, version)
    if not target.is_dir():
        raise ManageError(f"消すものがありません: {target}")
    if not yes:
        print("**これは元に戻せません。** 消すなら --yes を付けてください。")
        print(f"  消える場所: {target}")
        print("  外部バックアップ(D-17)はまだありません。")
        return 1
    try:
        with toolpack_store.admin_lock():
            removed = store.purge_files(tool_id, version)
    except toolpack_store.AdminBusy:
        raise
    except toolpack_store.StoreError as error:
        raise ManageError(str(error)) from error
    for path in removed:
        print(f"消しました: {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="追加ツールを管理する")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="登録されている追加ツールを並べる")
    for name, help_text in (
        ("disable", "一時的に止める(ファイルと版は残す)"),
        ("enable", "止めたものを戻す"),
        ("remove", "登録を外す(ファイルは残る)"),
    ):
        one = sub.add_parser(name, help=help_text)
        one.add_argument("tool_id")

    back = sub.add_parser("rollback", help="前の版へ戻す")
    back.add_argument("tool_id")
    back.add_argument("version")

    for name, help_text in (
        ("approve", "承認へ1票入れる(決まった人数で承認済みになる)"),
        ("unapprove", "承認の取り消しへ1票入れる"),
    ):
        vote = sub.add_parser(name, help=help_text)
        vote.add_argument("tool_id")
        vote.add_argument("--by", required=True, help="承認する人の名前")

    purge = sub.add_parser("purge", help="ファイル実体を消す(元に戻せない)")
    purge.add_argument("tool_id")
    purge.add_argument("--version", default=None, help="版を指定(無指定はツールごと)")
    purge.add_argument("--yes", action="store_true", help="本当に消す")

    args = parser.parse_args(argv)
    store = toolpack_store.ToolpackStore()
    store.ensure_layout()
    try:
        if args.command == "list":
            return command_list(store)
        if args.command == "disable":
            return command_disable(store, args.tool_id)
        if args.command == "enable":
            return command_enable(store, args.tool_id)
        if args.command == "rollback":
            return command_rollback(store, args.tool_id, args.version)
        if args.command == "remove":
            return command_remove(store, args.tool_id)
        if args.command == "purge":
            return command_purge(store, args.tool_id, args.version, args.yes)
        if args.command in ("approve", "unapprove"):
            return command_approve(
                store, args.tool_id, args.by, revoke=args.command == "unapprove"
            )
    except (ManageError, toolpack_store.StoreError) as error:
        print(str(error))
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
