"""
pipeのルーティング(依頼文→処理・形式の判定)の振る舞いテスト。

LLMに判断させず正規表現で確実に振り分ける、というpipeの設計の核を守る。
ブリッジやOpen WebUIには接続しない(判定ロジックと入口ガードのみ)。
実行: pre-commitフックが自動で回す。手動なら
  text-processing-bridge\\.venv\\Scripts\\python.exe -m pytest tools -q
"""

from __future__ import annotations

import asyncio

import importlib.util
from pathlib import Path

import open_webui_text_processing_pipe as pipe_mod


def _load_by_path(name: str, filename: str):
    """ファイル名に . を含むモジュールを読む(改名はしない方針のため)。"""
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ToolsModule = _load_by_path(
    "webui_text_processing_tool", "open_webui_dify_text_processing_tool_v0.6.0.py"
)
from open_webui_text_processing_pipe import (
    FORMAT_PATTERNS,
    HELP_MESSAGE,
    MAX_CHARS_PATTERN,
    NOT_INSTRUCTION_PATTERN,
    OPERATION_PATTERNS,
    Pipe,
)


def detect_operations(text: str) -> list[str]:
    return [op for op, pattern in OPERATION_PATTERNS if pattern.search(text)]


def detect_formats(text: str) -> list[str]:
    return [fmt for fmt, pattern in FORMAT_PATTERNS if pattern.search(text)]


# ------------------------------------------------------------------
# 処理の判定
# ------------------------------------------------------------------


def test_each_operation_keyword_routes_correctly() -> None:
    assert detect_operations("ケバ取りしてください") == ["kebatori"]
    assert detect_operations("けばとりお願い") == ["kebatori"]
    assert detect_operations("フィラーを除去して") == ["kebatori"]
    assert detect_operations("整形して") == ["rewrite"]
    assert detect_operations("読みやすくして") == ["rewrite"]
    assert detect_operations("要約をお願いします") == ["summarize"]
    assert detect_operations("この内容をまとめて") == ["summarize"]
    assert detect_operations("議事録にして") == ["minutes"]


def test_multiple_operations_are_all_detected_in_order() -> None:
    # 「一致した処理をすべて実行する」仕様。順序はOPERATION_PATTERNSの定義順
    assert detect_operations("ケバ取りと議事録をお願い") == ["kebatori", "minutes"]
    assert detect_operations("整形して要約もして") == ["rewrite", "summarize"]


def test_plain_chat_matches_no_operation() -> None:
    assert detect_operations("こんにちは") == []
    assert detect_operations("この会議はどうでしたか") == []


# ------------------------------------------------------------------
# 出力形式の判定
# ------------------------------------------------------------------


def test_format_keywords_route_correctly() -> None:
    assert detect_formats("議事録をWordで出して") == ["docx"]
    assert detect_formats("ワードでください") == ["docx"]
    assert detect_formats("要約をExcelで") == ["xlsx"]
    assert detect_formats("エクセルにして") == ["xlsx"]
    assert detect_formats("CSVで出力") == ["csv"]
    assert detect_formats("マークダウンで") == ["md"]
    assert detect_formats("テキストファイルで") == ["txt"]


def test_multiple_formats_are_all_detected() -> None:
    assert detect_formats("WordとExcelで出して") == ["docx", "xlsx"]


def test_format_keywords_absent_means_no_match() -> None:
    assert detect_formats("議事録にして") == []


# ------------------------------------------------------------------
# 文字数指定・追加指示の判定
# ------------------------------------------------------------------


def test_max_chars_is_extracted() -> None:
    assert MAX_CHARS_PATTERN.search("800字以内で要約して").group(1) == "800"
    assert MAX_CHARS_PATTERN.search("2000文字程度でまとめて").group(1) == "2000"
    assert MAX_CHARS_PATTERN.search("要約して") is None


def test_greetings_are_not_treated_as_instructions() -> None:
    for text in ("こんにちは", "ありがとう!", "了解です。", "お疲れさまでした"):
        assert NOT_INSTRUCTION_PATTERN.match(text) is not None, text


