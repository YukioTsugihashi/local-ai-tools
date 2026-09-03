r"""追加ツールの管理画面(Windows デスクトップアプリ)。

使い方:
  ...python.exe tools\toolpack_gui.py

設計の正本は TOOL_PACKAGE_DECISIONS.md(D-11・D-15)。

**この画面は判断を持たない。**
追加・更新は `toolpack_install`、無効化・削除・前の版へ戻すは `toolpack_manage`、
自己診断は `doctor` を呼び、**その出力をそのまま出す**。
試験されているのはコマンド側の経路であり、画面が独自の判断を持つと
テストが実際の挙動を覆わなくなる(台帳 D-11 推奨B)。

**ブラウザ画面にしていない理由**: ハブは既に院内LANの他端末から届く(5-12)。
管理画面をHTTPで足すと、露出面を増やしてから塞ぐことになる(D-16 が未決)。

**一覧の1行 = Open WebUI に登録されているモデル1つ**(D-11)。
利用者がモデル一覧で選ぶものと同じ粒度にする。
そのモデルが使うツールと、残っている版は、モデルの下へ1段下げて畳む。

```
モデル                          版      状態  承認     気になること
▶ 【未承認】Excel比較グラフ      1.1.0  有効  未承認
      ツール  excel_compare_chart 1.1.0 有効
      版      1.0.0             1.0.0  戻せる          ← 選んで「前の版へ戻す」
▶ Excel分析・科別分析シート        -    -     承認済み
      ツール  excel_read                有効
      ツール  excel_comment             有効
```

**承認されているかは列で見る。まとめ方には使わない。**
モデルが増えても行数はモデルの数どまりで、ツールや版では伸びない。
並びは、手を入れる必要があるもの(未承認)を上にする。
もとからある本番モデル(文章処理・Excel分析・ヘルプ)は既に運用で使われており、
この仕組みの承認手続きの対象ではないので**承認済みとして出す**。

**モデルでないものはモデルとして並べない。**
リポジトリにファイルがあるのに登録されていないもの、どのモデルからも
使われていないツールは、「気になること」として実行結果の欄へ出す。
並べると「使えるモデル」に見えてしまうため。

**権限は「人」ではなく「対象」で分ける**(D-15):

| 対象 | できること |
|---|---|
| このアプリで追加した未承認モデル | 無効化・有効化・前の版へ戻す・登録を外す |
| 承認済みモデル | 見るだけ |
| もとからある本番モデル(文章処理・Excel分析・ヘルプ) | 見るだけ |
| 管理外モデル(このアプリの外で登録されたもの) | 見るだけ |

共用Windowsアカウントでは本人確認ができない(D-14 の既知の限界)。
**できない本人確認を前提にした権限は、あるように見えて実際には無い。**
対象で分ければ、誰が操作しても本番モデルは壊れない。
"""

from __future__ import annotations

import io
import os
import queue
import sys
import threading
import traceback
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path

# **pythonw(コンソール無し)から起動されると sys.stdout が None になる。**
# tools/ の多くのモジュールは import 時に無条件で reconfigure を呼ぶため、
# 先に捨て場を用意しておかないと読み込み時に無言で終了する(台帳 5-17)
if sys.stdout is None or sys.stderr is None:  # pragma: no cover - pythonw のときだけ
    _sink = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = _sink
    if sys.stderr is None:
        sys.stderr = _sink

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import toolpack_state  # noqa: E402
import toolpack_store  # noqa: E402

# 出どころ。権限の判定に使う(表示上の主役ではない)
KIND_ADDED = "追加ツール"
KIND_CORE = "もとからある"
KIND_UNMANAGED = "管理外"

# もとからある正式モデル。**明示して持つ**(台帳 D-11)。
# `tools/open*webui*.py` を全部「正式」とみなすと、登録していないものや
# **廃止したもの**まで承認済みとして扱ってしまう。
# 実際 `transcription_refiner` は 2026-08-14 に登録解除した「再登録しない」もので、
# 誤って再登録されたときに「もとからある=承認済み」と出てはいけない。
# ここに無いものが Open WebUI に登録されていれば **管理外**として扱う
CORE_MODEL_IDS = frozenset({
    "text_processing",      # 文章処理
    "excel_analysis",       # Excel分析・科別分析シート
    "hospital_help_pipe",   # 使い方ヘルプ
})
# 登録を解除済みで、**再登録しない**もの。出てきたら理由を添えて知らせる
RETIRED_MODEL_IDS = {
    "transcription_refiner": "2026-08-14 に登録解除。役割が重複するため再登録しない",
}

