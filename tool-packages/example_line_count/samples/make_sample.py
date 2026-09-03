"""受入検査で使う合成サンプルを作る(実データは絶対に同梱しない)。

この生成器も本体と同じ契約・同じ隔離のもとで動く。
つまり書けるのは `outdir` の中だけで、標準出力はJSON1行だけである。
作ったファイルを `files` へ載せると、受入側がそれを本体への入力として使う。
"""

from __future__ import annotations

import argparse
import json
import os

SAMPLE = """第1回 定例会議(架空)
出席: 甲野、乙野
議題: 受入機構の確認
決定: 次回までに手順を整える
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="合成サンプルを作る")
    parser.add_argument("--request", required=True)
    args = parser.parse_args()

    request = json.loads(open(args.request, encoding="utf-8").read())
    target = os.path.join(os.path.dirname(args.request), request["outdir"], "sample.txt")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(SAMPLE)

    print(json.dumps({
        "status": "ok",
        "message": "合成サンプルを作りました",
        "files": ["sample.txt"],
        "skipped": [],
        "notes": [],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
