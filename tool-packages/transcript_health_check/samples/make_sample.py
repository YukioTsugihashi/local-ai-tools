"""診断の練習用に、わざと欠点を入れた架空の文字起こしを作る。

**実在の会議・実在の人物は使わない。** すべて架空である。
検査が「見つけるべきものを見つけられるか」を確かめるため、
発言者不明・重複・長すぎ・数値・文字化けを1つずつ埋め込んである。

**Word形式(.docx)で作る。** 院内の文字起こしは実際に25件すべて .docx だった
(台帳 5-20)。見本を .txt にしておくと、受入検査に合格しても実物では
1件も開けない、ということが起こる(同じ誤りを Excel ツールで一度やっている)。
"""

from __future__ import annotations

import argparse
import json
import os

LONG_LINE = (
    "それで先ほどの件なんですけれども現場のほうから上がってきている意見としては"
    "やはり手順が多すぎるという話が中心でして具体的には確認の工程が三回あるところを"
    "二回に減らせないかという提案が出ておりましてこれについては安全性の観点から"
    "簡単には決められないのですが検討する価値はあると考えておりますので"
    "次回までに各部署の意見をまとめて持ち寄るということでいかがでしょうか"
)

SAMPLE_LINES = [
    "第3回 業務改善検討会(架空)",
    "",
    "甲野課長: それでは始めます。本日の議題は3件です。",
    "乙野: 資料は事前に配布したとおりです。",
    "はい、確認しました。",                      # 発言者が分からない
    "丙野主任: 4月10日までに各部署へ周知します。",  # 日時
    "甲野課長: 対象は120名、担当は乙野さんでよろしいですか。",  # 数値と担当
    "乙野: 承知しました。",
    "はい、確認しました。",                      # 重複
    f"丁野: {LONG_LINE}",                        # 長すぎるかたまり
    "戊野: 次回は5月8日、14:30からです。",        # 日時
    "甲野課長: では、そのように進めます。、、、、",  # 記号の乱れ
    "乙野: ありがとうございました。",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="架空の文字起こしを作る")
    parser.add_argument("--request", required=True)
    args = parser.parse_args()

    request = json.loads(open(args.request, encoding="utf-8").read())
    name = "架空の文字起こし.docx"
    target = os.path.join(os.path.dirname(args.request), request["outdir"], name)

    import docx

    document = docx.Document()
    for line in SAMPLE_LINES:
        document.add_paragraph(line)
    document.save(target)

    print(json.dumps({
        "status": "ok",
        "message": "架空の文字起こしを作りました(欠点を埋め込んであります)",
        "files": [name],
        "skipped": [],
        "notes": [],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