def test_substantive_messages_are_treated_as_instructions() -> None:
    for text in ("稼働率の話を厚めに", "議題3の救急の話は議題2に移して"):
        assert NOT_INSTRUCTION_PATTERN.match(text) is None, text


# ------------------------------------------------------------------
# ケバ取りの強弱・追加指示の解決(抽出ハンドラー)
# ------------------------------------------------------------------


def test_kebatori_defaults_to_strong() -> None:
    pipe = Pipe()
    assert pipe._apply_kebatori_strength(["kebatori"], "ケバ取りして") == [
        "kebatori_plus"
    ]


def test_fast_words_switch_kebatori_to_rule_only() -> None:
    pipe = Pipe()
    assert pipe._apply_kebatori_strength(["kebatori"], "高速ケバ取りして") == [
        "kebatori"
    ]
    assert pipe._apply_kebatori_strength(["kebatori"], "さっとケバ取りして") == [
        "kebatori"
    ]


def test_strong_words_opt_in_when_default_is_fast() -> None:
    pipe = Pipe()
    pipe.valves.KEBATORI_DEFAULT_STRONG = False
    assert pipe._apply_kebatori_strength(["kebatori"], "ケバ取りして") == ["kebatori"]
    assert pipe._apply_kebatori_strength(["kebatori"], "しっかりケバ取りして") == [
        "kebatori_plus"
    ]


def test_extract_max_chars() -> None:
    assert Pipe._extract_max_chars("800字以内で要約して") == 800
    assert Pipe._extract_max_chars("要約して") == 0


def test_follow_up_reruns_last_operation() -> None:
    # 処理済みの会話では、処理名の無い入力は前回(最後)の処理への追加指示になる
    operations, instruction = Pipe._resolve_follow_up(
        [], "稼働率の話を厚めに", {"operations": ["kebatori_plus", "summarize"]}, False
    )
    assert operations == ["summarize"]
    assert instruction == "稼働率の話を厚めに"


def test_greeting_after_processing_shows_help() -> None:
    operations, instruction = Pipe._resolve_follow_up(
        [], "ありがとう", {"operations": ["summarize"]}, False
    )
    assert operations == []
    assert instruction == ""


def test_named_operation_in_continued_chat_keeps_instruction() -> None:
    # 会話の続きで処理名がある場合、依頼文を指示としても渡す
    operations, instruction = Pipe._resolve_follow_up(
        ["minutes"], "議事録を決定事項中心で", {"base_text": "本文"}, False
    )
    assert operations == ["minutes"]
    assert instruction == "議事録を決定事項中心で"


def test_new_files_reset_the_conversation() -> None:
    # 新しいファイルが添付されたら仕切り直し(指示を引き継がない)
    operations, instruction = Pipe._resolve_follow_up(
        ["minutes"], "議事録にして", {"base_text": "本文"}, True
    )
    assert operations == ["minutes"]
    assert instruction == ""


# ------------------------------------------------------------------
# 本文からの指示行除去
# ------------------------------------------------------------------


def test_command_lines_are_stripped_from_pasted_text() -> None:
    text = "ケバ取りしてください\n本文の1行目です。\n本文の2行目です。"
    assert Pipe._strip_command_lines(text) == "本文の1行目です。\n本文の2行目です。"


def test_body_lines_mentioning_operations_are_kept() -> None:
    # 指示語(して・お願い等)を伴わない行は本文として残す
    text = "議事録の管理方法について\n詳細は以下のとおり。"
    assert Pipe._strip_command_lines(text) == text


# ------------------------------------------------------------------
# 次第の判定
# ------------------------------------------------------------------

AGENDA_TEXT = (
    "令和8年度 第1回 経営企画会議 次第\n"
    "日 時：令和8年6月9日（火）17:00～\n"
    "議 題\n"
    "1 病院の経営状況について\n"
    "① 月次収支の報告 ・・・・・ 資料1\n"
    "次回開催予定：令和8年7月14日（月）\n"
)
TRANSCRIPT_TEXT = (
    "それでは始めます。よろしくお願いします。"
    "先月の状況ですが、外来患者数は増えています。ですね、そうです。"
    "次の議題についてはどうですか。はい、そうですね。頑張ります。"
) * 30


