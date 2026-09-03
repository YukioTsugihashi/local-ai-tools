r"""制限トークン + 低整合性レベルによる追加ツールの実行(工程4・ctypes)。

設計の正本は TOOL_PACKAGE_DECISIONS.md(D-9)。
**これは「将来の追加」ではなく、本番運用開始の前提である。**

第一段階(監査フック)はPythonレベルのI/Oしか見えない。ここでは Windows の
必須整合性制御(Mandatory Integrity Control)を使い、**outdir 外への書き込みを
OSに強制させる**。書き込み可能にしたいディレクトリだけへ低整合性ラベルを付け、
ツールプロセスを低整合性トークンで起動する。

【限界(隠さないこと)】低整合性が強制するのは**書き込み**であって読み取りではない。
低整合性プロセスは中整合性のファイルを読める(だから site-packages から import できる)。
読み取りの制限は引き続き監査フック(toolpack_child)に依存する。
"""

from __future__ import annotations

import ctypes
import msvcrt
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008
TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_ADJUST_DEFAULT = 0x0080
TOKEN_ADJUST_SESSIONID = 0x0100
_TOKEN_FOR_SPAWN = (
    TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ASSIGN_PRIMARY
    | TOKEN_ADJUST_DEFAULT | TOKEN_ADJUST_SESSIONID
)

_SecurityImpersonation = 2
_TokenPrimary = 1
_TokenIntegrityLevel = 25
_SE_GROUP_INTEGRITY = 0x00000020
LOW_INTEGRITY_SID = "S-1-16-4096"

CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_SUSPENDED = 0x00000004
STARTF_USESTDHANDLES = 0x00000100
HANDLE_FLAG_INHERIT = 0x00000001
WAIT_TIMEOUT = 0x00000102
INFINITE = 0xFFFFFFFF


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", _SID_AND_ATTRIBUTES)]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
    ]


# 64bit環境でハンドルが切り詰められないよう、必ずプロトタイプを宣言する
# (宣言を忘れると CreateProcessAsUserW が WinError 6 で失敗する。2026-08-31に実測)
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.GetCurrentProcess.argtypes = []
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
kernel32.ResumeThread.restype = wintypes.DWORD
kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
]
advapi32.DuplicateTokenEx.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
    ctypes.POINTER(wintypes.HANDLE),
]
advapi32.ConvertStringSidToSidW.argtypes = [
    wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)
]
advapi32.SetTokenInformation.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
]
advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
advapi32.GetLengthSid.restype = wintypes.DWORD
advapi32.CreateProcessAsUserW.argtypes = [
    wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
    wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
    ctypes.POINTER(_STARTUPINFOW), ctypes.POINTER(_PROCESS_INFORMATION),
]


class IsolationError(RuntimeError):
    """低整合性での起動に失敗した。**黙って第一段階へ降格しないこと。**"""


def create_low_integrity_token() -> wintypes.HANDLE:
    """自プロセスのトークンを複製し、整合性レベルを Low へ下げた一次トークンを返す。

    自分のトークンを弱めるだけなので、管理者昇格は要らない(台帳 5-13)。
    """
    if sys.platform != "win32":  # pragma: no cover - この環境はWindows固定
        raise IsolationError("低整合性実行はWindowsでのみ使えます")

    current = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), _TOKEN_FOR_SPAWN, ctypes.byref(current)
    ):
        raise IsolationError(f"OpenProcessToken に失敗しました: {ctypes.get_last_error()}")
    try:
        duplicated = wintypes.HANDLE()
        if not advapi32.DuplicateTokenEx(
            current, _TOKEN_FOR_SPAWN, None, _SecurityImpersonation,
            _TokenPrimary, ctypes.byref(duplicated),
        ):
            raise IsolationError(f"DuplicateTokenEx に失敗しました: {ctypes.get_last_error()}")
    finally:
        kernel32.CloseHandle(current)

    sid = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW(LOW_INTEGRITY_SID, ctypes.byref(sid)):
        kernel32.CloseHandle(duplicated)
        raise IsolationError("低整合性SIDを作れませんでした")

    label = _TOKEN_MANDATORY_LABEL()
    label.Label.Sid = sid
    label.Label.Attributes = _SE_GROUP_INTEGRITY
    if not advapi32.SetTokenInformation(
        duplicated, _TokenIntegrityLevel, ctypes.byref(label),
        ctypes.sizeof(label) + advapi32.GetLengthSid(sid),
    ):
        kernel32.CloseHandle(duplicated)
        raise IsolationError(f"整合性レベルを下げられませんでした: {ctypes.get_last_error()}")
    return duplicated