# 承認の状態。**まとめ方ではなく、行の状態として列に出す**
APPROVAL_UNAPPROVED = "未承認"
APPROVAL_APPROVED = "承認済み"
APPROVAL_UNMANAGED = "管理外"
# 並び順。手を入れる必要があるものを上へ
APPROVAL_ORDER = (APPROVAL_UNAPPROVED, APPROVAL_APPROVED, APPROVAL_UNMANAGED)


@dataclass
class ToolLink:
    """モデルが使っているツール(ハブの接続役)。"""

    name: str
    version: str = ""
    # **確認できなかったことを「無い」と混ぜない**(None = 未確認)
    in_hub: bool | None = None

    @property
    def state_label(self) -> str:
        if self.in_hub is None:
            return "未確認"
        return "有効" if self.in_hub else "**繋がっていない**"


@dataclass
class ModelRow:
    """一覧の1行 = **Open WebUI に登録されているモデル1つ**。

    利用者がモデル一覧で選ぶものと同じ粒度にする。
    ツール(ハブの接続役)や版はモデルの中身なので、この行の下へ畳む。
    """

    model_id: str
    display_name: str
    kind: str
    version: str = ""
    approved: bool = False
    enabled: bool = True
    in_registry: bool = False
    in_openwebui: bool | None = None
    source_path: str = ""   # このモデルの元になっているファイル
    approvals: list[str] = field(default_factory=list)      # いま集まっている承認
    revocations: list[str] = field(default_factory=list)    # いま集まっている取消
    approvals_required: int = 0
    revocations_required: int = 0
    tools: list[ToolLink] = field(default_factory=list)
    versions: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def approval_state(self) -> str:
        """承認の状態そのもの(途中経過を含まない)。**並べ替えはこちらで行う。**

        表示用の `approval_label` には「(1/2)」が付くので、
        そちらで並べ替えると状態が増えるたびに壊れる。
        """
        if self.kind == KIND_UNMANAGED:
            return APPROVAL_UNMANAGED
        if self.kind == KIND_CORE:
            return APPROVAL_APPROVED
        return APPROVAL_APPROVED if self.approved else APPROVAL_UNAPPROVED

    @property
    def approval_label(self) -> str:
        """承認の状態。**一覧では列として出す**(まとめ方には使わない)。

        もとからある本番モデル(文章処理・Excel分析・ヘルプ)は承認済みとして扱う。
        既に運用で使われているもので、この仕組みの承認手続きの対象ではない。
        管理外はそもそも承認の話に乗らないので、そのまま「管理外」と出す。
        """
        if self.kind == KIND_UNMANAGED:
            return APPROVAL_UNMANAGED
        if self.kind == KIND_CORE:
            return APPROVAL_APPROVED
        if self.approved:
            if self.revocations:
                return f"{APPROVAL_APPROVED}(取消 {len(self.revocations)}/{self.revocations_required})"
            return APPROVAL_APPROVED
        if self.approvals:
            return f"{APPROVAL_UNAPPROVED}({len(self.approvals)}/{self.approvals_required})"
        return APPROVAL_UNAPPROVED

    @property
    def state_label(self) -> str:
        # もとからある・管理外の有効/無効は、この画面の管理範囲ではない。
        # 見ていないものを「有効」と言い切らない
        if self.kind != KIND_ADDED:
            return "-"
        if not self.enabled:
            return "無効"
        if self.problems:
            return "要確認"
        return "有効"


def operable(row: ModelRow) -> bool:
    """この行を画面から操作してよいか(台帳 D-15)。

    **承認済み・もとからある・管理外は触らせない。**
    追加に失敗しても既存を壊さない、という方針を画面側でも守る。
    """
    return row.kind == KIND_ADDED and not row.approved


def button_states(row, node: str) -> dict[str, bool]:
    """選んだ行に対して、どのボタンを押せるようにするか(台帳 D-15)。

    **どの行を選んだかでできることを変える。**
    版を選んだまま「登録を外す」が押せると、戻すつもりでモデルごと消える。
    ツールの行は表示だけで、操作の対象にしない。
    """
    keys = ("disable", "enable", "rollback", "remove", "approve", "unapprove")
    if row is None or row.kind != KIND_ADDED or node == "tool":
        return {key: False for key in keys}
    # **承認済みは承認の取り消しだけできる**(D-15)。他は触らせない
    if row.approved:
        return {key: key == "unapprove" and node == "model" for key in keys}
    if node == "version":
        return {key: key == "rollback" for key in keys}
    return {
        "disable": row.enabled,
        "enable": not row.enabled,
        "rollback": len(row.versions) > 1,
        "remove": True,
        "approve": True,
        "unapprove": False,
    }


