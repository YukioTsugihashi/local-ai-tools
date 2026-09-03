r"""追加ツール(.localtool)の受入 — 検査から接続まで(追加と更新)。

使い方:
  ...python.exe tools\toolpack_install.py <パッケージ.localtool>          (検査だけ・既定)
  ...python.exe tools\toolpack_install.py <パッケージ.localtool> --apply  (配置・登録まで)
  ...python.exe tools\toolpack_install.py <パッケージ.localtool> --apply --update  (入れ替え)

無効化・有効化・削除・前の版へ戻すは `tools/toolpack_manage.py`。

設計の正本は TOOL_PACKAGE_DECISIONS.md、工程の定義は TOOL_PACKAGE_IMPLEMENTATION_PLAN.md。

段階:
  1 展開と形式検査(toolpack_verify)
  2 追加: 名前の衝突検査(既存の接続役・登録済みの追加ツール・Open WebUI)
    更新: 更新対象の確認(登録があるか・版が違うか。**同じ版で中身違いは拒否**)
  3 単体テスト(隔離ランナー内)
  4 スモーク(合成サンプル生成 → 実行 → 期待との照合。どちらも隔離ランナー内)
  5 配置(staging → installed)
  6 台帳へ登録(**常に未承認**。更新では版を足すだけで、まだ切り替えない)
  7 生成Pipeの作成
  8 更新のみ: 有効版の切り替え(**唯一の切り替え点**。ここまで旧版が動いたまま)
  9 Open WebUI への登録(差分表示 → --apply のときだけ反映)
 10 疎通確認

**更新の順序には理由がある(台帳 D-10)**: 先に台帳、後に Pipe。
逆にすると、新しい形式を受け付ける Pipe が古い版へ流れる。

途中で失敗したら、そこまでに変えたものを**逆順で全部戻す**。
ただし**更新の失敗では消さない**。消すと動いていた旧版まで無くなるため、
有効版を戻して旧版の Pipe を貼り直す。
成功したときだけ incoming と staging を消す。
失敗したパッケージの院内コピーは残さず、診断結果だけを rejected/ へ置く。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import toolpack_pipegen  # noqa: E402
import toolpack_runner  # noqa: E402
import toolpack_state  # noqa: E402
import toolpack_store  # noqa: E402
import toolpack_verify  # noqa: E402

TEST_ENTRY = ROOT / "tools" / "toolpack_test_entry.py"
# スモークとテストはツールを動かす。利用者のジョブと同じ順番待ちに並ぶ(台帳 D-9)
LOCK_WAIT_SECONDS = 1800

STAGE_VERIFY = "パッケージ形式"
STAGE_NAME = "名前の衝突"
STAGE_TARGET = "更新対象の確認"
STAGE_SWITCH = "有効版の切り替え"
STAGE_TESTS = "単体テスト"
STAGE_SMOKE = "サンプル実行"
STAGE_PLACE = "配置"
STAGE_REGISTER = "台帳へ登録"
STAGE_PIPE = "Pipe生成"
STAGE_DEPLOY = "Open WebUIへ登録"
STAGE_REACH = "疎通確認"


class InstallError(Exception):
    """どの段階で・何が・どう直すかを持つ失敗。"""

    def __init__(self, stage: str, reason: str, fix: str = "") -> None:
        super().__init__(f"[{stage}] {reason}")
        self.stage = stage
        self.reason = reason
        self.fix = fix


@dataclass
class InstallReport:
    tool_id: str = ""
    version: str = ""
    display_name: str = ""
    applied: bool = False
    ok: bool = False
    passed: list[str] = field(default_factory=list)
    failed_stage: str = ""
    reason: str = ""
    fix: str = ""
    diagnosis_path: Path | None = None
    examples: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    update: bool = False
    previous_version: str = ""


def _package_hash(localtool: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(localtool, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _existing_names() -> set[str]:
    """既存の接続役と、登録済みの追加ツールの名前。"""
    try:
        from local_tool_bridge import hub

        return set(hub.discover()[0])
    except Exception:
        return set()


def _openwebui_has_id(tool_id: str) -> bool | None:
    """Open WebUI に同じIDの登録があるか。確かめられなければ None。

    ここを見ないと、**管理アプリの外で登録された同名Pipeを「更新」してしまう**
    (deploy は id が既にあれば更新として動く)。追加ツールは常に新規なので、
    既にあるなら拒否する(台帳: 追加に失敗しても既存を壊さない・管理外は触らない)。

    **Function と モデル設定の両方**を見る。Functionだけ消えて
    モデル設定が残っている状態があり、その場合も既存を更新してしまうため。
    """
    try:
        import open_webui_deploy as deploy

        client = deploy.ApiClient(
            base_url=deploy.resolve_base_url(), api_key=deploy.load_api_key()
        )
        if any(entry.get("id") == tool_id for entry in client.list_entries("pipe")):
            return True
        return client.get_model(tool_id) is not None
    except Exception:
        return None


def _run_stage_tool(package_dir: Path, tool_json: dict, entry: str, *,
                    inputs, instruction: str, job_root: Path, config: dict | None = None,
                    produces: list[str] | None = None):
    """入口を差し替えて隔離ランナーで1回動かす(テスト・サンプル生成・本体で共用)。"""
    variant = dict(tool_json)
    variant["entry"] = {"script": entry}
    if produces is not None:
        variant["outputs"] = {"produces": produces, "may_be_empty": True}
    return toolpack_runner.run_tool_package(
        package_dir, variant, inputs=inputs, instruction=instruction,
        config=config, job_root=job_root,
    )


def _stage_tests(package_dir: Path, tool_json: dict, work: Path) -> None:
    tests_dir = package_dir / "tests"
    job_root = work / "tests_job"
    job_root.mkdir(parents=True, exist_ok=True)
    # コア側の入口を持ち込む(パッケージからは差し替えられない場所へ置く)
    entry = job_root / "_toolpack_test_entry.py"
    shutil.copy2(TEST_ENTRY, entry)

    report = _run_stage_tool(
        package_dir, tool_json, str(entry),
        inputs=[], instruction="単体テスト", job_root=job_root,
        config={"tests_dir": str(tests_dir)}, produces=[],
    )
    if not report.ok:
        raise InstallError(
            STAGE_TESTS, f"テストを実行できませんでした(コード: {report.code})",
            "パッケージの tests/ が隔離実行のもとで動くか確かめてください",
        )
    if report.result.status != "ok":
        raise InstallError(
            STAGE_TESTS, report.result.message,
            "テストが通るように直してから持ち込み直してください",
        )


def _stage_smoke(package_dir: Path, tool_json: dict, work: Path) -> None:
    smoke = tool_json.get("smoke") or {}
    make_sample = smoke.get("make_sample")
    request_file = smoke.get("request")
    expect = smoke.get("expect") or {}

    # (1) 合成サンプルの生成。生成器も同じ隔離下で動かす。
    # 生成されるのは**本体への入力**なので、許す拡張子は outputs.produces ではなく
    # inputs.accepts を使う(ここを取り違えると、正しい生成器が拡張子違いで落ちる)
    sample_job = work / "sample_job"
    sample_job.mkdir(parents=True, exist_ok=True)
    accepts = list((tool_json.get("inputs") or {}).get("accepts") or [])
    sample_report = _run_stage_tool(
        package_dir, tool_json, str(make_sample),
        inputs=[], instruction="サンプル生成", job_root=sample_job, produces=accepts,
    )
    if not sample_report.ok:
        raise InstallError(
            STAGE_SMOKE, f"合成サンプルを作れませんでした(コード: {sample_report.code})",
            f"{make_sample} は規定のJSON1行(status/message/files)を返し、"
            "生成したファイルを files へ載せてください",
        )
    samples = list(sample_report.result.files)
    if not samples:
        raise InstallError(
            STAGE_SMOKE, "合成サンプルが1件も作られませんでした",
            f"{make_sample} が生成ファイルを files へ載せているか確かめてください",
        )

    # (2) 本体をサンプルで動かす
    instruction = "サンプル実行"
    try:
        request_json = json.loads((package_dir / request_file).read_text(encoding="utf-8"))
        instruction = str(request_json.get("instruction") or instruction)
    except Exception:
        pass  # 読めなければ既定の依頼文で動かす(検証器が形式は見ている)

    main_job = work / "smoke_job"
    main_job.mkdir(parents=True, exist_ok=True)
    inputs = [
        (f"in-{index}", None, path, path.name)
        for index, path in enumerate(samples, start=1)
    ]
    report = _run_stage_tool(
        package_dir, tool_json, tool_json.get("entry", {}).get("script", "main.py"),
        inputs=inputs, instruction=instruction, job_root=main_job,
    )
    if not report.ok:
        raise InstallError(
            STAGE_SMOKE, f"サンプル実行に失敗しました(コード: {report.code})",
            "隔離実行のもとで動くか、禁止された操作をしていないか確かめてください",
        )
    result = report.result
    if result.status != expect.get("status", "ok"):
        raise InstallError(
            STAGE_SMOKE, f"期待した状態になりませんでした({result.status})", result.message,
        )
    files_min = int(expect.get("files_min", 0))
    if len(result.files) < files_min:
        raise InstallError(
            STAGE_SMOKE,
            f"成果物が足りません({len(result.files)}件 < 期待{files_min}件)",
            "tool.json の smoke.expect と実際の出力を合わせてください",
        )
    suffixes = [str(item).lower() for item in (expect.get("suffixes") or [])]
    if suffixes:
        produced = {path.suffix.lower() for path in result.files}
        missing = [suffix for suffix in suffixes if suffix not in produced]
        if missing:
            raise InstallError(
                STAGE_SMOKE, f"期待した種類の成果物がありません: {'、'.join(missing)}",
                "tool.json の smoke.expect.suffixes と実際の出力を合わせてください",
            )


def _stage_reachable(tool_id: str) -> None:
    """**稼働中のハブ(8010)** が認識したかをHTTPで確かめる。

    同じプロセスでコードを読み直しても、別プロセスで動いている本番のハブが
    見えているかの証拠にはならない。だから実際に問い合わせる。
    **確かめられなかったときは成功にしない。** 使えるかどうか分からないものを
    「接続しました」とは言えないため、失敗として扱って巻き戻す。
    """
    status = toolpack_state.hub_status()
    if status is None:
        raise InstallError(
            STAGE_REACH,
            "稼働中のハブへ問い合わせられませんでした",
            "ローカルサービスが動いているか確かめてから、もう一度取り込んでください",
        )

    names = {tool.get("name") for tool in status.get("tools", [])}
    if tool_id not in names:
        reasons = "、".join(status.get("errors") or []) or "理由は示されていません"
        raise InstallError(
            STAGE_REACH, f"稼働中のハブがツールを認識しませんでした({reasons})",
            "台帳とパッケージの中身が合っているか確かめてください",
        )


def _stage_update_target(store, tool_id: str, version: str, incoming: Path) -> str:
    """更新できる相手かを確かめ、**今動いている版**を返す(台帳 D-10)。

    同じ版で中身だけ違うものは拒否する。どちらが動いているか分からなくなるため。
    """
    entry = store.load_registry()["tools"].get(tool_id)
    if entry is None:
        raise InstallError(
            STAGE_TARGET, f"まだ入っていないツールです: {tool_id}",
            "更新ではなく追加として実行してください(--update を外す)",
        )
    # 承認済みの中身をこの経路で差し替えさせない(台帳 D-15)。
    # **画面のボタンを暗くするだけでは足りない。** ここでも断る
    if toolpack_store.is_approved(entry):
        raise InstallError(
            STAGE_TARGET, f"{tool_id} は承認済みです",
            "承認済みのツールは入れ替えられません。"
            "承認を外してからにしてください(承認の操作は企画課の担当です)",
        )
    known = entry.get("versions", {})
    if version in known:
        same = known[version].get("package_hash") == _package_hash(incoming)
        if same:
            raise InstallError(
                STAGE_TARGET, f"同じ版が既に入っています: {tool_id} {version}",
                "中身も同じです。入れ直す必要はありません",
            )
        raise InstallError(
            STAGE_TARGET, f"同じ版で中身が違います: {tool_id} {version}",
            "版を上げてから持ち込んでください。"
            "同じ版名で中身が変わると、どちらが動いているか分からなくなります",
        )
    return str(entry.get("active_version") or "")


def install(localtool: Path, *, apply: bool = False, update: bool = False,
            store: toolpack_store.ToolpackStore | None = None) -> InstallReport:
    """1本の .localtool を受け入れる。失敗したら変えたものを全部戻す。

    `update=True` は**既に入っているツールの入れ替え**(台帳 D-10)。
    追加とは別の操作にしてあり、取り違えると既存を壊すため自動では切り替えない。
    検査・単体テスト・サンプル実行は**追加とまったく同じもの**を通す。
    """
    store = store or toolpack_store.ToolpackStore()
    store.ensure_layout()
    report = InstallReport(applied=apply, update=update)
    localtool = Path(localtool)

    if not apply:
        # 検査だけなら台帳を変えない。ロックを取って他の操作を待たせない
        return _install_locked(localtool, apply, update, store, report)
    try:
        with toolpack_store.admin_lock():
            return _install_locked(localtool, apply, update, store, report)
    except toolpack_store.AdminBusy as error:
        report.failed_stage = "他の管理操作"
        report.reason = str(error)
        report.fix = "管理画面を2つ開いていないか確かめて、やり直してください"
        return report


def _install_locked(localtool: Path, apply: bool, update: bool, store,
                    report: InstallReport) -> InstallReport:
    """install の中身。**台帳を変える経路はプロセス間ロックの中で動く**(D-10)。"""

    # 持ち込みの記録として incoming へ複製してから扱う(原本はUSB側に残る)
    incoming = localtool
    if localtool.parent.resolve() != store.incoming.resolve():
        incoming = store.incoming / localtool.name
        shutil.copy2(localtool, incoming)

    staging = store.new_staging_dir()
    work = staging / "_work"
    work.mkdir()
    package_dir = staging / toolpack_store.PACKAGE_DIR

    placed = registered = deploy_attempted = switched = False
    tool_id = version = ""

    try:
        # 1 形式検査
        verified = toolpack_verify.verify_package(incoming, package_dir)
        if not verified.ok:
            raise InstallError(
                STAGE_VERIFY,
                f"検査に通りませんでした({len(verified.errors)}件)",
                "\n".join(str(error) for error in verified.errors[:10]),
            )
        tool_json = verified.tool_json or {}
        tool_id = str(tool_json.get("id"))
        version = str(tool_json.get("version"))
        report.tool_id, report.version = tool_id, version
        report.display_name = (
            toolpack_pipegen.UNAPPROVED_PREFIX + str(tool_json.get("display_name") or tool_id)
        )
        report.examples = [str(x) for x in ((tool_json.get("routing") or {}).get("examples") or [])]
        report.passed.append(STAGE_VERIFY)

        # 2 追加なら「名前の衝突」、更新なら「更新対象の確認」
        if update:
            report.previous_version = _stage_update_target(store, tool_id, version, incoming)
            report.passed.append(STAGE_TARGET)
        else:
            # 登録前に見る。ハブ・台帳・Open WebUI の3か所
            if tool_id in _existing_names():
                raise InstallError(
                    STAGE_NAME, f"同じ名前がすでに使われています: {tool_id}",
                    "既存のツールは残します。入れ替えるなら --update を付けてください。"
                    "別のツールとして入れるなら id を変えてください",
                )
            if apply:
                occupied = _openwebui_has_id(tool_id)
                if occupied is None:
                    raise InstallError(
                        STAGE_NAME, "Open WebUI の登録一覧を確認できませんでした",
                        "同じIDの登録を上書きしないため、確認できるまで取り込みません。"
                        "Open WebUI の稼働と管理APIキーを確かめてください",
                    )
                if occupied:
                    raise InstallError(
                        STAGE_NAME, f"Open WebUI に同じIDの登録があります: {tool_id}",
                        "既存の登録は触りません(更新ではなく追加のため)。"
                        "入れ替えるなら --update を付けてください",
                    )
            report.passed.append(STAGE_NAME)

        # 3・4 テストとサンプル実行(利用者のジョブと順番待ちを共有する)
        from local_tool_bridge.job_lock import JobLock

        lock = JobLock()
        if lock.is_busy():
            print("現在の処理が終わるのを待っています...")
        if not lock.acquire(timeout=LOCK_WAIT_SECONDS):
            raise InstallError(
                STAGE_TESTS, "他の処理が終わらないため実行できませんでした",
                "しばらく待ってからやり直してください",
            )
        try:
            _stage_tests(package_dir, tool_json, work)
            report.passed.append(STAGE_TESTS)
            _stage_smoke(package_dir, tool_json, work)
            report.passed.append(STAGE_SMOKE)
        finally:
            lock.release()
            lock.close()

        if not apply:
            # 検査だけでも後始末はする(持ち込みの複製と展開物を残さない)
            store.finish_success(incoming, staging)
            report.ok = True
            report.warnings.append(
                "検査だけを行いました。取り込むには --apply を付けて実行してください"
            )
            return report

        # 5 配置(検査に使った作業領域は持ち込まない)
        shutil.rmtree(work, ignore_errors=True)
        store.promote(staging, tool_id, version)
        placed = True
        store.install_json_path(tool_id, version).write_text(
            json.dumps({
                "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "package_hash": _package_hash(incoming),
                "source_name": localtool.name,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        source_path = store.source_path(tool_id, version)
        source_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(incoming, source_path)  # 原本を版ごとに保存(再検査・戻し用)
        report.passed.append(STAGE_PLACE)

        # 6 台帳へ登録(常に未承認)。
        # **更新では版を足すだけで、まだ切り替えない。** 旧版が動いたまま用意し終える
        if update:
            store.add_version(tool_id, version, package_hash=_package_hash(incoming))
        else:
            store.register(tool_id, version, package_hash=_package_hash(incoming))
        registered = True
        report.passed.append(STAGE_REGISTER)

        # 7 Pipe生成
        generated = toolpack_pipegen.generate(
            store.package_dir(tool_id, version), store.generated_dir(tool_id, version),
            approved=False,
        )
        report.passed.append(STAGE_PIPE)

        # 8 有効版の切り替え(更新のときだけ)。**ここが唯一の切り替え点**。
        # 先に台帳を切り替えてから Pipe を貼る。逆にすると、新しい形式を受け付ける
        # Pipe が古い版へ流れてしまう(台帳 D-10 推奨3)
        if update:
            store.set_active_version(tool_id, version)
            switched = True
            report.passed.append(STAGE_SWITCH)

        # 9 Open WebUI へ登録
        import open_webui_deploy as deploy

        client = deploy.ApiClient(
            base_url=deploy.resolve_base_url(), api_key=deploy.load_api_key()
        )
        # 「試みた」時点で取り消し対象にする。登録処理の途中で落ちても
        # Pipeだけ作られて残る、という状態を作らないため
        deploy_attempted = True
        if deploy.command_deploy(client, generated, None, True) != 0:
            raise InstallError(
                STAGE_DEPLOY, "Open WebUI へ登録できませんでした",
                "表示された差分と理由を確認してください",
            )
        report.passed.append(STAGE_DEPLOY)

        # 10 疎通確認(通らなければ取り込みを取り消す)
        _stage_reachable(tool_id)
        report.passed.append(STAGE_REACH)

        store.finish_success(incoming, None)
        report.ok = True
        return report

    except InstallError as error:
        report.warnings.extend(
            _rollback(store, report, deploy_attempted, registered, placed, switched)
        )
        report.failed_stage, report.reason, report.fix = error.stage, error.reason, error.fix
        report.diagnosis_path = store.reject(
            incoming, staging,
            tool_hint=tool_id or localtool.stem,
            diagnosis=_diagnosis(report),
        )
        return report
    except Exception as error:  # 想定外も同じ扱いで戻す
        report.warnings.extend(
            _rollback(store, report, deploy_attempted, registered, placed, switched)
        )
        report.failed_stage = "想定外"
        report.reason = f"{type(error).__name__}: {error}"
        report.diagnosis_path = store.reject(
            incoming, staging, tool_hint=tool_id or localtool.stem,
            diagnosis=_diagnosis(report),
        )
        return report


def _rollback(store, report: InstallReport, deploy_attempted: bool,
              registered: bool, placed: bool, switched: bool = False) -> list[str]:
    """変えたものを逆順で戻し、**戻しきれなかったもの**を返す(台帳 第6章)。

    Open WebUI への登録は「成功したとき」ではなく **「試みたとき」** に取り消す。
    登録処理の途中(Pipeは作られたがモデル設定で失敗した等)で落ちても
    残骸が残らないようにするためである。
    削除は必ず再取得で確かめ、**消せなかったことを握りつぶさない**。

    **更新の失敗では消さない。** 消すと、動いていた旧版まで無くなる。
    戻すのは「有効版を旧版へ戻し、旧版のPipeを貼り直し、新版の登録と置き場を外す」だけ。
    """
    if report.update:
        return _rollback_update(store, report, deploy_attempted, registered, placed, switched)

    tool_id, version = report.tool_id, report.version
    problems: list[str] = []
    if deploy_attempted and tool_id:
        try:
            import open_webui_deploy as deploy

            client = deploy.ApiClient(
                base_url=deploy.resolve_base_url(), api_key=deploy.load_api_key()
            )
            removed = client.remove_pipe_completely(tool_id)
            remaining = [name for name, gone in removed.items() if not gone]
            if remaining:
                problems.append(
                    f"Open WebUI から消せませんでした({'、'.join(remaining)})。"
                    f"管理画面で {tool_id} を手動で削除してください"
                )
        except Exception as error:
            problems.append(
                f"Open WebUI の取り消しに失敗しました({type(error).__name__})。"
                f"管理画面で {tool_id} が残っていないか確認してください"
            )
    if registered and tool_id:
        try:
            store.unregister(tool_id)
        except Exception as error:
            problems.append(f"台帳から外せませんでした({type(error).__name__})")
    if placed and tool_id and version:
        target = store.version_dir(tool_id, version)
        shutil.rmtree(target, ignore_errors=True)
        if target.exists():
            problems.append(f"配置したファイルを消せませんでした: {target}")
    return problems


def _rollback_update(store, report: InstallReport, deploy_attempted: bool,
                     registered: bool, placed: bool, switched: bool) -> list[str]:
    """更新の失敗を戻す(台帳 D-10)。**動いていた旧版を消さない。**

    順序は行った操作の逆:切り替えを戻す → 旧版のPipeを貼り直す →
    新版の登録を外す → 新版の置き場を消す。
    """
    tool_id, version = report.tool_id, report.version
    previous = report.previous_version
    problems: list[str] = []

    if switched and tool_id and previous:
        try:
            store.set_active_version(tool_id, previous)
        except Exception as error:
            problems.append(
                f"有効版を {previous} へ戻せませんでした({type(error).__name__})。"
                f"registry.json の {tool_id} を確認してください"
            )
    # Pipe を貼り替えた可能性がある。旧版の generated/ から貼り直す。
    # **消さずに上書きで戻す**(消すと動いていたツールが無くなる)
    if deploy_attempted and tool_id and previous:
        pipe = store.generated_dir(tool_id, previous) / toolpack_pipegen.pipe_filename(tool_id)
        if not pipe.is_file():
            problems.append(
                f"前の版のPipeが見つかりません: {pipe}。"
                f"Open WebUI の {tool_id} が新しい版のままになっている可能性があります"
            )
        else:
            try:
                import open_webui_deploy as deploy

                client = deploy.ApiClient(
                    base_url=deploy.resolve_base_url(), api_key=deploy.load_api_key()
                )
                if deploy.command_deploy(client, pipe, None, True) != 0:
                    problems.append(
                        f"前の版のPipeを貼り直せませんでした({tool_id})。"
                        "Open WebUI の登録内容を確認してください"
                    )
            except Exception as error:
                problems.append(
                    f"前の版のPipeの貼り直しに失敗しました({type(error).__name__})。"
                    f"Open WebUI の {tool_id} を確認してください"
                )
    if registered and tool_id and version:
        try:
            store.drop_version(tool_id, version)
        except Exception as error:
            problems.append(f"新しい版を台帳から外せませんでした({type(error).__name__})")
    if placed and tool_id and version:
        target = store.version_dir(tool_id, version)
        shutil.rmtree(target, ignore_errors=True)
        if target.exists():
            problems.append(f"配置したファイルを消せませんでした: {target}")
    return problems


def _diagnosis(report: InstallReport) -> str:
    """rejected/ へ残す診断。**本文・氏名・元ファイル名は書かない**(台帳 D-9)。"""
    lines = [
        f"日時: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
        f"ツール: {report.tool_id or '(不明)'} {report.version}",
        f"通過した段階: {'、'.join(report.passed) or 'なし'}",
        f"失敗した段階: {report.failed_stage}",
        f"理由: {report.reason}",
        f"巻き戻し: {'完了' if not report.warnings else '未完了'}",
    ]
    # 戻しきれなかったものは必ず記録へ残す(表示だけで消えないように)
    lines.extend(f"戻せなかったもの: {warning}" for warning in report.warnings)
    if report.fix:
        lines.append("対処:\n" + report.fix)
    return "\n".join(lines) + "\n"


def format_report(report: InstallReport) -> str:
    lines: list[str] = []
    for stage in report.passed:
        lines.append(f"  ✓ {stage}")
    if report.ok and report.applied and report.update:
        lines.append("")
        lines.append(f"「{report.display_name}」を {report.version} へ更新しました。")
        if report.previous_version:
            lines.append(
                f"前の版 {report.previous_version} は残してあります"
                "(問題があれば戻せます)。"
            )
        if report.examples:
            lines.append("")
            lines.append("依頼の例:")
            lines.extend(f"  - {example}" for example in report.examples)
        lines.append("")
        lines.append("※ 承認は版ごとです。この版は【未承認】から始まります。")
    elif report.ok and report.applied:
        lines.append("")
        lines.append(f"「{report.display_name}」を追加しました。")
        lines.append("Open WebUI のモデル一覧から選んで使えます。")
        if report.examples:
            lines.append("")
            lines.append("依頼の例:")
            lines.extend(f"  - {example}" for example in report.examples)
        lines.append("")
        lines.append("※ この追加ツールは【未承認】です。")
        lines.append("  正式なモデルにするには企画課の承認が要ります。")
        lines.append("※ 実行は隔離されていますが、悪意をもって作られたコードまでは")
        lines.append("  防ぎきれません。持ち込み元が確かなものだけを追加してください。")
    elif report.ok:
        lines.append("")
        lines.append("検査に通りました(まだ取り込んでいません)。")
        lines.append("取り込むには --apply を付けて実行してください。")
    else:
        lines.append(f"  × {report.failed_stage}")
        lines.append("")
        lines.append("ツールを追加できませんでした。")
        lines.append(f"失敗した段階: {report.failed_stage}")
        lines.append(f"原因: {report.reason}")
        if report.fix:
            lines.append(f"対処:\n{report.fix}")
        # 戻しきれていないのに「戻しました」と言わない
        if report.warnings:
            lines.append("**完全には戻せませんでした。** 次を手で確認してください:")
            lines.extend(f"  - {warning}" for warning in report.warnings)
        else:
            lines.append("既存の環境は変更前の状態へ戻しました。")
        if report.diagnosis_path:
            lines.append(f"詳細: {report.diagnosis_path}")
    if report.ok:
        for warning in report.warnings:
            lines.append(f"注意: {warning}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="追加ツール(.localtool)を受け入れる")
    parser.add_argument("package", help="持ち込んだ .localtool のパス")
    parser.add_argument(
        "--apply", action="store_true",
        help="検査だけでなく、配置・登録まで行う(無指定は検査のみ)",
    )
    parser.add_argument(
        "--update", action="store_true",
        help="既に入っているツールを入れ替える(追加とは別の操作。版を上げておくこと)",
    )
    args = parser.parse_args(argv)
    report = install(Path(args.package), apply=args.apply, update=args.update)
    print(format_report(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
