"""
使い方ヘルプPipeの、ネットワーク不要な部分のテスト。

このPipeの肝は「検索をLLMに任せず自分で行い、渡した資料だけで答えさせる」こと。
そこが崩れると、Open WebUIのナレッジ機能に戻ったのと同じ(回答が空)になる。
"""

from __future__ import annotations

import asyncio

import open_webui_help_pipe
from open_webui_help_pipe import HELP_MESSAGE, INSTRUCTION, Pipe


def _suggestions() -> list[tuple[str, str]]:
    """docstringの suggestion: 行を (見出し, 質問文) にする。"""
    chips = []
    for line in (open_webui_help_pipe.__doc__ or "").splitlines():
        if line.startswith("suggestion:"):
            title, _, content = line[len("suggestion:") :].partition("|")
            heading, _, sub = title.partition("|")
            chips.append((heading.strip() or sub.strip(), content.strip()))
    return chips


def test_提案チップの見出しと質問文が食い違わない() -> None:
    """
    「どこに何がある?」を押したら開発者向けの質問文が入る状態だった(利用者から指摘)。

    見出しは利用者の言葉、質問文はその見出しどおりの内容にする。
    """
    chips = _suggestions()

    assert len(chips) >= 4
    for heading, content in chips:
        assert content.endswith(("か", "い", "？", "?")), content
        # 内部識別子をそのまま質問文に出さない
        assert "excel_comment" not in content

    headings = [heading for heading, _ in chips]
    assert "こんなことできる?" in headings, "できることを探す入口が必要"
    assert "どこに何がある?" in headings, "場所を調べる入口が必要"


def test_背景タスクには定型で応える() -> None:
    assert asyncio.run(Pipe().pipe({}, __task__="title_generation")) == "使い方ヘルプ"


def test_空の質問にはヘルプを返す() -> None:
    assert asyncio.run(Pipe().pipe({"messages": []})) == HELP_MESSAGE


def test_最新の質問を履歴から取る() -> None:
    body = {
        "messages": [
            {"role": "user", "content": "古い質問"},
            {"role": "assistant", "content": "応答"},
            {"role": "user", "content": [{"type": "text", "text": "最新の質問"}]},
        ]
    }
    assert Pipe()._latest_user_message(body) == "最新の質問"


def test_資料は番号付きで質問と一緒に渡す() -> None:
    prompt = Pipe()._build_prompt(
        "議事録の作り方は?",
        [("AGENTS.md", "議事録の手順はこう"), ("tools__doctor.py", "診断ツール")],
    )

    assert "【資料1】AGENTS.md" in prompt
    assert "【資料2】tools__doctor.py" in prompt
    assert "【質問】\n議事録の作り方は?" in prompt
    # 指示が先頭に無いと、モデルが資料を無視して自分の知識で答える
    assert prompt.startswith(INSTRUCTION)


def test_資料が多すぎるときは上限で打ち切る() -> None:
    pipe = Pipe()
    pipe.valves.MAX_CONTEXT_CHARS = 120
    found = [(f"file{i}.md", "あ" * 100) for i in range(10)]

    prompt = pipe._build_prompt("質問", found)

    # 上限を超えて詰め込むと、モデルの文脈からあふれて回答が空になる
    assert "【資料2】" not in prompt
    assert "【資料1】file0.md" in prompt


def test_指示は資料の外を答えることを禁じている() -> None:
    assert "資料に書かれている内容だけ" in INSTRUCTION
    assert "推測せず" in INSTRUCTION
    # できるか否かを先に言わせる(利用者の要望)
    assert "できます" in INSTRUCTION and "今はありません" in INSTRUCTION
    # 場所は関数まで
    assert "関数" in INSTRUCTION


def test_検索がゼロ件なら作り話をさせない() -> None:
    pipe = Pipe()

    async def empty(_question):
        return []

    pipe._search = empty
    answer = asyncio.run(Pipe.pipe(pipe, {"messages": [{"role": "user", "content": "質問"}]}))

    assert "見つかりませんでした" in answer


