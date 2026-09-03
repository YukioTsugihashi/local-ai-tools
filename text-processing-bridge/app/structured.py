"""
構造化議事録のパイプライン(/v1/text/minutes_structured の本体)。

次第から骨格を確定し、文字起こしをブロック分割してLLMへ2周かけ、
結果をコード側で決定的に統合する。LLMの分類漏れ・重複・話し言葉は
それぞれ後段のチェックで拾う。
"""

from __future__ import annotations

import re

from fastapi import HTTPException

from app.agenda_parse import Agenda, _item_key, build_inferred_agenda, parse_agenda
from app.chunking import split_text_with_overlap
from app.config import (
    long_text_chunk_chars,
    long_text_overlap_chars,
    long_text_threshold_chars,
)
from app.document import build_document_data
from app.instructions import (
    build_format_instruction,
    build_topic_inference_instruction,
)
from app.kebatori import KebatoriProfiles, apply_kebatori, normalize_line_endings
from app.llm_lines import parse_structured_lines, parse_topic_lines
from app.local_workflow import LocalWorkflowClient
from app.merge import merge_chunk_results
from app.models import StructuredMinutesRequest, StructuredMinutesResponse
from app.processing import _run_workflow
from app.roster import parse_roster, parse_roster_categories

# 話し言葉の残存検出。強制チェック②で書き言葉へ直す対象を拾う。
SPOKEN_STYLE_PATTERN = re.compile(
    r"でございま|ですね|ですけど|んですけど|けれども|けども、|しましては|"
    r"かなと思|なんですよ|んですよ、|でして、"
)


async def _infer_agenda(
    client: LocalWorkflowClient, blocks: list[str], source_head: str
) -> Agenda:
    """フェーズ1: 次第が無い場合、議題構成を文字起こしから推定する。"""
    known_titles: list[str] = []
    for index, block in enumerate(blocks):
        result = await _run_workflow(
            client,
            {
                "operation": "minutes",
                "text": block,
                "max_chars": 0,
                "kebatori_rules_json": "{}",
                "retry_instruction": build_topic_inference_instruction(
                    index, len(blocks), known_titles
                ),
            },
        )
        known_titles.extend(parse_topic_lines(result.text))
    return build_inferred_agenda(known_titles, source_head)


async def _extract_pass(
    client: LocalWorkflowClient,
    blocks: list[str],
    agenda: Agenda,
    roster: list[str],
    extra_instruction: str,
    already: list[str] | None = None,
) -> tuple[list[dict[str, dict]], str]:
    """
    全ブロックへ振り分け指示を掛けて1周分の抽出結果を返す。

    会議は次第の順に進むため、現在位置と既出項目を毎回伝える。
    全項目の一覧だけを見せると、どの項目の話か判断できず誤配が起きる。
    2周目は1周目の結果を「既出一覧」(already)として渡し、差分だけを出させる。
    """
    parsed_blocks: list[dict[str, dict]] = []
    covered: list[str] = []
    last_run_id = ""
    for index, block in enumerate(blocks):
        instruction = build_format_instruction(
            agenda,
            roster,
            block_index=index,
            block_total=len(blocks),
            covered=covered,
            already=already,
        ) + extra_instruction

        result = await _run_workflow(
            client,
            {
                "operation": "minutes",
                "text": block,
                "max_chars": 0,
                "kebatori_rules_json": "{}",
                "retry_instruction": instruction,
            },
        )
        parsed = parse_structured_lines(result.text)
        parsed_blocks.append(parsed)
        for key in parsed:
            if key not in covered and key != "その他":
                covered.append(key)
        last_run_id = result.workflow_run_id
    return parsed_blocks, last_run_id


