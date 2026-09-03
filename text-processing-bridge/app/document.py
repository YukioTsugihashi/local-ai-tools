"""
構造化議事録の最終データ組み立て。

テンプレートレンダラ(app/render_minutes.py)が受け取る構造を作る。
ハイライト色 = 出典マーカー(緑=文字起こし由来 / 黄=次第由来 / 赤=不明)。
"""

from __future__ import annotations

from typing import Any

from app.agenda_parse import Agenda, _item_key
from app.roster import normalize_person

# 特定できなかったときの穴埋め表記。赤字にして手入力を促す。
BLANK_REPORTER = "  "
BLANK_MATERIAL = "  "


def build_document_data(
    agenda: Agenda,
    assignments: dict[str, dict],
    roster: list[str] | None = None,
    roster_categories: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """テンプレートレンダラが受け取る構造へ組み立てる。"""
    topics: list[dict[str, Any]] = []
    for topic in agenda.topics:
        items: list[dict[str, Any]] = []
        for item in topic.items:
            key = _item_key(topic.number, item.branch)
            payload = assignments.get(key) or {}

            # 出所で色を分ける。
            #   文字起こしから取れた   -> そのまま(黒)
            #   次第から補った         -> 黄色マーカー
            #   どちらからも不明       -> 赤の空欄
            reporter = (payload.get("報告者") or "").strip()
            if reporter:
                reporter_mark = "green"
            elif item.speaker():
                reporter, reporter_mark = item.speaker(), "yellow"
            else:
                reporter, reporter_mark = BLANK_REPORTER, "red"
            if reporter_mark != "red":
                # 呼称は名簿の表記を正とする(乙野先生→乙野教授)。
                reporter = normalize_person(reporter, roster)

            material = (payload.get("資料") or "").strip()
            if material:
                material_mark = "green"
            elif item.material:
                material, material_mark = item.material, "yellow"
            else:
                material, material_mark = BLANK_MATERIAL, "red"

            if agenda.inferred:
                # 推定議題では「  が  に基づき…」の全空欄行は出さない。
                if reporter_mark == "red":
                    explanation = []
                elif material_mark == "red":
                    explanation = [
                        {"text": reporter, "mark": reporter_mark},
                        {"text": "が説明を行った。", "mark": "none"},
                    ]
                else:
                    explanation = [
                        {"text": reporter, "mark": reporter_mark},
                        {"text": "が", "mark": "none"},
                        {"text": material, "mark": material_mark},
                        {"text": "に基づき説明を行った。", "mark": "none"},
                    ]
            else:
                explanation = [
                    {"text": reporter, "mark": reporter_mark},
                    {"text": "が", "mark": "none"},
                    {"text": material, "mark": material_mark},
                    {"text": "に基づき説明を行った。", "mark": "none"},
                ]

            items.append(
                {
                    "枝番": item.branch,
                    "表題": item.title,
                    # 見出しには実在する資料番号だけを載せる。不明時の
                    # 穴埋めスペースは説明文側(赤マーカー)にのみ出す。
                    "資料": "" if material_mark == "red" else material,
                    "資料マーク": material_mark,
                    "担当": item.owner_display(),
                    "説明": explanation,
                    "質疑応答": [
                        {
                            **remark,
                            "発言者": normalize_person(
                                remark.get("発言者", ""), roster
                            ),
                        }
                        for remark in payload.get("発言") or []
                    ],
                }
            )
        if items:
            topics.append(
                {
                    "番号": topic.number,
                    "表題": topic.title,
                    "推定": agenda.inferred,
                    "項目": items,
                }
            )

    header: dict[str, Any] = {
        "タイトル": f"{agenda.title} 議事録" if agenda.title else "",
        "日時": agenda.datetime_text,
        "場所": agenda.place,
    }
    # 名簿が添付されていれば、ヘッダの名簿欄も埋める。
    for category, names in (roster_categories or {}).items():
        header[category] = "、".join(names)

    return {
        "ヘッダ": header,
        "議題": topics,
        "その他": (assignments.get("その他") or {}).get("発言") or [],
        "次回開催": agenda.next_meeting,
    }