def test_agenda_is_separated_from_transcript() -> None:
    agenda, rest = Pipe._split_agenda(
        [("文字起こし.txt", TRANSCRIPT_TEXT), ("次第.pdf", AGENDA_TEXT)]
    )
    assert agenda == AGENDA_TEXT
    assert rest == [("文字起こし.txt", TRANSCRIPT_TEXT)]


def test_single_attachment_is_never_agenda() -> None:
    agenda, rest = Pipe._split_agenda([("次第.pdf", AGENDA_TEXT)])
    assert agenda == ""
    assert len(rest) == 1


def test_two_transcripts_yield_no_agenda() -> None:
    agenda, rest = Pipe._split_agenda(
        [("会議A.txt", TRANSCRIPT_TEXT), ("会議B.txt", TRANSCRIPT_TEXT)]
    )
    assert agenda == ""
    assert len(rest) == 2


# ------------------------------------------------------------------
# 入力の取り出し
# ------------------------------------------------------------------


def test_latest_user_message_is_taken_from_history() -> None:
    pipe = Pipe()
    body = {
        "messages": [
            {"role": "user", "content": "古いメッセージ"},
            {"role": "assistant", "content": "応答"},
            {"role": "user", "content": [{"type": "text", "text": "最新の依頼"}]},
        ]
    }
    assert pipe._latest_user_message(body) == "最新の依頼"


# ------------------------------------------------------------------
# pipe()の入口ガード(ネットワーク不要の経路のみ)
# ------------------------------------------------------------------


def test_background_task_gets_fixed_reply() -> None:
    result = asyncio.run(Pipe().pipe({}, __task__="title_generation"))
    assert result == "文章処理"


def test_empty_message_shows_help() -> None:
    result = asyncio.run(Pipe().pipe({"messages": []}))
    assert result == HELP_MESSAGE


def test_greeting_without_state_shows_help() -> None:
    body = {"messages": [{"role": "user", "content": "こんにちは"}]}
    assert asyncio.run(Pipe().pipe(body)) == HELP_MESSAGE


# ------------------------------------------------------------------
# 添付の取り違え(前のメッセージのファイルを巻き込む)防止
# ------------------------------------------------------------------


def test_only_newly_attached_files_are_processed() -> None:
    """
    Open WebUI の __files__ は会話全体のファイルを渡してくる。

    絞り込まないと、2回目の依頼で前回の文字起こしまで本文に混ざる
    (Excel側で2026-08-14に利用者から報告された不具合と同型)。
    """
    pipe = Pipe()
    first = [{"type": "file", "id": "f1", "name": "1月定例.txt"}]
    pipe._remember_handled("chat1", first)

    second = first + [{"file": {"id": "f2", "filename": "2月定例.txt"}}]
    assert pipe._pending_files("chat1", second) == [second[1]]


def test_follow_up_without_new_attachment_has_no_files() -> None:
    # 添付なしの追加指示は、会話の土台(前回の処理結果)で動く経路へ渡す
    pipe = Pipe()
    files = [{"type": "file", "id": "f1", "name": "1月定例.txt"}]
    pipe._remember_handled("chat1", files)

    assert pipe._pending_files("chat1", files) == []


def test_handled_ids_are_tracked_per_chat() -> None:
    pipe = Pipe()
    files = [{"type": "file", "id": "f1", "name": "1月定例.txt"}]
    pipe._remember_handled("chat1", files)

    assert pipe._pending_files("chat2", files) == files


