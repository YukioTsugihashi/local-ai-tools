r"""追加ツールを隔離して実行する親ランナー(第一段階)。

設計の正本は TOOL_PACKAGE_DECISIONS.md(D-9)。

役割:
- 最小環境変数を新規に作って渡す(鍵・USERPROFILE 系を渡さない)
- ジョブディレクトリを組み、request.json を書く(契約は toolpack_contract)
- 子プロセスのエントリを **コアのGit管理下 toolpack_child.py に固定**して起動する
  (パッケージ側から実行入口を差し替えさせない)
- Windowsジョブオブジェクトで資源制限と後始末を保証する
- 標準出力(JSON1行)を契約で検証して返す

隔離は2段構え(台帳 D-9)。既定は本番前提の `low`:

- `basic`(段階1・切戻し用): 監査フック + 最小env + ジョブオブジェクト
- `low`(段階2・**本番運用の前提**): 上に加えて、制限トークン + 低整合性レベルにより
  **outdir 外への書き込みをOSが強制的に拒否する**

【限界(隠さないこと)】低整合性が縛るのは**書き込み**であって読み取りではない。
読み取りの制限は監査フック(Pythonレベルのみ)に依存しており、
ctypes 直呼びやネイティブ拡張の内部I/Oによる**読み取り**は防げない。
管理APIキーファイルの読み取り遮断もこの依存の下にある。
成功表示や診断でこの限界を隠さないこと(台帳 D-9)。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import toolpack_contract as contract

ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = ROOT / "text-processing-bridge" / ".venv" / "Scripts" / "python.exe"
CHILD_SCRIPT = ROOT / "tools" / "toolpack_child.py"
# パッケージが添付ファイルを文字として読むための共通部品(D-5)。
# ジョブ直下へ複製し、隔離の中で import させる
TEXTIO_MODULE = ROOT / "tools" / "toolpack_textio.py"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def resolve_interpreter(venv_python: Path) -> tuple[Path, Path | None]:
    """実行に使う「単一プロセスの」インタプリタと、追加すべき site-packages を返す。

    venv の Scripts\\python.exe はベースインタプリタを子プロセスとして起動し直す
    リダイレクタで、ジョブオブジェクトの ActiveProcessLimit=1 に引っかかる
    (2026-08-31 に実測。台帳へ積む論点)。そこで pyvenv.cfg からベース実行体を取り、
    venv の site-packages を PYTHONPATH で足すことで、1プロセスで venv 相当の依存を使う。
    """
    venv_python = Path(venv_python)
    cfg = venv_python.resolve().parent.parent / "pyvenv.cfg"
    if cfg.is_file():
        values: dict[str, str] = {}
        for line in cfg.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
        base = values.get("executable")
        if not base and values.get("home"):
            base = str(Path(values["home"]) / "python.exe")
        site = venv_python.resolve().parent.parent / "Lib" / "site-packages"
        if base and Path(base).is_file():
            return Path(base), (site if site.is_dir() else None)
    return venv_python, None

# 親のenvから決して子へ渡さない接頭辞・名前(鍵・資格情報の遮断)
_ENV_DENY_SUBSTRINGS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


@dataclass
class RunReport:
    """実行結果。ok なら result が入る。失敗時は code と detail。

    detail は開発者向け(本文・氏名を含みうるため運用ログへそのまま保存しない。台帳 D-9)。
    """

    ok: bool
    code: str = ""
    result: contract.ToolResult | None = None
    detail: str = ""
    returncode: int | None = None
    duration_seconds: float = 0.0


class RunnerError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# 隔離方式。"low" が本番の既定(台帳 D-9 の段階2)。"basic" は段階1相当の切戻し用で、
# **失敗しても黙って basic へ降格しない**(降格を隠すと「失敗を成功として扱う」ことになる)
ISOLATION_LOW = "low"
ISOLATION_BASIC = "basic"


def _run_basic(argv, job_dir: Path, env: dict, timeout: int, job) -> tuple[int, str, str]:
    """段階1: 通常起動 + 起動直後にジョブ割り当て。標準出力はパイプで受ける。"""
    try:
        proc = subprocess.Popen(
            argv, cwd=str(job_dir), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=_NO_WINDOW,
        )
    except OSError as error:
        raise RunnerError("E-SPAWN", f"子プロセスを起動できません: {error}") from error
    if job is not None:
        try:
            job.assign(int(proc._handle))
        except Exception as error:
            # 割り当てに失敗したら資源制限が外れる。走らせたままにしない
            proc.kill()
            proc.communicate()
            raise RunnerError(
                "E-ISOLATION", f"資源制限を適用できません: {error}"
            ) from error
    try:
        out_bytes, err_bytes = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise TimeoutError("制限時間を超えました")
    return (
        proc.returncode,
        out_bytes.decode("utf-8", errors="replace"),
        err_bytes.decode("utf-8", errors="replace"),
    )


def _run_low(argv, job_dir: Path, dirs: dict, env: dict, timeout: int, job) -> tuple[int, str, str]:
    """段階2: 書込可能領域だけへ低整合性ラベルを付け、低整合性トークンで起動する。

    標準出力・標準エラーの記録先は **ジョブ直下**(書込許可の外)に置く。
    書込可能な tmp/ へ置くと、ツール自身が結果を書き換えて成功を偽装できてしまう。
    親(中整合性)が開いたハンドルを継承させるので、子は書けるが開き直せない。
    """
    import toolpack_winsec

    try:
        for name in ("out", "tmp", "mpl"):
            toolpack_winsec.set_low_integrity(dirs[name])
    except toolpack_winsec.IsolationError as error:
        raise RunnerError("E-ISOLATION", str(error)) from error

    stdout_path = job_dir / "stdout.log"
    stderr_path = job_dir / "stderr.log"
    try:
        returncode = toolpack_winsec.run_low_integrity(
            argv, cwd=job_dir, env=env,
            stdout_path=stdout_path, stderr_path=stderr_path,
            timeout=timeout, job=job,
        )
    except toolpack_winsec.IsolationError as error:
        raise RunnerError("E-ISOLATION", str(error)) from error
    return (
        returncode,
        stdout_path.read_text(encoding="utf-8", errors="replace"),
        stderr_path.read_text(encoding="utf-8", errors="replace"),
    )


def build_min_env(tmp_dir: Path, mpl_dir: Path, pythonpath: Path | None = None) -> dict[str, str]:
    """最小の環境変数を新規に作る。鍵・トークン・ユーザープロファイルは渡さない。"""
    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    env = {
        "SYSTEMROOT": system_root,
        "PATH": os.path.join(system_root, "System32"),
        "TEMP": str(tmp_dir),
        "TMP": str(tmp_dir),
        "MPLCONFIGDIR": str(mpl_dir),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    if pythonpath is not None:
        env["PYTHONPATH"] = str(pythonpath)  # venv の依存を1プロセスで使うため
    # 念のため、名前に鍵・資格情報らしき語を含む変数は一切混ぜない
    return {k: v for k, v in env.items() if not any(d in k.upper() for d in _ENV_DENY_SUBSTRINGS)}


def run_tool_package(
    package_dir: Path,
    tool_json: dict,
    inputs: list[tuple[str, str | None, Path, str]],
    instruction: str,
    config: dict | None = None,
    *,
    python: Path = VENV_PYTHON,
    job_root: Path | None = None,
    isolation: str = ISOLATION_LOW,
) -> RunReport:
    """1回のツール実行。ジョブディレクトリを作り、隔離した子プロセスで走らせる。"""
    package_dir = Path(package_dir).resolve()
    main_name = (
        tool_json.get("entry", {}).get("script")
        if isinstance(tool_json.get("entry"), dict)
        else None
    ) or "main.py"
    if os.path.isabs(main_name):
        # 差し替えた入口(受入検査のテスト入口など)も長い形へ揃える。job_dir と
        # 同じ理由で、短縮名のままだと読み取り許可の判定に通らない
        main_name = str(Path(main_name).resolve())
    timeout = int(tool_json.get("timeout_seconds", 300))
    ollama = bool(
        isinstance(tool_json.get("permissions"), dict)
        and tool_json["permissions"].get("ollama") is True
    )
    produces = []
    if isinstance(tool_json.get("outputs"), dict):
        produces = tool_json["outputs"].get("produces") or []
    allowed_suffixes = tuple(s.lower() for s in produces)

    # 必ず長い形へ揃える。Windowsの 8.3 短縮名(C:\Users\KELCRE~1\...)のまま渡すと、
    # 子が Path.resolve() した長い形と文字列が一致せず、**許可した領域への書き込みが
    # 拒否される**。pytest は basetemp を resolve するため、これで tmp_path を使う
    # 単体テストが全部落ちていた(2026-08-31)
    job_dir = Path(job_root) if job_root else Path(tempfile.mkdtemp(prefix="toolpack_job_"))
    job_dir = job_dir.resolve()
    dirs = contract.prepare_job_dirs(job_dir)
    request_path = contract.build_request(job_dir, instruction, inputs, config)

    # 読み取り部品をジョブ直下へ置く。ジョブ直下は読めるが**書けない**ので、
    # ツール側から差し替えられない。パッケージは `import toolpack_textio` で使う
    shutil.copy2(TEXTIO_MODULE, job_dir / TEXTIO_MODULE.name)

    interpreter, extra_site = resolve_interpreter(python)
    env = build_min_env(dirs["tmp"], dirs["mpl"], pythonpath=extra_site)
    argv = [
        str(interpreter), str(CHILD_SCRIPT),
        "--package", str(package_dir),
        "--job", str(job_dir),
        "--main", main_name,
        "--request", str(request_path),
        "--ollama", "1" if ollama else "0",
    ]

    started = time.monotonic()
    # 資源制限(メモリ2GB・プロセス1・子ツリー全滅)は台帳 D-9 の確定事項。
    # 作れないまま実行すると、確定した制限が黙って外れる。**安全側に倒して拒否する**
    try:
        import toolpack_winjob

        job = toolpack_winjob.create_job()
    except Exception as error:
        raise RunnerError(
            "E-ISOLATION",
            f"資源制限(ジョブオブジェクト)を用意できません: {error}",
        ) from error

    try:
        if isolation == ISOLATION_LOW:
            returncode, stdout_text, stderr_text = _run_low(
                argv, job_dir, dirs, env, timeout, job
            )
        elif isolation == ISOLATION_BASIC:
            returncode, stdout_text, stderr_text = _run_basic(
                argv, job_dir, env, timeout, job
            )
        else:
            raise RunnerError("E-ISOLATION", f"未知の隔離方式です: {isolation!r}")
    except TimeoutError:
        return RunReport(
            ok=False, code="E-TIMEOUT",
            detail=f"制限時間 {timeout}秒 を超えました",
            duration_seconds=time.monotonic() - started,
        )
    finally:
        if job is not None:
            job.close()  # KILL_ON_JOB_CLOSE で子ツリー全滅

    duration = time.monotonic() - started
    stderr_tail = stderr_text[-4000:]

    if returncode != 0:
        # PermissionError 等はここに来る。生 stderr は detail(開発者向け)だけに置き、
        # 運用ログへは呼び出し側がコードと分類だけを残す(台帳 D-9)
        code = "E-DENIED" if "PermissionError" in stderr_tail else "E-TOOL"
        return RunReport(
            ok=False, code=code, detail=stderr_tail,
            returncode=returncode, duration_seconds=duration,
        )

    try:
        result = contract.validate_result(stdout_text, dirs["out"], allowed_suffixes)
    except contract.ContractError as error:
        return RunReport(
            ok=False, code=str(error).split(":", 1)[0], detail=str(error),
            returncode=0, duration_seconds=duration,
        )
    return RunReport(ok=True, result=result, returncode=0, duration_seconds=duration)


# ---------------------------------------------------------------------------
# セルフチェック(doctor から定期実行。ランナーが効いているかを本番環境で確かめる)
# ---------------------------------------------------------------------------
_CANARY_MAIN = '''\
import argparse, sys
p = argparse.ArgumentParser()
p.add_argument("--request")
p.parse_args()
# 許可されていない場所への書き込みを試みる(ブロックされるべき)
try:
    open(r"{escape_target}", "w").write("x")
except PermissionError:
    print('{{"status":"ok","message":"blocked","files":[],"skipped":[],"notes":[]}}')
    sys.exit(0)
# ブロックされなければ、ガードが効いていない
sys.stderr.write("guard did not block escape write")
sys.exit(3)
'''


# ctypes で直接 CreateFileW を呼ぶ canary。監査フックは素通りするので、
# これが弾かれるかどうかが「低整合性(段階2)が実際に効いているか」の判定になる
_CANARY_CTYPES = '''\
import argparse, json, ctypes
from ctypes import wintypes
p = argparse.ArgumentParser(); p.add_argument("--request"); p.parse_args()
k = ctypes.WinDLL("kernel32", use_last_error=True)
k.CreateFileW.restype = wintypes.HANDLE
k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                          ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
handle = k.CreateFileW(r"{escape_target}", 0x40000000, 0, None, 2, 0x80, None)
invalid = ctypes.cast(ctypes.c_void_p(-1), ctypes.c_void_p).value
opened = handle is not None and handle != invalid
if opened:
    k.CloseHandle(wintypes.HANDLE(handle))
print(json.dumps({{"status": "ok", "message": "opened=%s" % opened,
                   "files": [], "skipped": [], "notes": []}}))
'''


def _run_canary(source: str, escape_target: Path, root: Path, python: Path, isolation: str):
    package = root / "pkg"
    package.mkdir(parents=True)
    (package / "main.py").write_text(
        source.format(escape_target=str(escape_target).replace("\\", "\\\\")),
        encoding="utf-8",
    )
    return run_tool_package(
        package,
        {"permissions": {"ollama": False},
         "outputs": {"produces": [], "may_be_empty": True},
         "timeout_seconds": 60},
        inputs=[], instruction="self-check",
        job_root=root / "job", python=python, isolation=isolation,
    )


def self_check(python: Path = VENV_PYTHON, isolation: str = ISOLATION_LOW) -> bool:
    """ガードが実際に効いているかを本番環境で確かめる。doctor から呼ぶ。

    1. Python経由の outdir 外書き込みが弾かれるか(監査フック)
    2. `low` のときは、**ctypes 直呼びの書き込みもOSに拒否されるか**
       (監査フックでは防げないため、低整合性が本当に効いているかの判定になる)

    どちらか一方でも通ってしまえば False。**通ったのに真を返さない。**
    """
    with tempfile.TemporaryDirectory(prefix="toolpack_selfcheck_") as tmp:
        tmp_path = Path(tmp)

        hook_target = tmp_path / "escape_hook.txt"
        hook_report = _run_canary(
            _CANARY_MAIN, hook_target, tmp_path / "hook", python, isolation
        )
        if not hook_report.ok or hook_target.exists():
            return False

        if isolation != ISOLATION_LOW:
            return True

        os_target = tmp_path / "escape_os.txt"
        os_report = _run_canary(
            _CANARY_CTYPES, os_target, tmp_path / "os", python, isolation
        )
        return (
            os_report.ok
            and os_report.result is not None
            and os_report.result.message == "opened=False"
            and not os_target.exists()
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="追加ツール専用ランナー")
    parser.add_argument("--self-check", action="store_true", help="ガードが効いているか確認する")
    args = parser.parse_args(argv)
    if args.self_check:
        ok = self_check()
        print(json.dumps({"self_check": "ok" if ok else "failed"}, ensure_ascii=False))
        return 0 if ok else 1
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
