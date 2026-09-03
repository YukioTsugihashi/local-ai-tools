"""Excel分析パイプのルーティング(依頼文→操作の判定)と各操作の振る舞いテスト。

LLMに判断させず正規表現で確実に振り分ける、というpipeの設計の核を守る。
Open WebUIには接続しない(判定ロジックとopenpyxl処理のみ)。
実行: text-processing-bridge\\.venv\\Scripts\\python.exe -m pytest tools -q
"""

from __future__ import annotations

import io
from pathlib import Path

from openpyxl import Workbook

from open_webui_excel_analysis_pipe import HELP_MESSAGE, Pipe


def build_workbook_bytes() -> bytes:
    """テスト用の2シート構成ブック(集計表と名簿もどき。架空データ)。"""
    workbook = Workbook()
    sheet1 = workbook.active
    sheet1.title = "集計"
    sheet1.append(["月", "件数", "備考"])
    sheet1.append(["4月", 12, "通常"])
    sheet1.append(["5月", 34, "連休あり"])
    sheet1.append(["合計", 46, ""])
    sheet2 = workbook.create_sheet("設定")
    sheet2["A1"] = "しきい値"
    sheet2["B1"] = 100
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# ------------------------------------------------------------------
# 操作の判定
# ------------------------------------------------------------------


def test_each_operation_keyword_routes_correctly() -> None:
    assert Pipe.detect_operation("シート一覧を見せて") == "index"
    assert Pipe.detect_operation("どんな内容が入ってる?") == "index"
    assert Pipe.detect_operation("『合計』を検索して") == "find"
    assert Pipe.detect_operation("4月はどこにある?") == "find"
    assert Pipe.detect_operation("A1:C10を見せて") == "range"
    assert Pipe.detect_operation("集計シートをCSVで出して") == "table"
    assert Pipe.detect_operation("表として出力して") == "table"
    assert Pipe.detect_operation("コメントを生成") == "comment"
    assert Pipe.detect_operation("意見を入力") == "comment"
    assert Pipe.detect_operation("分析内容を追加") == "comment"
    assert Pipe.detect_operation("評価文を書く") == "comment"
    assert Pipe.detect_operation("全対象シートにコメントを作って") == "comment"


def test_range_reference_wins_over_other_keywords() -> None:
    # 範囲指定があれば「見せて/検索」などの語より範囲表示を優先する
    assert Pipe.detect_operation("集計シートのA1:B4を検索して") == "range"
    assert Pipe.detect_operation("分析内容を書くA1:C5を見せて") == "range"


def test_unknown_request_falls_back_to_index() -> None:
    assert Pipe.detect_operation("よろしく") == "index"
    assert Pipe.detect_operation("なにかを書く") == "index"


# ------------------------------------------------------------------
# 依頼文からの抽出
# ------------------------------------------------------------------


def test_quoted_query_and_plain_query_are_extracted() -> None:
    assert Pipe.extract_find_query("「合計」を検索して") == "合計"
    assert Pipe.extract_find_query("『4月』がどこにあるか探して") == "4月"
    assert Pipe.extract_find_query("合計を検索して") == "合計"
    assert Pipe.extract_find_query("検索して") == ""


def test_range_is_extracted_with_fullwidth_colon() -> None:
    assert Pipe.extract_range("A1:C10を表示") == "A1:C10"
    assert Pipe.extract_range("B2：D5 を見せて") == "B2:D5"


def test_sheet_name_is_picked_from_message() -> None:
    sheets = ["集計", "設定"]
    assert Pipe.pick_sheet_name("「設定」シートを見せて", sheets) == "設定"
    assert Pipe.pick_sheet_name("設定シートをCSVで", sheets) == "設定"
    assert Pipe.pick_sheet_name("A1:B2を見せて", sheets) == "集計"  # 既定は先頭


# ------------------------------------------------------------------
# ハブへの委譲(処理本体は tools/excel_reader.py 側にあり、ここでは組み立てだけ)
# ------------------------------------------------------------------