def selection_hint(row, node: str, version: str = "") -> str:
    """選んだ行について、操作できない理由や注意を1行で返す。"""
    if row is None:
        return ""
    if node == "tool":
        return "ツールの行です(表示のみ)。モデルの行を選んでください"
    if row.kind != KIND_ADDED or row.approved:
        if row.kind == KIND_UNMANAGED:
            return "管理外のため、この画面からは操作しません"
        if row.kind == KIND_CORE:
            return "もとからある本番モデルのため、この画面からは変更できません"
        return "承認済みです(変更するには、まず承認を取り消してください)"
    if node == "version":
        return f"版 {version} を選んでいます(戻すことだけできます)"
    return ""


def core_plugin_links(root: Path = ROOT, known_tools: set[str] | None = None):
    """正式モデルの ({id: 使っているツール名}, {id: ファイルの場所}) を返す。

    どのモデルがどのツールを呼ぶかは、Pipeの中の文字列から拾う
    (例: Excel分析は `"excel_read"` と `"excel_comment"` を持つ)。
    **拾えなくても困らない** — ツールがぶら下がらないだけで、モデルは並ぶ。
    """
    try:
        from open_webui_deploy import parse_frontmatter, resolve_plugin_id
    except Exception:
        return {}, {}
    links: dict[str, set[str]] = {}
    paths: dict[str, str] = {}
    for path in sorted((root / "tools").glob("open*webui*.py")):
        try:
            text = path.read_text(encoding="utf-8")
            plugin_id = resolve_plugin_id(parse_frontmatter(text), None)
        except Exception:
            continue  # id: の無いファイル(デプロイ対象外)は対象外
        if plugin_id not in CORE_MODEL_IDS:
            continue  # 正式モデルは明示したものだけ(廃止済みを拾わないため)
        used = {
            name for name in (known_tools or set())
            if name != plugin_id and f'"{name}"' in text
        }
        links[plugin_id] = used
        paths[plugin_id] = path.relative_to(root).as_posix()
    return links, paths


@dataclass
class Sources:
    """一覧を作るための材料。**渡す数が増えたので束ねる。**"""

    registry: dict
    hub_names: set[str] | None
    models: dict[str, str] | None
    core_links: dict[str, set[str]]
    fallback_names: dict[str, str]
    source_paths: dict[str, str]
    approvals: dict[str, dict] = field(default_factory=dict)


