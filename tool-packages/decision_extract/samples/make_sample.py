"""検査用の架空の文字起こしを作る。**実在の会議・人物は使わない。**

決定事項と宿題が1件ずつはっきり含まれるようにしてある。
受入検査は「LLMの答えの良し悪し」ではなく「一連の流れが通るか」を見るので、
抽出しやすい素直な文章にしてある。
"""

from __future__ import annotations

import argparse
import json
import os

SAMPLE = """第2回 備品管理検討会(架空)

甲野課長: 本日は備品の発注方法について決めたいと思います。
乙野: 現状は各部署がばらばらに発注しています。
甲野課長: それでは、来月から発注窓口を総務課に一本化することを決定します。
丙野主任: 承知しました。
甲野課長: 丙野主任は、4月30日までに各部署へ新しい手順を周知してください。
丙野主任: はい、対応します。
乙野: 様式の見直しも必要だと思います。
甲野課長: 様式については次回また相談しましょう。
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="架空の文字起こしを作る")
    parser.add_argument("--request", required=True)
    args = parser.parse_args()

    request = json.loads(open(args.request, encoding="utf-8").read())
    # 実物の文字起こしは .docx なので、見本もその形式で作る(台帳 5-20)
    name = "架空の会議.docx"
    target = os.path.join(os.path.dirname(args.request), request["outdir"], name)

    import docx

    document = docx.Document()
    for line in SAMPLE.splitlines():
        document.add_paragraph(line)
    document.save(target)

    print(json.dumps({
        "status": "ok",
        "message": "架空の文字起こしを作りました",
        "files": [name],
        "skipped": [],
        "notes": [],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