def test_follow_up_falls_back_to_previous_text_instead_of_the_old_file() -> None:
    """添付が古いものだけなら、ファイルではなく前回の本文を土台にする。"""
    pipe = Pipe()
    files = [{"type": "file", "id": "f1", "name": "1月定例.txt"}]
    pipe._remember_handled("chat1", files)
    state = {"base_text": "前回のケバ取り結果", "base_name": "1月定例.txt"}

    pending = pipe._pending_files("chat1", files)
    sources = asyncio.run(pipe._prepare_sources("要約して", state, pending))

    file_contents, _agenda, _roster, source_name, source_text = sources
    assert file_contents == []
    assert source_text == "前回のケバ取り結果"
    assert source_name == "1月定例.txt"


# ------------------------------------------------------------------
# 添付の取り違え(前のメッセージのファイルを巻き込む)防止
# ------------------------------------------------------------------


def test_tool_uses_only_the_latest_message_attachments() -> None:
    """
    __files__ / metadata["files"] は会話全体のファイルを持つ。

    先に見てしまうと、2回目の依頼で前のメッセージの添付まで処理する
    (Pipe側で2026-08-14に利用者から報告された不具合と同型)。
    """
    tools = ToolsModule.Tools()
    chat_wide = [
        {"type": "file", "id": "old", "name": "1月定例.txt"},
        {"type": "file", "id": "new", "name": "2月定例.txt"},
    ]
    metadata = {
        "files": chat_wide,
        "user_message": {"role": "user", "content": "議事録にして", "files": [chat_wide[1]]},
    }

    _messages, files = tools._resolve_inputs(None, chat_wide, metadata)
    assert files == [chat_wide[1]]


def test_tool_falls_back_to_chat_files_when_nothing_newly_attached() -> None:
    tools = ToolsModule.Tools()
    chat_wide = [{"type": "file", "id": "old", "name": "1月定例.txt"}]
    metadata = {"user_message": {"role": "user", "content": "もっと短く"}}

    _messages, files = tools._resolve_inputs(None, chat_wide, metadata)
    assert files == chat_wide


# ---------------------------------------------------------------------------
# 出席者名簿の選び分け
# ---------------------------------------------------------------------------

ROSTER_TEXT = (
    "所属\t職名\t氏名\n"
    "事務局\t局長\t架空 太郎\n"
    "内科\t部長\t架空 花子\n"
    "外科\t医長\t架空 次郎\n"
)
TRANSCRIPT_TEXT = (
    "それでは会議を始めます。まず資料の説明をお願いします。\n"
    "はい、こちらの数値ですけど、前年比で増加しています。\n"
    "ありがとうございます。ご質問はありますでしょうか。\n"
) * 30


def test_名簿は添付の順番に関係なく選び分ける() -> None:
    """次第→名簿→文字起こしの順で渡すと、名簿を文字起こしとして
    処理し、本物の文字起こしを名簿として捨てていた(発言0件)。
    """
    for order in (
        [("出席者名簿.xlsx", ROSTER_TEXT), ("会議.docx", TRANSCRIPT_TEXT)],
        [("会議.docx", TRANSCRIPT_TEXT), ("出席者名簿.xlsx", ROSTER_TEXT)],
    ):
        roster, rest = Pipe._split_roster(order)
        assert "架空 太郎" in roster, order
        assert len(rest) == 1, order
        assert rest[0][0] == "会議.docx", order


def test_名簿は次第が無くても選び分ける() -> None:
    """次第が無いと名簿を見分けず、名簿からも空の議事録が1本できていた。"""
    roster, rest = Pipe._split_roster(
        [("会議.docx", TRANSCRIPT_TEXT), ("出席者名簿.xlsx", ROSTER_TEXT)]
    )
    assert roster and len(rest) == 1


def test_名簿が無ければ複数の文字起こしはそのまま残す() -> None:
    """名簿と見分けられない複数添付は、今までどおり1本ずつ処理する。"""
    both = [("会議A.docx", TRANSCRIPT_TEXT), ("会議B.docx", TRANSCRIPT_TEXT)]
    roster, rest = Pipe._split_roster(both)
    assert roster == ""
    assert rest == both