def collect_models(
    registry: dict,
    hub_names: set[str] | None,
    openwebui_models: dict[str, str] | None,
    core_links: dict[str, set[str]],
    fallback_names: dict[str, str] | None = None,
    source_paths: dict[str, str] | None = None,
    approvals: dict[str, dict] | None = None,
) -> list[ModelRow]:
    """3系統を突き合わせて**モデルの一覧**を作る。**I/Oはしない**(呼ぶ側が集める)。

    `hub_names` / `openwebui_models` が None なら「問い合わせられなかった」。
    **未確認を「無い」として扱わない。** 見られなかったことを見えるようにする。

    行になるのは次の2つだけ:

    - Open WebUI に登録されているモデル
    - 台帳にある追加ツール(無効にしていると登録が無いが、戻せるよう出す)

    **リポジトリにファイルがあるだけで登録されていないものはモデルではない。**
    並べると「使えるモデル」に見えてしまうので、気になることとして別に出す。
    """
    added = registry.get("tools", {})
    registered = openwebui_models if openwebui_models is not None else {}
    # Open WebUI から取れないとき(無効にした・繋がらない)は、
    # **内部IDではなくパッケージの表示名**を出す。利用者向けの名前を消さない
    fallback = fallback_names or {}
    paths = source_paths or {}
    votes = approvals or {}

    def in_hub(name: str) -> bool | None:
        return None if hub_names is None else name in hub_names

    rows: dict[str, ModelRow] = {}

    def ensure(model_id: str) -> ModelRow:
        if model_id in rows:
            return rows[model_id]
        entry = added.get(model_id)
        if entry is not None:
            kind = KIND_ADDED
        elif model_id in core_links:
            kind = KIND_CORE
        else:
            kind = KIND_UNMANAGED
        row = ModelRow(
            model_id=model_id,
            display_name=(
                registered.get(model_id) or fallback.get(model_id) or model_id
            ),
            kind=kind,
            in_registry=entry is not None,
            in_openwebui=None if openwebui_models is None else model_id in registered,
            source_path=paths.get(model_id, ""),
        )
        rows[model_id] = row
        return row

    for model_id in sorted(registered):
        ensure(model_id)
    for model_id in sorted(added):
        ensure(model_id)

    for model_id, row in rows.items():
        if row.kind == KIND_ADDED:
            entry = added[model_id]
            row.version = str(entry.get("active_version") or "")
            row.approved = toolpack_store.is_approved(entry)
            row.enabled = entry.get("enabled", True) is not False
            row.versions = sorted(entry.get("versions", {}))
            state = votes.get(model_id) or {}
            row.approvals = list(state.get("approvals") or [])
            row.revocations = list(state.get("revocations") or [])
            row.approvals_required = int(state.get("approvals_required") or 0)
            row.revocations_required = int(state.get("revocations_required") or 0)
            # 追加ツールは1モデル1ツール(生成Pipeがそのツールだけを呼ぶ)
            row.tools = [ToolLink(model_id, row.version, in_hub(model_id))]
            if row.enabled and row.tools[0].in_hub is False:
                row.problems.append("台帳にあるのに稼働中のハブが認識していません")
            if row.enabled and row.in_openwebui is False:
                row.problems.append("Open WebUI に登録がありません")
            if not row.enabled and row.in_openwebui:
                row.problems.append("無効なのに Open WebUI に残っています")
            if not row.enabled and in_hub(model_id):
                row.problems.append("無効なのに稼働中のハブが配っています")
        elif row.kind == KIND_CORE:
            row.tools = [
                ToolLink(name, in_hub=in_hub(name))
                for name in sorted(core_links.get(model_id, ()))
            ]
        else:
            # **勝手に消さない。「管理外」と表示するだけ**(台帳 D-11)
            row.problems.append("この画面の外で登録されたものです")

    # 手を入れる必要があるもの(未承認)を上へ。同じ状態なら表示名順
    return sorted(
        rows.values(),
        key=lambda row: (APPROVAL_ORDER.index(row.approval_state), row.display_name),
    )


def stray_findings(
    hub_names: set[str] | None,
    openwebui_models: dict[str, str] | None,
    core_links: dict[str, set[str]],
    rows: list[ModelRow],
) -> list[str]:
    """モデルではないが気になるもの。**モデルとして並べない**。

    - 正式モデルなのに Open WebUI へ登録されていない
    - **廃止したはずのものが登録されている**
    - 稼働中のハブが配っているのに、どのモデルからも使われていない
    """
    findings: list[str] = []
    if openwebui_models is not None:
        missing = sorted(set(core_links) - set(openwebui_models))
        if missing:
            findings.append(
                f"正式モデルなのに Open WebUI へ登録されていません: {'、'.join(missing)}"
            )
        for model_id, reason in sorted(RETIRED_MODEL_IDS.items()):
            if model_id in openwebui_models:
                findings.append(
                    f"廃止したはずの {model_id} が登録されています({reason})"
                )
    if hub_names is not None:
        used = {tool.name for row in rows for tool in row.tools}
        unused = sorted(hub_names - used)
        if unused:
            findings.append(
                f"どのモデルからも使われていないツール: {'、'.join(unused)}"
            )
    return findings


def gather_sources(store: toolpack_store.ToolpackStore, root: Path = ROOT):
    """一覧の材料を集める(ここだけがI/O)。取れなかったものは None を返す。

    **ハブは稼働中のサービス(8010)へ問い合わせる。**
    同じプロセスで `hub.discover()` を呼んでも、それは「このプロセスで
    読み込めるか」を見ているだけで、**本番のサービスが止まっていても
    『認識している』と表示されてしまう**。
    """
    registry = store.load_registry()
    hub_names = toolpack_state.hub_tool_names()
    models = toolpack_state.openwebui_models()
    # どのモデルがどのツールを呼ぶかを拾うため、既知のツール名を渡す
    known_tools = set(hub_names or ()) | set(registry.get("tools", {}))
    links, core_paths = core_plugin_links(root, known_tools)
    return Sources(
        registry=registry,
        hub_names=hub_names,
        models=models,
        core_links=links,
        fallback_names=package_display_names(store, registry),
        source_paths={**core_paths, **generated_pipe_paths(store, registry, root)},
        approvals={
            model_id: store.approval_state(model_id)
            for model_id in (registry.get("tools") or {})
        },
    )


