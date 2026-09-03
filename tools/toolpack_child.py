r"""追加ツールを実行する子プロセスの固定ブートストラップ。

設計の正本は TOOL_PACKAGE_DECISIONS.md(D-9)。

**このファイルはコアのGit管理下にあり、パッケージ側から置換できない。**
親ランナー(toolpack_runner)がこのファイルを子プロセスのエントリとして起動する。
子プロセスの中で**最初に監査フックを登録**してから main.py を読み込む。
親ランナーの監査フックでは子プロセスを監視できない(監査フックはプロセス単位)ため、
ここで登録することが要になる。

制限(台帳 D-9):
- 読み取り: Python実行環境 / 自パッケージ / ジョブディレクトリ のみ
- 書き込み: out / tmp / mpl のみ(入力コピーも書き換えられない)
- リンク作成の遮断(os.link / os.symlink / _winapi.CreateJunction)
- 子プロセス起動の禁止
- 通信の禁止(permissions.ollama 宣言時のみ 127.0.0.1:11434 を許可)

【限界】これは実行時ガードであり、ctypes による直接API呼び出しやネイティブ拡張の
内部I/O、os.environ の読み取りまでは監視できない。悪意あるコードの完全封じ込めではない。
読み取り隔離のOS強制は工程4(低整合性)で重ねる。os.environ 対策は親の最小env構築が担う。
"""

from __future__ import annotations

import argparse
import os
import runpy
import sys


# nul(空デバイス)は中身を持たないので読み書きとも無害。
# pytest の出力捕捉が os.devnull を開くため、これを塞ぐとテストが走らない
_NULL_DEVICE = os.path.normcase(os.path.abspath(os.devnull))


def _norm(value: object) -> str | None:
    """パスを比較用に正規化する。監査イベントを起こさない文字列操作だけを使う。"""
    if isinstance(value, int):
        return None  # 既に開かれたfd等は対象外
    try:
        text = os.fsdecode(value)
    except (TypeError, ValueError):
        return None
    return os.path.normcase(os.path.abspath(text))


def _under(path: str, roots: tuple[str, ...]) -> bool:
    for root in roots:
        if path == root or path.startswith(root + os.sep):
            return True
    return False


def _is_write_mode(mode: object, flags: object) -> bool:
    if isinstance(mode, str) and any(mark in mode for mark in ("w", "a", "x", "+")):
        return True
    if isinstance(flags, int):
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        return bool(flags & write_flags)
    return False


def _as_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii", errors="replace")
    return str(value)


def _is_ollama_address(address: object) -> bool:
    """許すのは 127.0.0.1:11434(Ollama)だけ。"""
    if not isinstance(address, tuple) or len(address) < 2:
        return False
    host, port = address[0], address[1]
    return _as_text(host) in ("127.0.0.1", "::1", "localhost") and port == 11434


def install_guard(
    read_roots: tuple[str, ...], write_roots: tuple[str, ...], ollama: bool
) -> None:
    """監査フックを登録する。この後に読み込むコードすべてに効く。"""

    def audit(event: str, args: tuple) -> None:
        if event == "open":
            path = _norm(args[0]) if args else None
            if path is None or path == _NULL_DEVICE:
                return
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else None
            if _is_write_mode(mode, flags):
                if not _under(path, write_roots):
                    raise PermissionError(f"書き込みが許可されていません: {path}")
            elif not _under(path, read_roots):
                raise PermissionError(f"読み取りが許可されていません: {path}")
        elif event in ("os.link", "os.symlink", "nt._winapi.CreateJunction", "_winapi.CreateJunction"):
            raise PermissionError(f"リンクの作成は禁止されています: {event}")
        elif event in ("os.remove", "os.unlink", "os.rmdir", "os.rename", "os.replace",
                       "os.mkdir", "os.chmod", "os.truncate"):
            for arg in args:
                path = _norm(arg)
                if path is not None and not _under(path, write_roots):
                    raise PermissionError(f"{event} が許可されていない場所です: {path}")
        elif event == "os.chdir":
            pass  # 位置移動は無害(パス判定は絶対化しているため)
        elif event in ("subprocess.Popen", "os.system", "os.startfile") or (
            event.startswith("os.") and (
                event[3:].startswith("exec") or event[3:].startswith("spawn") or event[3:] == "posix_spawn"
            )
        ):
            raise PermissionError(f"子プロセスの起動は禁止されています: {event}")
        elif event.startswith("socket.") and event != "socket.__new__":
            # **許可リスト方式**。個別に許した操作以外の socket.* はすべて拒否する。
            # 個別禁止の並べ方だと、gethostbyaddr / getnameinfo のように
            # 書き漏らしたイベントが素通りする(実測で発火を確認)
            if not ollama:
                raise PermissionError("通信は許可されていません(permissions.ollama が false)")
            if event in ("socket.getaddrinfo", "socket.gethostbyname"):
                # Ollamaは 127.0.0.1 直指定で足りる。名前解決を許すと外部DNSへ出られる
                host = args[0] if args else None
                if not (isinstance(host, (str, bytes))
                        and _as_text(host) in ("127.0.0.1", "::1", "localhost")):
                    raise PermissionError("名前解決は 127.0.0.1 以外に使えません")
            elif event in ("socket.connect", "socket.sendto"):
                address = args[1] if len(args) > 1 else None
                if not _is_ollama_address(address):
                    raise PermissionError("接続先は 127.0.0.1:11434(Ollama)だけが許されます")
            else:
                raise PermissionError(f"許可されていない通信操作です: {event}")

    sys.addaudithook(audit)