def test_検索に失敗したら理由を出して止まる() -> None:
    pipe = Pipe()

    async def broken(_question):
        raise RuntimeError("ナレッジがありません")

    pipe._search = broken
    answer = asyncio.run(Pipe.pipe(pipe, {"messages": [{"role": "user", "content": "質問"}]}))

    assert "ナレッジがありません" in answer


# ------------------------------------------------------------------
# 担当への誘導(2026-08-17)
# 手順だけ答えると、このヘルプの中で処理されると読めてしまう
# ------------------------------------------------------------------


def test_議事録の相談は文章処理へ回す() -> None:
    for question in (
        "議事録作りたいんやけどどうしたらいいかな",
        "会議の文字起こしから議事録を作れますか",
        "この文章を要約したい",
        "ケバ取りだけしたい",
    ):
        route = Pipe()._route(question)
        assert route is not None, question
        assert route["model_id"] == "text_processing", question


def test_エクセルの相談はexcel分析へ回す() -> None:
    for question in (
        "四半期の各診療科別の収入エクセルにコメント入れたいんやけどどうすればいい?",
        "Excelの全シートにコメントを入れる手順は",
        "xlsxのシートを読みたい",
        # 語がぶつかる質問。対象がExcelなのでExcel側が正しい
        "エクセルの数値を要約してほしい",
    ):
        route = Pipe()._route(question)
        assert route is not None, question
        assert route["model_id"] == "excel_analysis", question


def test_場所を聞かれたときは誘導しない() -> None:
    """「どこに書かれているか」はヘルプ自身が答える仕事。"""
    assert Pipe()._route("使い方ヘルプはどのファイルで実装されていますか") is None


def test_誘導は切替先とリンクと添付を示す() -> None:
    pipe = Pipe()
    route = pipe._route("議事録を作りたい")

    banner = pipe._handover(route)

    assert "「文章処理」" in banner
    # この画面では処理されないと言い切る(利用者が誤解した点)
    assert "この使い方ヘルプの画面では処理できません" in banner
    assert "models=text_processing" in banner
    # 添付を先にしてほしいので、開いた瞬間に送信させない
    assert "submit=false" in banner
    assert "添付" in banner


def test_誘導は回答の一番上に出る() -> None:
    pipe = Pipe()

    async def found(_question):
        return [("HELP_KILO_BEGINNER.md", "本文")]

    async def answer(_prompt):
        return "できます。手順はこうです。"

    pipe._search = found
    pipe._answer = answer
    result = asyncio.run(
        Pipe.pipe(pipe, {"messages": [{"role": "user", "content": "議事録を作りたい"}]})
    )

    assert result.startswith(">"), "説明より先に切替先を出す"
    assert "「文章処理」" in result
    assert "できます。手順はこうです。" in result


def test_誘導先が無い質問には案内を足さない() -> None:
    pipe = Pipe()

    async def found(_question):
        return [("AGENTS.md", "本文")]

    async def answer(_prompt):
        return "そのファイルにあります。"

    pipe._search = found
    pipe._answer = answer
    result = asyncio.run(
        Pipe.pipe(
            pipe, {"messages": [{"role": "user", "content": "doctorはどこで実装されていますか"}]}
        )
    )

    assert not result.startswith(">")
    assert "でやります" not in result


def test_ヘルプ自身は作業しないと指示している() -> None:
    assert "あなた自身は作業をしません" in INSTRUCTION
    assert "文章処理" in INSTRUCTION and "Excel分析" in INSTRUCTION


def test_回答には必ず参照した資料を添える() -> None:
    pipe = Pipe()

    async def found(_question):
        return [("AGENTS.md", "本文"), ("AGENTS.md", "別の断片"), ("DEMO.md", "本文")]

    async def answer(_prompt):
        return "こうすればできます。"

    pipe._search = found
    pipe._answer = answer
    result = asyncio.run(Pipe.pipe(pipe, {"messages": [{"role": "user", "content": "質問"}]}))

    assert "こうすればできます。" in result
    assert "**参照した資料**" in result
    # エクスプローラーへ貼れるフルパスで出す(平坦な名前のままでは探せない)
    assert result.count(r"- `D:\minutes-pipeline\AGENTS.md`") == 1, "同じファイルは1回だけ"
    assert r"- `D:\minutes-pipeline\DEMO.md`" in result


