"""tools/open_webui_help_pipe.py の再発防止テスト。

Ollama や Open WebUI には接続しません。
_answer が送る HTTP ペイロードだけが検証対象です。

背景(2026-08-18):
  - 「たまに本文が空」の理由 = gemma4 の思考分が num_ctx の残りを
    使い切り、done_reason=length で本文が0字になる。
  - 「返事が遅い」の理由 = 本文を書く前に思考を生成される。
  両方ともペイロードに ``"think": false`` が入らなかったことが
    tools/excel_comment_build.py (2026-08-17実測、同様の障害) と
    ヘルプPipe の A/B 実測(11.5s/1165字思考 → 1.8s/0字思考)で確定。

  本ファイルは「ペイロードに think: false が入っていること」を
  検証するためだけにある。思考を抑制すると思考が本文を消費しないため
  num_ctx は資料全文+質問+指示を保持できる。
"""

from __future__ import annotations

import asyncio
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import open_webui_help_pipe as pipe_mod
from open_webui_help_pipe import Pipe


class _FakeResponse:
    def __init__(self, body: dict) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._body


class _FakeAsyncClient:
    """httpx.AsyncClient の置き換え。POST されたペイロードを self へ保持。"""

    def __init__(self) -> None:
        self.captured: dict | None = None
        self.sent_url: str | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def post(self, url: str, json: dict | None = None, **kwargs):
        self.sent_url = url
        self.captured = json
        return _FakeResponse({"message": {"content": "ok"}})


def _run_answer(pipe: Pipe, prompt: str = "質問です") -> tuple[str, _FakeAsyncClient]:
    """Ollama非接続で _answer を1回実行し、(回答, 捕捉したペイロード保持用のfake)を返す。"""
    fake = _FakeAsyncClient()
    with patch.object(pipe_mod.httpx, "AsyncClient", return_value=fake):
        answer = asyncio.run(pipe._answer(prompt))
    return answer, fake


def test_answer_returns_content_field() -> None:
    """_answer はレスポンスの message.content を返す(思考を本文にしてはいけない)。"""
    pipe = Pipe()
    answer, _ = _run_answer(pipe)
    assert answer == "ok"


def test_answer_payload_disables_thinking() -> None:
    """think を指定しないと思考が本文より先に生成される(2026-08-17実測、excel_comment_build)。"""
    pipe = Pipe()
    _, fake = _run_answer(pipe)
    assert fake.captured is not None
    assert fake.captured["think"] is False, (
        "_answer のペイロードに 'think': false が無い。"
        "gemma4 は思考を先に生成し、長い資料で done_reason=length になる"
        "(tools/excel_comment_build.py:320 の前例を参照)"
    )


def test_answer_payload_targets_chat_api() -> None:
    pipe = Pipe()
    _, fake = _run_answer(pipe)
    assert fake.sent_url is not None
    assert fake.sent_url.endswith("/api/chat")


def test_answer_num_ctx_covers_max_context() -> None:
    """思考を抑制した num_ctx が、資料全文+指示+質問(文字数として)に足りる。

    日本語のトークン化で悲観的に 1トークン=2.5字 をとる。
    1トークン=1字 の最悪ケースでも num_ctx が chars 以下だと
    本文が done_reason=length で切られる。
    """
    pipe = Pipe()
    _, fake = _run_answer(pipe)
    num_ctx = fake.captured["options"]["num_ctx"]
    chars = pipe.valves.MAX_CONTEXT_CHARS
    assert num_ctx >= chars / 2.5, (
        f"num_ctx={num_ctx} は MAX_CONTEXT_CHARS={chars} を(1トークン=2.5字で)持てない。"
        "思考を抑制しても本文が切られ、空回答が再発する"
    )


def test_answer_num_ctx_not_below_known_working_value() -> None:
    """前例 tools/excel_comment_build.py が検証済みの num_ctx=16384 を下回ってはならない。

    ヘルプPipe は資料の方が長いので、それより小さい値へ後退する
    回帰は「空回答/遅延が再発する」ことと同じである。
    """
    pipe = Pipe()
    _, fake = _run_answer(pipe)
    assert fake.captured["options"]["num_ctx"] >= 16384


# ---------------------------------------------------------------------------
# 参照元をエクスプローラーへ貼れるフルパスで出す
# ---------------------------------------------------------------------------

ROOT = r"D:\minutes-pipeline"


def _path(name: str, root: str = ROOT) -> str:
    return Pipe._source_path(name, root)