def _rescue_unmatched_keys(
    assignments: dict[str, dict], valid_keys: set[str]
) -> list[str]:
    """
    記号の書き癖の救済: 「1-1」のように枝番を付け足された場合、
    議題番号の部分が一致すればその議題へ振り替える。
    救済できなかったキーの一覧を返す(呼び出し側で警告して捨てる)。
    """
    for key in list(assignments.keys()):
        if key in valid_keys:
            continue
        fallback = key.split("-", 1)[0]
        if fallback in valid_keys and fallback != key:
            target = assignments.setdefault(
                fallback, {"報告者": "", "資料": "", "発言": []}
            )
            payload = assignments.pop(key)
            if not target.get("報告者") and payload.get("報告者"):
                target["報告者"] = payload["報告者"]
            if not target.get("資料") and payload.get("資料"):
                target["資料"] = payload["資料"]
            target["発言"] = (target.get("発言") or []) + (
                payload.get("発言") or []
            )
    return sorted(k for k in assignments if k not in valid_keys)


async def _polish_spoken_style(
    client: LocalWorkflowClient, assignments: dict[str, dict]
) -> list[str]:
    """
    強制チェック②: 話し言葉が残った発言を検出し、まとめて書き言葉へ直す。

    検出はコード(正規表現)、書き直しはLLM、差し替えは行番号の一致を確認して
    コードが行う。行数が合わなければ差し替えず警告だけ残す。
    """
    flagged = [
        remark
        for payload in assignments.values()
        for remark in payload.get("発言") or []
        if SPOKEN_STYLE_PATTERN.search(remark.get("内容", ""))
    ]
    if not flagged:
        return []

    numbered = "\n".join(
        f"{index + 1}. {remark['内容']}" for index, remark in enumerate(flagged)
    )
    try:
        polish = await _run_workflow(
            client,
            {
                "operation": "refine",
                "text": numbered,
                "max_chars": 0,
                "kebatori_rules_json": "{}",
                "retry_instruction": (
                    "各行を議事録の書き言葉に直してください。"
                    "文末は「〜である」「〜と考える」「〜のか」「〜してほしい」等に整え、"
                    "「ですね」「けども」「でございます」などの話し言葉を取り除きます。"
                    "内容・数値・固有名詞は一切変えません。"
                    "行数と行頭の番号を保ったまま、同じ形式で出力してください。"
                ),
            },
        )
    except HTTPException:
        return [f"話し言葉が残っている発言が{len(flagged)}件あります。"]

    numbered_line = re.compile(r"^\s*(\d+)[\.．:：]?\s*(.+)$")
    fixed: dict[int, str] = {}
    for line in polish.text.split("\n"):
        match = numbered_line.match(line.strip())
        if match:
            fixed[int(match.group(1))] = match.group(2).strip()
    if len(fixed) != len(flagged):
        return [
            f"話し言葉が残っている発言が{len(flagged)}件あります"
            "(自動修正は行数不一致のため見送り)。"
        ]
    for index, remark in enumerate(flagged):
        replacement = fixed.get(index + 1)
        if replacement:
            remark["内容"] = replacement
    return [f"話し言葉が残っていた{len(flagged)}件を書き言葉へ整えました。"]