def generated_pipe_paths(store, registry: dict, root: Path = ROOT) -> dict[str, str]:
    """追加ツールの生成Pipeの場所。**どのファイルが動いているか**を画面で引けるように。"""
    import toolpack_pipegen

    paths: dict[str, str] = {}
    for model_id, entry in (registry.get("tools") or {}).items():
        version = str(entry.get("active_version") or "")
        if not version:
            continue
        target = (
            store.generated_dir(model_id, version)
            / toolpack_pipegen.pipe_filename(model_id)
        )
        try:
            paths[model_id] = target.relative_to(root).as_posix()
        except ValueError:
            paths[model_id] = str(target)
    return paths


def package_display_names(store, registry: dict) -> dict[str, str]:
    """パッケージの `tool.json` から「あるべき表示名」を作る(控え)。

    Open WebUI から表示名を取れないとき——**無効にしている間**や、
    一時的に問い合わせられないとき——に内部IDを出さないための控え。
    承認状態から `【未承認】` を付けるところまで、doctor と同じ判定を使う。
    """
    import doctor

    names: dict[str, str] = {}
    for model_id, entry in (registry.get("tools") or {}).items():
        version = str(entry.get("active_version") or "")
        if not version:
            continue
        try:
            import json as _json

            meta = _json.loads(
                (store.package_dir(model_id, version) / "tool.json")
                .read_text(encoding="utf-8")
            )
        except Exception:
            continue  # 読めなければ控えなし(内部IDのまま)
        names[model_id] = doctor.expected_display_name(
            str(meta.get("display_name") or model_id),
            toolpack_store.is_approved(entry, version),
        )
    return names


# ---------------------------------------------------------------------------
# 長い処理を画面と切り離す
# ---------------------------------------------------------------------------
class _Capture(io.StringIO):
    """出力の捕まえ先。**`reconfigure` を受け流す。**

    `tools/` の多くのモジュールは import 時に無条件で
    `sys.stdout.reconfigure(...)` を呼ぶ。素の StringIO へ差し替えていると、
    捕まえている最中に読み込んだモジュールがそこで落ちる
    (2026-09-01、自己診断ボタンで実際に起きた)。
    台帳 5-17 の「pythonw では stdout が None」と同じ種類の罠である。
    """

    def reconfigure(self, **kwargs) -> None:
        return None


class Worker:
    """別スレッドで動かし、結果を待ち行列へ積む。**画面を固めないため。**

    受入検査は隔離実行を何度も行うので数十秒かかり、さらに
    利用者のジョブと順番待ちを共有する(台帳 D-9)。
    """

    def __init__(self) -> None:
        self.results: queue.Queue = queue.Queue()
        self.busy = False

    def run(self, label: str, func, *, capture: bool = True) -> threading.Thread:
        """`capture` はコマンド側の出力を拾うかどうか。

        **拾うのは1つだけにする。** `redirect_stdout` はプロセス全体に効くので、
        拾う処理を2つ同時に走らせると、片方の出力がもう片方の箱へ入ってしまう。
        一覧の読み直しは出力を持たないので拾わない。
        """
        self.busy = True

        def body() -> None:
            buffer = _Capture()
            value = None
            ok = True
            try:
                if capture:
                    # コマンド側の出力をそのまま拾う。**画面で要約しない**
                    with redirect_stdout(buffer), redirect_stderr(buffer):
                        value = func()
                else:
                    value = func()
                ok = value is not False
            except Exception:
                ok = False
                buffer.write("\n" + traceback.format_exc())
            finally:
                self.busy = False
                self.results.put((label, ok, buffer.getvalue(), value))

        thread = threading.Thread(target=body, daemon=True, name=f"toolpack-{label}")
        thread.start()
        return thread

    def take(self):
        """終わったものがあれば (label, ok, 出力, 戻り値) を返す。無ければ None。"""
        try:
            return self.results.get_nowait()
        except queue.Empty:
            return None