def test_sheet_names_are_read_for_routing_only() -> None:
    # シートの選択はPipeの仕事(操作判定と同じ理由)。中身の処理はハブが行う
    assert Pipe._sheet_names(build_workbook_bytes()) == ["集計", "設定"]


def test_pipe_does_not_process_workbooks_itself() -> None:
    import open_webui_excel_analysis_pipe as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    # 読み取り処理をここへ書き戻すと excel_reader.py と二重実装になる
    for reimplemented in ("_show_index", "_find_values", "_show_range", "_export_table", "import csv"):
        assert reimplemented not in source, f"{reimplemented} が復活している"


def test_mime_is_chosen_by_extension() -> None:
    assert Pipe._mime_for("a.xlsm").endswith("macroEnabled.12")
    assert "spreadsheetml" in Pipe._mime_for("a.xlsx")
    assert Pipe._mime_for("a.csv").startswith("text/csv")
    assert Pipe._mime_for("a.bin") == "application/octet-stream"


# ------------------------------------------------------------------
# 入口ガード(Open WebUI非接続で確かめられる範囲)
# ------------------------------------------------------------------


def run(coroutine):
    import asyncio

    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        coroutine
    )


def test_background_task_returns_fixed_title() -> None:
    result = run(Pipe().pipe({"messages": []}, __task__="title_generation"))
    assert result == "Excel分析"


def test_no_attachment_shows_help() -> None:
    body = {"messages": [{"role": "user", "content": "シート一覧を見せて"}]}
    result = run(Pipe().pipe(body))
    assert result == HELP_MESSAGE


def test_non_excel_attachment_is_explained() -> None:
    body = {"messages": [{"role": "user", "content": "一覧を見せて"}]}
    files = [{"type": "file", "id": "f1", "name": "memo.txt"}]
    result = run(Pipe().pipe(body, __files__=files))
    assert "xlsx" in result


def test_excel_attachments_are_collected_and_remembered() -> None:
    pipe = Pipe()
    files = [
        {"type": "file", "id": "f1", "name": "会議.xlsx"},
        {"type": "file", "id": "f2", "name": "議事録.docx"},
        {"file": {"id": "f3", "filename": "予定.XLSM"}},
    ]
    attachments = pipe._excel_attachments(files)
    assert attachments == [("f1", "会議.xlsx"), ("f3", "予定.XLSM")]
    pipe._remember("chat1", files=attachments)
    assert pipe._recall("chat1")["files"] == attachments


# ------------------------------------------------------------------
# 添付の取り違え(前のメッセージのファイルを巻き込む)防止
# ------------------------------------------------------------------


def test_only_newly_attached_files_are_processed() -> None:
    """
    Open WebUI の __files__ は会話全体のファイルを渡してくる。

    2ファイル添付して依頼 → さらに2ファイル添付して依頼、とすると
    2回目に4件処理していた(2026-08-14に利用者から報告)。
    """
    pipe = Pipe()
    first = [{"type": "file", "id": "f1", "name": "1月.xlsx"}]
    pipe._remember_handled("chat1", pipe._excel_attachments(first))

    # 2回目。__files__ には1回目のファイルも入ってくる
    second = first + [{"type": "file", "id": "f2", "name": "2月.xlsx"}]
    pending = pipe._pending_attachments("chat1", pipe._excel_attachments(second))
    assert pending == [("f2", "2月.xlsx")]


def test_follow_up_without_new_attachment_reuses_the_last_files() -> None:
    # 「さっきのをもう一度」に応えられなくなると、それはそれで使えない
    pipe = Pipe()
    files = pipe._excel_attachments([{"type": "file", "id": "f1", "name": "1月.xlsx"}])
    pipe._remember_handled("chat1", files)

    assert pipe._pending_attachments("chat1", files) == files


def test_handled_ids_are_tracked_per_chat() -> None:
    pipe = Pipe()
    files = pipe._excel_attachments([{"type": "file", "id": "f1", "name": "1月.xlsx"}])
    pipe._remember_handled("chat1", files)

    # 別の会話に同じファイルを出したら、それは初回として処理する
    assert pipe._pending_attachments("chat2", files) == files