async def run_structured_minutes(
    request: StructuredMinutesRequest,
    client: LocalWorkflowClient,
    profiles: KebatoriProfiles,
) -> StructuredMinutesResponse:
    # 次第があればそれを骨格にする。無ければ文字起こしから推定する。
    agenda = None
    if request.agenda_text.strip():
        agenda = parse_agenda(request.agenda_text)
        if agenda.is_empty():
            raise HTTPException(
                status_code=422,
                detail="次第から議題を読み取れませんでした。次第のファイルを確認してください。",
            )

    source = normalize_line_endings(request.text)
    warnings: list[str] = []

    # 名簿があれば発言者名の表記揺れを正す材料にする。
    roster = parse_roster(request.roster_text) or parse_roster(request.agenda_text)
    roster_categories = parse_roster_categories(request.roster_text)

    # フィラーは入力段階で落としておく。LLMの負担を減らし、
    # 抽出後にもう一度掛けるため二重に効く。
    kebatori_profile = profiles.get("default")
    source = apply_kebatori(source, kebatori_profile)

    extra_instruction = ""
    if request.instruction.strip():
        extra_instruction = "\n\n【追加指示】\n" + request.instruction.strip()

    # 項目が確定しているので、分割しても項目ごとに機械的へ統合できる。
    # LLMによる統合パスを挟まないため、形式崩れも情報の取りこぼしも起きない。
    if len(source) > long_text_threshold_chars():
        # 境界をまたぐ発言の取りこぼしを防ぐため、ブロック間を重ねて分割する。
        # 重ね部分の二重抽出は統合側の近似重複除去が吸収する。
        blocks = split_text_with_overlap(
            source, long_text_chunk_chars(), long_text_overlap_chars()
        )
        warnings.append(f"長文のため{len(blocks)}ブロックに分割して処理しました。")
    else:
        blocks = [source]

    # フェーズ1: 骨格さえ決まれば、以降は次第ありと同じ経路をそのまま使える。
    agenda_inferred = False
    if agenda is None:
        agenda = await _infer_agenda(client, blocks, source[:800])
        agenda_inferred = True
        warnings.append(
            f"次第が無いため、議題構成({len(agenda.topics)}件)を"
            "文字起こしから推定しました(見出しは黄マーカー)。"
        )

    parsed_blocks, last_run_id = await _extract_pass(
        client, blocks, agenda, roster, extra_instruction
    )
    assignments = merge_chunk_results(parsed_blocks)

    # 強制チェック①: 条件なしで全ブロックをもう一周し、和集合を取る。
    # 拾い方のムラは実行ごとに変わるため、独立した2周分を合算すると
    # 部分的な漏れが減る。重複は近似重複除去が吸収する。
    # ゼロ項目だけを狙う方式は「一部だけ拾えた項目」の漏れに無力だった。
    first_pass_count = sum(
        len(payload.get("発言") or []) for payload in assignments.values()
    )
    # 「見つからなかった項目を特に探せ」という圧力は、無い発言の捏造を
    # 誘発したため使わない。
    already = [
        (remark.get("内容") or "")[:60]
        for payload in assignments.values()
        for remark in payload.get("発言") or []
    ]
    second_blocks, last_run_id = await _extract_pass(
        client, blocks, agenda, roster, extra_instruction, already=already
    )
    parsed_blocks += second_blocks
    assignments = merge_chunk_results(parsed_blocks)
    merged_count = sum(
        len(payload.get("発言") or []) for payload in assignments.values()
    )
    warnings.append(
        f"全ブロックを2周して抽出し、和集合を取りました"
        f"(1周目{first_pass_count}件 → 合算{merged_count}件)。"
    )

    # LLMが取りこぼしたフィラーを、ルールで確定的に除去する。
    # 文書へ載る最終テキストなので、ここを通せば結果が必ず揃う。
    for payload in assignments.values():
        for remark in payload.get("発言") or []:
            remark["内容"] = apply_kebatori(remark["内容"], kebatori_profile).strip()
        payload["発言"] = [r for r in payload.get("発言") or [] if r["内容"]]

    valid_keys = {
        _item_key(topic.number, item.branch)
        for topic, item in agenda.flat_items()
    }
    valid_keys.add("その他")

    unmatched = _rescue_unmatched_keys(assignments, valid_keys)
    if unmatched:
        warnings.append(
            "項目一覧に無い記号が返されたため無視しました: " + "、".join(unmatched[:5])
        )
        for key in unmatched:
            assignments.pop(key, None)

    warnings.extend(await _polish_spoken_style(client, assignments))

    assigned = sum(len(v.get("発言") or []) for v in assignments.values())
    if assigned == 0:
        warnings.append("発言を1件も振り分けられませんでした。文字起こしと次第の対応を確認してください。")

    return StructuredMinutesResponse(
        document=build_document_data(
            agenda,
            assignments,
            roster=roster,
            roster_categories=roster_categories,
        ),
        source_chars=len(source),
        agenda_chars=len(request.agenda_text),
        roster_chars=len(request.roster_text),
        roster_names=len(roster),
        blocks=len(blocks),
        assigned_remarks=assigned,
        unmatched_keys=unmatched,
        agenda_inferred=agenda_inferred,
        warnings=warnings,
        workflow_run_id=last_run_id,
    )