# ---------------------------------------------------------------------------
# 操作(コマンド側をそのまま呼ぶ)
# ---------------------------------------------------------------------------
def action_install(path: Path, update: bool, store) -> bool:
    import toolpack_install as installer

    report = installer.install(Path(path), apply=True, update=update, store=store)
    print(installer.format_report(report))
    return report.ok


def action_manage(command: str, store, *args) -> bool:
    import toolpack_manage as manage

    handlers = {
        "disable": manage.command_disable,
        "enable": manage.command_enable,
        "remove": manage.command_remove,
        "rollback": manage.command_rollback,
        "approve": lambda store, tool_id, name: manage.command_approve(
            store, tool_id, name, revoke=False
        ),
        "unapprove": lambda store, tool_id, name: manage.command_approve(
            store, tool_id, name, revoke=True
        ),
    }
    try:
        return handlers[command](store, *args) == 0
    except (manage.ManageError, toolpack_store.StoreError) as error:
        print(str(error))
        return False


def action_doctor() -> bool:
    """自己診断。**`doctor.main()` は呼ばない。**

    `main()` は引数を取らず `sys.argv` を読み、最後に `SystemExit` を投げる。
    画面から呼ぶと、この画面の起動引数を診断の指定として読んでしまうし、
    終了例外で処理が止まる。中身の関数をそのまま使う。
    """
    import doctor

    findings = doctor.collect_findings()
    print(doctor.format_report(findings))
    return doctor.exit_code(findings) == 0


