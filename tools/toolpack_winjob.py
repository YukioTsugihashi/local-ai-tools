r"""Windows ジョブオブジェクトによる追加ツールプロセスの資源制限(ctypes)。

設計の正本は TOOL_PACKAGE_DECISIONS.md(D-9)。pywin32 は wheelhouse に無いため
標準ライブラリの ctypes だけで実装する(台帳 5-13)。

- KILL_ON_JOB_CLOSE: 親がジョブハンドルを閉じると、子プロセスツリー全体が確実に死ぬ
- メモリ上限・アクティブプロセス数上限

監査フック(toolpack_child)が主防御で、これはリソース暴走と後始末の保証を足すもの。
ジョブ作成に失敗しても実行自体は続けられるよう、呼び出し側はNoneのJobHandleを扱える。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

# 2GB/ジョブ・アクティブプロセス1(台帳 D-9)
DEFAULT_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_ACTIVE_PROCESSES = 1

_JobObjectExtendedLimitInformation = 9
_LIMIT_ACTIVE_PROCESS = 0x00000008
_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_LIMIT_PROCESS_MEMORY = 0x00000100


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.POINTER(wintypes.ULONG)),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JobHandle:
    """ジョブハンドルの薄いラッパ。close() で子プロセスツリーを全滅させる。"""

    def __init__(self, handle: int) -> None:
        self._handle = handle
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def assign(self, process_handle: int) -> None:
        if not self._kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(self._handle), wintypes.HANDLE(process_handle)
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        """ハンドルを閉じる。KILL_ON_JOB_CLOSE によりジョブ内の全プロセスが終了する。"""
        if self._handle:
            self._kernel32.CloseHandle(wintypes.HANDLE(self._handle))
            self._handle = 0


def create_job(
    memory_bytes: int = DEFAULT_MEMORY_BYTES,
    active_processes: int = DEFAULT_ACTIVE_PROCESSES,
) -> JobHandle:
    """制限付きジョブオブジェクトを作る。Windows以外・失敗時は OSError。"""
    if sys.platform != "win32":  # pragma: no cover - この環境はWindows固定
        raise OSError("ジョブオブジェクトはWindowsでのみ使えます")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())

    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = (
        _LIMIT_KILL_ON_JOB_CLOSE | _LIMIT_ACTIVE_PROCESS | _LIMIT_PROCESS_MEMORY
    )
    info.BasicLimitInformation.ActiveProcessLimit = active_processes
    info.ProcessMemoryLimit = memory_bytes

    if not kernel32.SetInformationJobObject(
        wintypes.HANDLE(handle),
        _JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(wintypes.HANDLE(handle))
        raise ctypes.WinError(error)
    return JobHandle(handle)