def set_low_integrity(path: Path) -> None:
    """ディレクトリへ低整合性ラベルを継承付きで設定する(ここだけが書込可能になる)。"""
    icacls = Path(
        __import__("os").environ.get("SYSTEMROOT", r"C:\Windows")
    ) / "System32" / "icacls.exe"
    # 出力は日本語(cp932)になりうるので、復号せずバイトのまま捨てる
    result = subprocess.run(
        [str(icacls), str(path), "/setintegritylevel", "(OI)(CI)low"],
        capture_output=True, check=False,
        creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        raise IsolationError(f"低整合性ラベルを付けられませんでした: {path}")


def _inheritable(handle: int) -> None:
    if not kernel32.SetHandleInformation(
        wintypes.HANDLE(handle), HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT
    ):
        raise IsolationError("標準出力ハンドルを継承可能にできませんでした")


def run_low_integrity(
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    stdout_path: Path,
    stderr_path: Path,
    timeout: float,
    job=None,
) -> int:
    """低整合性トークンで argv を実行し、終了コードを返す。

    標準出力・標準エラーはファイルへ落とす(パイプより単純で、低整合性でも詰まらない)。
    ジョブオブジェクトへは **CREATE_SUSPENDED のまま割り当ててから再開**するため、
    起動から割り当てまでの隙が無い(第一段階の Popen 方式より厳しい)。
    タイムアウト時は TimeoutError を投げる(後始末は呼び出し側がジョブを閉じて行う)。
    """
    token = create_low_integrity_token()
    out_file = open(stdout_path, "wb")
    err_file = open(stderr_path, "wb")
    try:
        out_handle = msvcrt.get_osfhandle(out_file.fileno())
        err_handle = msvcrt.get_osfhandle(err_file.fileno())
        _inheritable(out_handle)
        _inheritable(err_handle)

        startup = _STARTUPINFOW()
        startup.cb = ctypes.sizeof(startup)
        startup.dwFlags = STARTF_USESTDHANDLES
        startup.hStdInput = None
        startup.hStdOutput = wintypes.HANDLE(out_handle)
        startup.hStdError = wintypes.HANDLE(err_handle)

        # 環境ブロックは大文字小文字を無視した順に並べる(Windowsの慣例)
        block = "".join(
            f"{key}={value}\0" for key, value in sorted(env.items(), key=lambda kv: kv[0].upper())
        ) + "\0"
        env_buffer = ctypes.create_unicode_buffer(block)

        info = _PROCESS_INFORMATION()
        created = advapi32.CreateProcessAsUserW(
            token, None, ctypes.create_unicode_buffer(subprocess.list2cmdline(argv)),
            None, None, True,
            CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT | CREATE_SUSPENDED,
            env_buffer, str(cwd), ctypes.byref(startup), ctypes.byref(info),
        )
        if not created:
            raise IsolationError(
                f"低整合性プロセスを起動できませんでした: {ctypes.get_last_error()}"
            )
    finally:
        kernel32.CloseHandle(token)
        out_file.close()
        err_file.close()

    try:
        # 停止状態で作ってあるので、ここで失敗したら**必ず殺してから閉じる**。
        # 閉じるだけだと、停止したままのプロセスが残り続ける
        try:
            if job is not None:
                job.assign(int(info.hProcess))  # 再開前に割り当てる(隙を作らない)
            if kernel32.ResumeThread(info.hThread) == 0xFFFFFFFF:
                raise IsolationError("プロセスを再開できませんでした")
        except BaseException:
            kernel32.TerminateProcess(info.hProcess, 1)
            kernel32.WaitForSingleObject(info.hProcess, 5000)
            raise

        waited = kernel32.WaitForSingleObject(
            info.hProcess, int(timeout * 1000) if timeout else INFINITE
        )
        if waited == WAIT_TIMEOUT:
            kernel32.TerminateProcess(info.hProcess, 1)
            kernel32.WaitForSingleObject(info.hProcess, 5000)
            raise TimeoutError("制限時間を超えました")

        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        kernel32.CloseHandle(info.hThread)
        kernel32.CloseHandle(info.hProcess)