def test_名簿だけを渡したときは本文を奪わない() -> None:
    only = [("出席者名簿.xlsx", ROSTER_TEXT), ("参加者一覧.xlsx", ROSTER_TEXT)]
    roster, rest = Pipe._split_roster(only)
    assert roster == "", "本文が無くなってしまう"
    assert rest == only


def test_添付1件のときは名簿を切り出さない() -> None:
    single = [("出席者名簿.xlsx", ROSTER_TEXT)]
    assert Pipe._split_roster(single) == ("", single)


def test_文字起こしは名簿と判定しない() -> None:
    assert Pipe._roster_score("会議.docx", TRANSCRIPT_TEXT) < 6
    assert Pipe._roster_score("出席者名簿.xlsx", ROSTER_TEXT) >= 6


AGENDA_TEXT_FOR_ROLES = (
    "令和8年度 第3回 病院経営・運営会議 次第\n"
    "1 開会\n"
    "2 議題\n"
    "　① 経営指標について ･･････････････ 資料1\n"
    "　② 運営状況について ･･････････････ 資料2\n"
    "3 次回開催予定\n"
)


def test_次第と名簿と文字起こしを順不同で正しく割り当てる() -> None:
    """利用者が実際に渡した順(次第→名簿→文字起こし)で発言0件になっていた。"""
    pipe = Pipe()
    attachments = [
        ("00_令和8年度第3回次第.docx", AGENDA_TEXT_FOR_ROLES),
        ("出席者名簿.xlsx", ROSTER_TEXT),
        ("病院経営・運営会議 20260609.docx", TRANSCRIPT_TEXT),
    ]

    async def loaded(_files):
        return list(attachments)

    pipe._load_full_file_contents = loaded
    file_contents, agenda, roster, source_name, source_text = asyncio.run(
        pipe._prepare_sources("これの議事録を作成してください", {}, [{"id": "x"}])
    )

    assert "次第" in agenda, "次第を取り出せていない"
    assert "架空 太郎" in roster, "名簿を取り出せていない"
    assert source_name == "病院経営・運営会議 20260609.docx", "本文が文字起こしになっていない"
    assert "会議を始めます" in source_text
    assert len(file_contents) == 1, "議事録が複数本できてしまう"


# 名簿は番号付きの行が並ぶため、次第の採点(番号付き項目が3行以上で加点)に
# 引っかかりやすい。実運用で実際に次第として取られ、名簿が使われなかった
ROSTER_LOOKS_LIKE_AGENDA = (
    "令和8年度 第3回 病院経営・運営会議 出席者名簿\n"
    "１ 議長　　架空 太郎　　　　資料1\n"
    "２ 副議長　架空 花子\n"
    "３ 委員　　架空 次郎\n"
    "４ 委員　　架空 三郎\n"
    "５ 事務局　架空 四郎\n"
)


def test_名簿を次第として取ってしまわない() -> None:
    """
    実運用ログ: source 28,937 / agenda 809 / roster 0。
    名簿(5.4KB)が次第として持って行かれ、残り1件になって名簿が使われなかった。
    """
    pipe = Pipe()
    attachments = [
        ("出席者名簿.xlsx", ROSTER_LOOKS_LIKE_AGENDA),
        ("病院経営・運営会議.docx", TRANSCRIPT_TEXT),
    ]

    async def loaded(_files):
        return list(attachments)

    pipe._load_full_file_contents = loaded
    _, agenda, roster, source_name, source_text = asyncio.run(
        pipe._prepare_sources("議事録を作って", {}, [{"id": "x"}])
    )

    assert "架空 太郎" in roster, "名簿が使われていない"
    assert agenda == "", "名簿を次第として取っている"
    assert source_name == "病院経営・運営会議.docx"
    assert "会議を始めます" in source_text


def test_名簿を先に抜いてから次第を選ぶ() -> None:
    """順番が逆だと、次第の採点が先に名簿をさらっていく。"""
    import inspect

    source = inspect.getsource(Pipe._prepare_sources)
    assert source.index("_split_roster") < source.index("_split_agenda"), (
        "名簿より先に次第を選んでいる"
    )