# ---------------------------------------------------------------------------
# 画面
# ---------------------------------------------------------------------------
class App:  # pragma: no cover - 画面の組み立ては単体テストの対象外
    """一覧・追加・詳細・自己診断。判断は持たず、コマンド側を呼ぶだけ。"""

    POLL_MS = 200

    def __init__(self, master, store: toolpack_store.ToolpackStore | None = None) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.master = master
        self.store = store or toolpack_store.ToolpackStore()
        self.store.ensure_layout()
        self.worker = Worker()      # 追加・更新・管理操作
        self.refresher = Worker()   # 一覧の読み直し(操作と並行してよい)
        self.rows: list[ModelRow] = []
        self.reported_strays: set[str] = set()  # 同じ注意を毎回出さない

        master.title("追加ツールの管理")
        master.geometry("980x680")

        bar = ttk.Frame(master, padding=(8, 8, 8, 4))
        bar.pack(fill="x")
        self.add_button = ttk.Button(bar, text="ツールを追加...", command=self.on_add)
        self.add_button.pack(side="left")
        self.update_button = ttk.Button(bar, text="ツールを入れ替え...", command=self.on_update)
        self.update_button.pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="自己診断", command=self.on_doctor).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="再読込", command=self.reload).pack(side="left", padx=(6, 0))
        self.status = ttk.Label(bar, text="")
        self.status.pack(side="right")

        # **モデル1つで1行**。その下へ版などを1段下げて畳んでおく。
        # 承認かどうかは列で見る(まとめ方には使わない)
        columns = ("version", "state", "approval", "note")
        self.tree = ttk.Treeview(
            master, columns=columns, show="tree headings", height=14
        )
        self.tree.heading("#0", text="モデル")
        self.tree.column("#0", width=300, anchor="w")
        for key, text, width in (
            ("version", "版", 90), ("state", "状態", 70),
            ("approval", "承認", 90), ("note", "気になること", 400),
        ):
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=8)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self.on_select())

        ops = ttk.Frame(master, padding=(8, 6))
        ops.pack(fill="x")
        self.buttons: dict[str, object] = {}
        for key, text, handler in (
            ("disable", "無効にする", self.on_disable),
            ("enable", "有効に戻す", self.on_enable),
            ("rollback", "前の版へ戻す...", self.on_rollback),
            ("remove", "登録を外す", self.on_remove),
            ("approve", "承認する...", self.on_approve),
            ("unapprove", "承認を取り消す...", self.on_unapprove),
        ):
            button = ttk.Button(ops, text=text, command=handler, state="disabled")
            button.pack(side="left", padx=(0, 6))
            self.buttons[key] = button
        self.hint = ttk.Label(ops, text="")
        self.hint.pack(side="left", padx=(10, 0))

        ttk.Label(master, text="実行結果", padding=(8, 4)).pack(anchor="w")
        self.log = tk.Text(master, height=12, wrap="word")
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log.configure(state="disabled")

        self._poll_id: str | None = None
        self.reload()
        self._poll_id = master.after(self.POLL_MS, self.poll)

    # -- 表示 ---------------------------------------------------------------
    def write(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def reload(self) -> None:
        """一覧を読み直す。**別スレッドで集める**(ハブとOpen WebUIへ問い合わせるため)。"""
        if self.refresher.busy:
            return
        self.refresher.run("再読込", lambda: gather_sources(self.store), capture=False)

    def apply_rows(self, gathered) -> None:
        self.rows = collect_models(
            gathered.registry, gathered.hub_names, gathered.models,
            gathered.core_links, gathered.fallback_names, gathered.source_paths,
            gathered.approvals,
        )
        hub_names, models = gathered.hub_names, gathered.models
        strays = stray_findings(hub_names, models, gathered.core_links, self.rows)

        # 開いている・選んでいる状態を覚えておく。読み直しのたびに
        # 畳み直されると、操作のあとに毎回開き直すことになる
        opened = {
            name for name in self.tree.get_children("")
            if self.tree.item(name, "open")
        }
        previous = self.tree.selection()

        self.tree.delete(*self.tree.get_children())
        for row in self.rows:
            # **1行 = Open WebUI のモデル1つ。** 中身はこの下へ畳む
            self.tree.insert(
                "", "end", iid=row.model_id, text=row.display_name,
                open=row.model_id in opened,
                values=(
                    row.version or "-", row.state_label,
                    row.approval_label, "、".join(row.problems),
                ),
            )
            self.tree.insert(
                row.model_id, "end", iid=f"{row.model_id}#file",
                text="ファイル",
                values=("", "", "", row.source_path or "リポジトリに実体がありません"),
            )
            for who in (row.revocations if row.approved else row.approvals):
                self.tree.insert(
                    row.model_id, "end", iid=f"{row.model_id}#vote:{who}",
                    text=("取消" if row.approved else "承認"),
                    values=("", "", who, ""),
                )
            for tool in row.tools:
                self.tree.insert(
                    row.model_id, "end", iid=f"{row.model_id}#tool:{tool.name}",
                    text=f"ツール  {tool.name}",
                    values=(tool.version or "", tool.state_label, "", ""),
                )
            # 残っている版。選んでから「前の版へ戻す」を押せるようにする
            for version in row.versions:
                if version == row.version:
                    continue
                self.tree.insert(
                    row.model_id, "end", iid=f"{row.model_id}@{version}",
                    text=f"版      {version}", values=(version, "戻せる", "", ""),
                )
        for name in previous:
            if self.tree.exists(name):
                self.tree.selection_set(name)
                self.tree.see(name)
                break

        unreachable = []
        if hub_names is None:
            unreachable.append("稼働中のハブ(8010)")
        if models is None:
            unreachable.append("Open WebUI")
        if unreachable:
            self.status.configure(text="、".join(unreachable) + " を確認できませんでした")
        else:
            self.status.configure(text=f"モデル {len(self.rows)} 件")
        for finding in strays:
            if finding not in self.reported_strays:
                self.reported_strays.add(finding)
                self.write(f"気になること: {finding}")
        self.on_select()

    def selection(self) -> tuple[ModelRow | None, str, str]:
        """選ばれているモデル・版・行の種類("model" / "version" / "tool")を返す。

        **どの行を選んだかでできることを変える。**
        版を選んだまま「登録を外す」が押せると、戻すつもりでモデルごと消える。
        """
        chosen = self.tree.selection()
        if not chosen:
            return None, "", ""
        name = chosen[0]
        if "#" in name:
            model_id, kind, version = name.split("#")[0], "tool", ""
        elif "@" in name:
            model_id, _, version = name.partition("@")
            kind = "version"
        else:
            model_id, version, kind = name, "", "model"
        row = next((item for item in self.rows if item.model_id == model_id), None)
        return row, version, kind

    def selected(self) -> ModelRow | None:
        return self.selection()[0]

    def on_select(self) -> None:
        row, version, node = self.selection()
        for key, enabled in button_states(row, node).items():
            self.buttons[key].configure(state="normal" if enabled else "disabled")
        self.hint.configure(text=selection_hint(row, node, version))

    # -- 実行 ---------------------------------------------------------------
    def start(self, label: str, func) -> None:
        if self.worker.busy:
            self.write("いま別の処理を実行しています。終わるまでお待ちください。")
            return
        self.status.configure(text=f"{label} を実行中...(数十秒かかることがあります)")
        self.write(f"--- {label} ---")
        self.worker.run(label, func)

    def poll(self) -> None:
        gathered = self.refresher.take()
        if gathered is not None:
            _, ok, output, value = gathered
            if ok and value is not None:
                self.apply_rows(value)
            else:
                self.status.configure(text="一覧を読めませんでした")
                self.write(output or "一覧を読めませんでした")

        finished = self.worker.take()
        if finished is not None:
            label, ok, output, _ = finished
            self.write(output or "(出力はありません)")
            self.write(f"{label}: {'完了' if ok else '**失敗**'}")
            self.reload()
        self._poll_id = self.master.after(self.POLL_MS, self.poll)

    def stop(self) -> None:
        """待ち受けを止める。**画面を閉じる前に呼ぶ。**

        止めずに破棄すると、予約済みの after が破棄後に動いて落ちることがある。
        """
        if self._poll_id is not None:
            try:
                self.master.after_cancel(self._poll_id)
            except Exception:
                pass
            self._poll_id = None

    def _choose_package(self) -> Path | None:
        from tkinter import filedialog

        chosen = filedialog.askopenfilename(
            title="持ち込んだ .localtool を選んでください",
            filetypes=[("追加ツール", "*.localtool")],
            initialdir=str(self.store.incoming),
        )
        return Path(chosen) if chosen else None

    def on_add(self) -> None:
        package = self._choose_package()
        if package:
            self.start("追加", lambda: action_install(package, False, self.store))

    def on_update(self) -> None:
        package = self._choose_package()
        if package:
            self.start("入れ替え", lambda: action_install(package, True, self.store))

    def on_disable(self) -> None:
        row = self.selected()
        if row:
            self.start(f"{row.model_id} を無効化",
                       lambda: action_manage("disable", self.store, row.model_id))

    def on_enable(self) -> None:
        row = self.selected()
        if row:
            self.start(f"{row.model_id} を有効化",
                       lambda: action_manage("enable", self.store, row.model_id))

    def on_remove(self) -> None:
        from tkinter import messagebox

        row = self.selected()
        if not row:
            return
        if not messagebox.askyesno(
            "登録を外す",
            f"{row.model_id} の登録を外します。\n\n"
            "Open WebUI のモデル一覧からは消えます。\n"
            "**ファイルは残す**ので、必要なら入れ直せます。\n\nよろしいですか。",
        ):
            return
        self.start(f"{row.model_id} の登録を外す",
                   lambda: action_manage("remove", self.store, row.model_id))

    def on_rollback(self) -> None:
        from tkinter import simpledialog

        row, version, _node = self.selection()
        if not row:
            return
        others = [item for item in row.versions if item != row.version]
        if not version:
            # 版を選んでいなければ聞く。**打ち間違いを避けたいので、
            # 一覧でツールを開いて版を選んでもらうのが本来の道**
            version = simpledialog.askstring(
                "前の版へ戻す",
                f"{row.model_id} をどの版へ戻しますか。\n"
                f"いまは {row.version} です。\n"
                f"戻せる版: {'、'.join(others) or 'なし'}\n\n"
                "(一覧でツールを開いて版を選んでから押すと、この確認は出ません)",
            )
        if not version:
            return
        self.start(f"{row.model_id} を {version} へ戻す",
                   lambda: action_manage("rollback", self.store, row.model_id, version))

    def _ask_name(self, title: str) -> str:
        """承認する人の名前を聞く。**決めておけば選ぶだけ**(打ち間違いを避ける)。"""
        from tkinter import simpledialog

        approvers = self.store.load_policy().approvers
        prompt = "あなたの名前を入れてください。"
        if approvers:
            prompt += "\n登録されている人: " + "、".join(approvers)
        return (simpledialog.askstring(title, prompt) or "").strip()

    def _vote(self, revoke: bool) -> None:
        row = self.selected()
        if not row:
            return
        word = "承認を取り消す" if revoke else "承認する"
        name = self._ask_name(word)
        if not name:
            return
        self.start(
            f"{row.model_id} を{word}({name})",
            lambda: action_manage(
                "unapprove" if revoke else "approve", self.store, row.model_id, name
            ),
        )

    def on_approve(self) -> None:
        self._vote(revoke=False)

    def on_unapprove(self) -> None:
        self._vote(revoke=True)

    def on_doctor(self) -> None:
        self.start("自己診断", action_doctor)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - 画面の起動
    import tkinter as tk

    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.stop(), root.destroy()))
    root.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