def _env_python_roots() -> list[str]:
    """import のために読めるべき Python 実行環境のディレクトリ。

    `sys.path[0]` は**このブートストラップが置かれた tools/** になるため、
    そのまま許可するとリポジトリの tools/ 全体が読めてしまう。
    実行環境として要るのは標準ライブラリと site-packages だけなので、
    自分の置き場は許可リストから外す(必要なコアの入口はジョブ領域へ複製して渡す)。
    """
    own_dir = os.path.normcase(os.path.abspath(os.path.dirname(__file__)))
    roots: list[str] = []
    for value in (sys.prefix, sys.base_prefix, sys.exec_prefix, *sys.path):
        if not value or not os.path.isdir(value):
            continue
        normalized = os.path.normcase(os.path.abspath(value))
        if normalized == own_dir:
            continue
        roots.append(normalized)
    return roots


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="追加ツールの隔離実行ブートストラップ")
    parser.add_argument("--package", required=True, help="パッケージ(自ツール)ディレクトリ")
    parser.add_argument("--job", required=True, help="ジョブディレクトリ")
    parser.add_argument("--main", required=True, help="実行する main.py(パッケージ相対または絶対)")
    parser.add_argument("--request", required=True, help="request.json のパス")
    parser.add_argument("--ollama", default="0", help="Ollama を許可するなら 1")
    args = parser.parse_args(argv)

    package_dir = os.path.normcase(os.path.abspath(args.package))
    job_dir = os.path.normcase(os.path.abspath(args.job))
    out_dir = os.path.join(job_dir, "out")
    tmp_dir = os.path.join(job_dir, "tmp")
    mpl_dir = os.path.join(job_dir, "mpl")

    read_roots = tuple(dict.fromkeys([package_dir, job_dir, *_env_python_roots()]))
    write_roots = (out_dir, tmp_dir, mpl_dir)

    main_path = os.path.join(args.package, args.main) if not os.path.isabs(args.main) else args.main
    main_path = os.path.abspath(main_path)
    request_path = os.path.abspath(args.request)

    # フック登録より前に済ませておく(この後は許可リスト外を読めない)
    if not os.path.isfile(main_path):
        raise SystemExit(f"main.py が見つかりません: {main_path}")

    # パッケージ内の兄弟モジュールを import できるようにする。
    # runpy.run_path は実行ファイルの場所を sys.path へ入れないため、
    # これが無いと検証器が許している「パッケージ内のimport」が実行時に失敗する。
    # 自分の置き場(tools/)は上で許可リストから外してあるので、ここでも入れ替える
    sys.path[0] = args.package
    # ジョブ直下にはコアが置いた共通部品(toolpack_textio)がある。
    # **パッケージより先**に置くことで、同名ファイルを同梱しても差し替えられない
    sys.path.insert(0, os.path.abspath(args.job))

    install_guard(read_roots, write_roots, ollama=(str(args.ollama) == "1"))

    # 追加ツールは `main.py --request <path>` として起動される(台帳 D-5)
    sys.argv = [main_path, "--request", request_path]
    runpy.run_path(main_path, run_name="__main__")


if __name__ == "__main__":
    main()
