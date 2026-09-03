"""
Windows上のローカルサービスをまとめて起動する。

  python tools\\start_local_services.py            (画面にログを出す。手動確認用)
  pythonw tools\\start_local_services.py --log     (ログをファイルへ。自動起動用)

  port 8010  ハブ(Open WebUIからKiloツールを呼ぶ受付)
  port 8008  文章処理ブリッジ(議事録・要約・整形・ケバ取り)

2026-08-14 に Dify を撤去した結果、ブリッジの相手がWindows側のOllamaだけになり、
コンテナへ置く理由が無くなったため、ここへ降ろして1プロセスにまとめた。
起動を1つにしておかないと、片方だけ動いていない状態に気づけない。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import uvicorn


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_ROOT = ROOT / "text-processing-bridge"
# 運用ログの保存先。既定は実行ディレクトリ基準のため、ここで固定する
os.environ.setdefault("BRIDGE_LOG_DIR", str(BRIDGE_ROOT / "logs"))

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BRIDGE_ROOT))

from app.main import app as bridge_app  # noqa: E402
from local_tool_bridge.app import app as hub_app  # noqa: E402

LOG_PATH = ROOT / "logs" / "local_services.log"


def _redirect_output_to_log() -> None:
    """標準出力と標準エラーをログファイルへ向ける。"""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stream = LOG_PATH.open("a", encoding="utf-8", errors="replace", buffering=1)
    sys.stdout = stream
    sys.stderr = stream


# **この端末の中からだけ届くようにする**(台帳 D-16)。
# 0.0.0.0 で待つと院内LANの他端末から呼べてしまい、追加ツールが増えるほど
# 「LANから任意のツールを実行できる受付」になる。
# 台帳には「127.0.0.1 にすると Open WebUI(コンテナ)から繋がらないので採れない」と
# 書いていたが、**2026-09-02 に実測して誤りと分かった**。
# Docker Desktop は host.docker.internal 宛てを折り返しへ中継するため、
# コンテナからの要求はハブに **127.0.0.1 として届く**(5-12 を訂正)
DEFAULT_HOST = "127.0.0.1"


async def _serve(services: list[tuple[object, int]], host: str = DEFAULT_HOST) -> None:
    servers = [
        uvicorn.Server(uvicorn.Config(service, host=host, port=port))
        for service, port in services
    ]
    await asyncio.gather(*(server.serve() for server in servers))


def main() -> None:
    parser = argparse.ArgumentParser(description="ローカルサービスをまとめて起動する")
    parser.add_argument("--log", action="store_true", help="ログをファイルへ出す(自動起動用)")
    parser.add_argument("--hub-port", type=int, default=8010)
    parser.add_argument("--bridge-port", type=int, default=8008)
    parser.add_argument(
        "--host", default=DEFAULT_HOST,
        help="待ち受けるアドレス(既定は端末内のみ)。**広げるとLANから呼べる**",
    )
    args = parser.parse_args()

    if args.log:
        _redirect_output_to_log()
    asyncio.run(
        _serve([(hub_app, args.hub_port), (bridge_app, args.bridge_port)], args.host)
    )


if __name__ == "__main__":
    main()