def test_書式の変え方を聞かれたら誘導しない() -> None:
    """ひな型Excelを直す話。「Excel分析」へ送っても書式は変わらない。

    誘導すると入力欄へコメント生成の固定文が入り、案内どおり送ると
    フォントは変わらないままコメント生成が走る(2026-08-18に実発生)。
    """
    for question in (
        "エクセルのコメントの文字サイズとかフォントをいじりたいんやけど、どうしたらいい？",
        "コメントの書式を変えたい",
        "Excelのコメント欄のひな型はどこで直せますか",
        "コメントの罫線を変更したい",
    ):
        assert Pipe()._route(question) is None, question


def test_置き場所を聞かれたら誘導しない() -> None:
    for question in (
        "コメント生成はどのファイルに書かれていますか",
        "Excelのコメント処理はどこに実装されていますか",
        "議事録のレンダラーのソースを見たい",
    ):
        assert Pipe()._route(question) is None, question


def test_作業をやりたい質問は疑問形でも誘導する() -> None:
    """疑問形かどうかでは切らない。やりたいのが作業なら誘導が正しい。"""
    expected = {
        "議事録作りたいんやけどどうしたらいいかな": "text_processing",
        "会議の文字起こしから議事録を作れますか": "text_processing",
        "Excelの全シートにコメントを入れる手順は": "excel_analysis",
        "四半期の収入エクセルにコメント入れたいんやけどどうすればいい?": "excel_analysis",
    }
    for question, model_id in expected.items():
        route = Pipe()._route(question)
        assert route is not None, question
        assert route["model_id"] == model_id, question


# ----------------------------------------------------------------------
# ひな型の編集手順は決め打ちで出す(検索に頼らない)
# ----------------------------------------------------------------------


def test_書式の質問にはExcelのひな型手順を出す() -> None:
    guides = Pipe._guides("エクセルのコメントの文字サイズとかフォントをいじりたい")
    assert [g["key"] for g in guides] == ["excel_comment_style"]


def test_議事録のひな型には議事録の手順を出す() -> None:
    guides = Pipe._guides("議事録のひな型をいじりたいんやけど")
    assert [g["key"] for g in guides] == ["minutes_template"]


def test_どちらのひな型か分からない聞き方には両方出す() -> None:
    """「ひな型どこにあるっけ」で片方だけ出すと、探し物が入っていないことがある。"""
    guides = Pipe._guides("ひな型をいじりたいんやけどどこにあるっけ")
    assert len(guides) == len(open_webui_help_pipe.GUIDES)


def test_関係ない質問には手順を出さない() -> None:
    for question in (
        "議事録を作りたい",
        "Excelにコメントを入れてください",
        "結果が返ってこないときは何を見ればいい?",
    ):
        assert Pipe._guides(question) == [], question


def test_手順には開くファイルと打つコマンドがそのまま入る() -> None:
    """検索が手順を渡せなかったのが原因なので、ここで省略したら意味がない。"""
    excel = Pipe._guide_banner(Pipe._guides("コメントのフォントを変えたい"))
    assert "excel_comment_gui_template.xlsx" in excel
    assert "comment_template" in excel
    assert "列幅・行高" in excel, "できないことも書く"

    minutes = Pipe._guide_banner(Pipe._guides("議事録のテンプレートを直したい"))
    assert "minutes_gui_template.docx" in minutes
    assert "office_template.py check" in minutes
    assert "office_template.py deploy" in minutes
    # 引用ブロックとして出す(説明より先に目に入る)
    assert minutes.startswith("> ")


def test_手順は回答の一番上に出る() -> None:
    pipe = Pipe()

    async def found(_question):
        return [("templates__gui__README.md", "本文")]

    async def answer(_prompt):
        return "ひな型の場所についてお答えします。"

    pipe._search = found
    pipe._answer = answer
    result = asyncio.run(
        Pipe.pipe(
            pipe,
            {"messages": [{"role": "user", "content": "コメントの文字サイズを変えたい"}]},
        )
    )

    assert result.startswith("> "), "説明より先に手順を出す"
    assert "excel_comment_gui_template.xlsx" in result
    assert "ひな型の場所についてお答えします。" in result
