"""
ハブの実行と管理アプリのスモークが共有するプロセス間ロック。

設計の正本は TOOL_PACKAGE_DECISIONS.md(D-9)。

これまでの `threading.Lock` は**ハブのプロセス内でしか効かない**ため、
別プロセスの管理アプリから「今ハブが処理中か」を待つことができなかった。
Windows の名前付きミューテックスへ置き換えて、両者が同じ1件ずつの順番待ちに並ぶ。

- ローカルGPUとローカルLLMを共有するため、同時に流すのは1件だけ(従来どおり)
- 管理アプリは `is_busy()` を見て「現在の処理が終わるのを待っています」と出せる
- 保持したままプロセスが落ちても、Windows が放棄扱いにして次の待ち手が取得できる

ミューテックスを作れない環境では、プロセス内ロックへ落ちる。
**その事実は `cross_process` で見えるようにしてある**(黙って劣化させない)。
"""

from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes

# 同じログオンセッション内で共有する。ハブも管理アプリも同じ利用者が動かす
MUTEX_NAME = r"Local\minutes-pipeline-toolpack-run"

_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_WAIT_TIMEOUT = 0x00000102
_INFINITE = 0xFFFFFFFF


class JobLock:
    """1件ずつ実行するためのプロセス間ロック。"""

    def __init__(self, name: str = MUTEX_NAME) -> None:
        self.name = name
        self._handle = None
        self._fallback = threading.Lock()
        self._kernel32 = None
        if sys.platform == "win32":
            try:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                # 64bitでハンドルが切り詰められないよう必ず宣言する(台帳 5-14 と同じ罠)
                kernel32.CreateMutexW.restype = wintypes.HANDLE
                kernel32.CreateMutexW.argtypes = [
                    ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR
                ]
                kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                kernel32.WaitForSingleObject.restype = wintypes.DWORD
                kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
                kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
                handle = kernel32.CreateMutexW(None, False, name)
                if handle:
                    self._handle = handle
                    self._kernel32 = kernel32
            except (OSError, AttributeError):
                self._handle = None

    @property
    def cross_process(self) -> bool:
        """プロセスをまたいで効いているか。False ならプロセス内ロックへ落ちている。"""
        return self._handle is not None

    def acquire(self, timeout: float | None = None) -> bool:
        """取得できたら True。timeout(秒)を過ぎたら False。"""
        if self._handle is None:
            return self._fallback.acquire(timeout=-1 if timeout is None else timeout)
        milliseconds = _INFINITE if timeout is None else int(max(timeout, 0) * 1000)
        waited = self._kernel32.WaitForSingleObject(
            wintypes.HANDLE(self._handle), milliseconds
        )
        # 放棄されたミューテックスも所有権は得られる(前の持ち主が落ちた場合)
        return waited in (_WAIT_OBJECT_0, _WAIT_ABANDONED)

    def release(self) -> None:
        if self._handle is None:
            if self._fallback.locked():
                self._fallback.release()
            return
        self._kernel32.ReleaseMutex(wintypes.HANDLE(self._handle))

    def is_busy(self) -> bool:
        """他が実行中かを覗く(表示用)。取れたらすぐ返すので、空きなら False。"""
        if not self.acquire(timeout=0):
            return True
        self.release()
        return False

    def __enter__(self) -> "JobLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(wintypes.HANDLE(self._handle))
            self._handle = None