def test_平坦な名前をフルパスに戻す() -> None:
    assert _path("tools__office_excel.py") == r"D:\minutes-pipeline\tools\office_excel.py"
    assert _path("AGENTS.md") == r"D:\minutes-pipeline\AGENTS.md"
    assert _path("templates__gui__README.md") == r"D:\minutes-pipeline\templates\gui\README.md"


def test_名前そのものにアンダースコア2つを含むファイルは戻さない() -> None:
    """local_tool_bridge/__init__.py は区切りか名前かを見分けられない。

    存在しない場所を探させるくらいなら、平坦な名前のまま出す。
    """
    flat = "local_tool_bridge____init__.py"
    assert _path(flat) == flat


def test_単独のアンダースコアは区切りと誤解しない() -> None:
    assert _path("text-processing-bridge__app__render_minutes.py") == (
        r"D:\minutes-pipeline\text-processing-bridge\app\render_minutes.py"
    )


def test_置き場が空なら平坦な名前のまま出す() -> None:
    assert _path("tools__doctor.py", "") == "tools__doctor.py"


def test_末尾の区切り記号があっても二重にならない() -> None:
    assert _path("tools__doctor.py", "D:\\minutes-pipeline\\") == (
        r"D:\minutes-pipeline\tools\doctor.py"
    )


# ----------------------------------------------------------------------
# 「どう頼めばいいか」への依頼文づくり(2026-08-18に追加)
# ----------------------------------------------------------------------


def test_頼み方を聞かれたときだけ依頼文モードになる() -> None:
    当たる = [
        "Excelにコメントを入れたいのですが、どう頼めばいいですか",
        "議事録を作ってもらうときの頼み方を教えてください",
        "文面を考えてください",
        "なんて言えばいいですか",
    ]
    当たらない = [
        "添付したExcelにコメントを入れてください",
        "議事録を作る手順を教えてください",
        "コメント生成はどのファイルに書かれていますか",
    ]
    for text in 当たる:
        assert pipe_mod.REQUEST_TEXT_PATTERN.search(text), text
    for text in 当たらない:
        assert not pipe_mod.REQUEST_TEXT_PATTERN.search(text), text


def test_依頼文から前置きと囲みを落とす() -> None:
    """LLMは頼んでいない前置き・コードブロック・かぎ括弧を付けてくる。"""
    生 = (
        "以下のように頼んでください:\n"
        "```\n"
        "「添付したExcelの全対象シートにコメントを入れてください。」\n"
        "```"
    )
    assert (
        Pipe._clean_request_text(生)
        == "添付したExcelの全対象シートにコメントを入れてください。"
    )


def test_依頼文は改行を畳んで長さを切る() -> None:
    整形後 = Pipe._clean_request_text("あ\nい\nう")
    assert 整形後 == "あ い う"
    assert len(Pipe._clean_request_text("あ" * 500)) == pipe_mod.REQUEST_TEXT_MAX_CHARS


def test_整形して何も残らなければ空を返す() -> None:
    """空の依頼文をリンクへ入れると、押した先で何も起きない。"""
    assert Pipe._clean_request_text("```\n```") == ""


def test_作った依頼文がリンクの入力欄へ入る() -> None:
    """固定文ではなく、その場で作った文が ?q= に載ること。"""
    pipe = Pipe()
    route = pipe_mod.ROUTES[0]
    依頼文 = "添付したブックの入院シートだけにコメントを入れてください。"
    block = pipe._handover(route, 依頼文)
    assert urllib.parse.quote_plus(依頼文) in block
    assert urllib.parse.quote_plus(route["prompt"]) not in block
    # 文面を渡さないときは今までどおり固定文
    assert urllib.parse.quote_plus(route["prompt"]) in pipe._handover(route)



def test_Kilo宛は宣言したときだけ() -> None:
    """語から意図を当てにいくと、不具合報告が業務依頼へ吸われる(2026-08-18に実測)。"""
    当たる = ["kilo: コメントが途中で止まるので直してほしい", "Kilo：Wordを読む機能を足して"]
    当たらない = [
        "コメントが途中で止まるので直してほしい",   # 宣言が無い
        "Kiloってなんですか",                       # 語が出るだけ
        "議事録の頼み方を教えて",
    ]
    for text in 当たる:
        assert pipe_mod.KILO_TRIGGER_PATTERN.match(text), text
    for text in 当たらない:
        assert not pipe_mod.KILO_TRIGGER_PATTERN.match(text), text


def test_書式の質問は宣言があってもKilo宛にしない() -> None:
    """ひな型Excelを直すだけの話に「コードを直せ」と言わせない(実測した誤り)。"""
    assert Pipe._not_a_job("kilo: コメントの文字サイズを変えたい")
    assert not Pipe._not_a_job("kilo: コメントが途中で止まるので直してほしい")


