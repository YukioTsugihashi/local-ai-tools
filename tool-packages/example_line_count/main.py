"""行数と文字数を数える(追加ツールの作成例)。

追加ツールの決まり(詳しくは tool-packages/SPEC.md):

- 起動は `main.py --request <request.json>` の1つだけ
- 入力・出力のパスは request.json に書いてある。**絶対パスを自分で組み立てない**
- 書いてよいのは `outdir` の中だけ。入力ファイルは読むだけで、変更しない
- 標準出力へは**JSONを1行だけ**。進捗や本文を print しない
  (失敗時にログへ流れるため、氏名や本文を出力してはいけない)
- 添付ファイルは `toolpack_textio` で読む。**自前で open しない**。
  Word・字幕・Excel など形式ごとの読み方はコアが持っており、
  読めないときは利用者向けの理由と直し方が返る
"""

from __future__ import annotations

import argparse
import json
import os


def main() -> int:
    parser = argparse.ArgumentParser(description="テキストの行数と文字数を数える")
    parser.add_argument("--request", required=True)
    args = parser.parse_args()

    request = json.loads(open(args.request, encoding="utf-8").read())
    job_dir = os.path.dirname(args.request)

    # 入力はコア側の共通部品で読む。形式も文字コードもここでは気にしない
    import toolpack_textio

    try:
        text, _ = toolpack_textio.read_request_input(request, job_dir)
    except toolpack_textio.UnreadableFile as error:
        # 利用者の対処が要る失敗は user_error。理由はそのまま見せてよい
        print(json.dumps({
            "status": "user_error", "message": str(error),
            "files": [], "skipped": [], "notes": [],
        }, ensure_ascii=False))
        return 0

    lines = text.splitlines()
    report = f"行数: {len(lines)}\n文字数: {len(text)}\n"

    # 書き込みは outdir の中だけ
    target = os.path.join(job_dir, request["outdir"], "かぞえた結果.txt")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(report)

    # 標準出力はこのJSON1行のみ。files は outdir からの相対パス
    print(json.dumps({
        "status": "ok",
        "message": f"行数 {len(lines)} / 文字数 {len(text)} を数えました",
        "files": ["かぞえた結果.txt"],
        "skipped": [],   # 利用者の対処が要るもの
        "notes": [],     # 対処が要らない補足
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