def test_宣言の合図は資料検索とLLMへ渡さない() -> None:
    assert (
        pipe_mod.KILO_TRIGGER_PATTERN.sub("", "kilo: Wordを読む機能を足して").strip()
        == "Wordを読む機能を足して"
    )


def test_Kilo宛の案内にはリンクを出さない() -> None:
    """?models= の行き先が無い相手にリンクを出すと、押しても何も起きない。"""
    block = Pipe._kilo_handover()
    assert "http" not in block and "VS Code" in block and "承認" in block


def test_モデルの説明文は決めた文面から動かさない() -> None:
    """
    利用者が読む唯一の説明。別セッションやモデルの手直しで静かに変わると、
    「ここで直る」という誤解(2026-08-17に実際に起きた)へ戻る。
    変えるときはこのテストごと直すこと。
    """
    from open_webui_deploy import parse_frontmatter

    source = (Path(__file__).with_name("open_webui_help_pipe.py")).read_text(encoding="utf-8")
    assert parse_frontmatter(source)["model_description"] == (
        "「議事録を作りたい」「これできる?」「どこに何がある?」など、そのまま聞いてください。"
        "直してほしいこと・足してほしいことは、先頭に「kilo:」と書くと、"
        "開発担当へ渡す依頼文を作ります(このヘルプでは直せません)。"
    )


def test_Kilo宛の依頼文は用件と検索結果だけで作る() -> None:
    """
    LLMに書かせると資料の例文を丸写しする(2026-08-18に実発生)。
    出来上がる文に、入力していない語が混ざらないことを固定する。
    """
    pipe = Pipe()
    pipe.valves.REPO_ROOT_PATH = r"D:\minutes-pipeline"
    found = [
        ("tools__excel_comment_build.py", "本文は使わない"),
        ("AGENTS.md", "本文は使わない"),
    ]
    text = pipe._kilo_request("コメントの文章を大きく変えたい", found)
    assert text.startswith("コメントの文章を大きく変えたい")
    assert r"D:\minutes-pipeline\tools\excel_comment_build.py" in text
    assert r"D:\minutes-pipeline\AGENTS.md" in text
    assert "doctor.py" in text
    # 資料に載っている定型文(Excel分析用)を持ち込まない
    assert "表示中のコメント対象シート" not in text
    assert ".xlsx" not in text


def test_Kilo宛は資料の本文をLLMへ渡さない() -> None:
    """組み立てに使うのはファイル名だけ。本文の言い回しが混ざる余地を残さない。"""
    pipe = Pipe()
    pipe.valves.REPO_ROOT_PATH = ""
    text = pipe._kilo_request("直してほしい", [("AGENTS.md", "秘密の本文XYZ")])
    assert "秘密の本文XYZ" not in text


def test_資料の例文を写したら不合格にする() -> None:
    """2026-08-18に実発生: Excel分析用の定型文をそのまま返してきた。"""
    写経 = pipe_mod.ROUTES[0]["prompt"]
    理由 = Pipe._verify_kilo_request(写経, "コメントの文章を変えたい", [])
    assert 理由 is not None and "写しています" in 理由


def test_質問にも資料にも無いファイル名は不合格にする() -> None:
    """2026-08-18に実発生: 存在しない input\\実績.xlsx を足してきた。"""
    理由 = Pipe._verify_kilo_request(
        r"input\実績.xlsx を直してください",
        "コメントの文章を変えたい",
        [r"D:\minutes-pipeline\tools\excel_comment_build.py"],
    )
    assert 理由 is not None and "実績.xlsx" in 理由


def test_既知のファイルから派生した名前は通す() -> None:
    """
    「変更後にtest_excel_comment_build.pyを流して」は正当な依頼。
    日本語を含めて1語に拾ってしまうため、既知のファイル名を含むかで見る
    (2026-08-18に、正しい生成文をこの検査で誤って落とした)。
    """
    assert (
        Pipe._verify_kilo_request(
            "tools/excel_comment_build.pyの文体整形を変更し、"
            "変更後にtest_excel_comment_build.pyを流してください。",
            "コメントの文章を変えたい",
            [r"D:\minutes-pipeline\tools\excel_comment_build.py"],
        )
        is None
    )


def test_資料にあるファイル名なら通す() -> None:
    assert (
        Pipe._verify_kilo_request(
            "tools/excel_comment_build.py の文章の作り方を変えてください。",
            "コメントの文章を変えたい",
            [r"D:\minutes-pipeline\tools\excel_comment_build.py"],
        )
        is None
    )
