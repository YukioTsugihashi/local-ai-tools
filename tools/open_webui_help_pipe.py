"""
title: 使い方ヘルプ
id: hospital_help_pipe
author: local
version: 1.0.0
description: 院内ツールの使い方・できること・障害対処・コードの場所を、リポジトリのドキュメントとプログラム本体から引用付きで答える。検索はこのPipeが自分で行い、LLMには「渡した内容だけで答える」ことしかさせない
model_description: 「議事録を作りたい」「これできる?」「どこに何がある?」など、そのまま聞いてください。直してほしいこと・足してほしいことは、先頭に「kilo:」と書くと、開発担当へ渡す依頼文を作ります(このヘルプでは直せません)。
suggestion: こんなことできる? | 使える機能を探す | 会議の文字起こしから議事録を作りたいのですが、今できますか
suggestion: 議事録を作りたい | 文章処理の使い方 | 文字起こしから議事録を作る手順を教えてください
suggestion: Excelにコメント | Excel分析の使い方 | Excelの全対象シートに分析コメントを入れる手順を教えてください
suggestion: どこに何がある? | ファイルの場所を調べる | Excelにコメントを入れる処理は、どのファイルのどこに書かれていますか
suggestion: 直してほしいとき | 開発担当へ渡す文を作る | kilo: コメントが途中で止まるので直してほしい
suggestion: 動かないときは | 障害の切り分け | Excelにコメントを入れても結果が返ってきません。何を確認すればいいですか
requirements: httpx
"""

# なぜPipeにしたか(2026-08-17):
# Open WebUIのモデルへナレッジを付ける方式は、この版だと
# 「モデルが検索ツールを自分で呼ぶ」形になり、呼んだところで実行されずに
# 本文が空のまま返ってきていた(利用者の画面には「●」だけが出ていた)。
# ツールを切ると今度は検索が一切走らず「資料に記載がありません」になる。
# どちらに倒しても使えないため、**検索をこちらで確定的に行う**方式へ変えた。
# 文章処理・Excel分析パイプと同じ考え方(LLMに振り分けを任せない)。

from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx
from pydantic import BaseModel, Field

EventEmitter = Callable[[dict[str, Any]], Awaitable[None]]

# 担当の振り分け。ここもLLMに任せず正規表現で決める。
# 手順だけ答えると「このヘルプの中でやってもらえる」と読めてしまい、
# 実際には何も起きない(利用者から指摘、2026-08-17)。
# Excelを先に置く。「エクセルの数値を要約して」のように語がぶつかる質問は、
# 対象がExcelである側が正しいため。
ROUTES = [
    {
        "model_id": "excel_analysis",
        "model_name": "Excel分析",
        "pattern": r"エクセル|ｴｸｾﾙ|excel|xlsx|xlsm|シート|セル|ブック|表に|数値|コメント",
        "attach": "コメントを入れたいExcelファイルを添付する",
        "prompt": "表示中のコメント対象シートをすべて処理してください。"
        "ブック内の表と記入例を使い、数値を検証し、完成品と確認一覧をoutputへ保存してください。",
    },
    {
        "model_id": "text_processing",
        "model_name": "文章処理",
        "pattern": r"議事録|要約|ようやく|ケバ|けば|フィラー|整形|文字起こし|書き起こし|"
        r"話し言葉|敬語|文章|文書|テキスト|録音",
        "attach": "文字起こしファイルを添付する(会議次第・出席者名簿もあれば一緒に)",
        "prompt": "文字起こし、会議次第、出席者名簿を使って議事録を作ってください。"
        "数字、金額、期限を省略せず、Wordで出してください。",
    },
]

# 名詞(エクセル・コメント…)は当たるが、頼まれているのは作業ではない質問。
# 誘導すると入力欄へ無関係な固定文が入り、案内どおり送ると
# 「書式を変えたいのにコメント生成が走る」ことになる(2026-08-18に実発生)。
NOT_A_JOB = (
    # 書式・見た目の変え方。直すのは templates/gui のひな型Excelであって、
    # 「Excel分析」に投げても何も変わらない
    r"文字サイズ|フォント|書式|ひな型|雛形|テンプレ|見た目|太字|斜体|"
    r"罫線|塗りつぶし|列幅|行高|色を(変|付|つ)",
    # 置き場所・実装を聞く質問。コードの案内はヘルプ自身の仕事
    r"どのファイル|どこに(書|実装|定義|ある)|実装され|定義され|ソース|どこですか",
)

# 「どう頼めばいいか」と聞かれたとき。作業そのものの依頼ではないので、
# 固定文を入力欄へ入れるのではなく、その人の状況に合わせた依頼文を作って渡す。
# 判定は正規表現(振り分けはLLMに任せない方針どおり)。LLMに書かせるのは
# 文面だけで、処理はさせない。
REQUEST_TEXT_PATTERN = re.compile(
    r"どう(頼|たの|お願い|言え|伝え)|頼み方|頼むには|お願いの仕方|依頼文|文面|"
    r"なんて(言|書)|どのように(頼|お願い|伝え)|指示の(仕方|出し方)",
    re.IGNORECASE,
)
# 依頼文の前後に付きがちな囲み。LLMはかぎ括弧や引用符で包むことがある
REQUEST_TEXT_QUOTES = '「」『』"' + "'" + "　 "

# Kilo(開発)宛の依頼文は、**利用者が宣言したときだけ**作る。
# 語から意図を当てにいく方式は2026-08-18に実装して取り下げた: 「直してほしい」と
# 「Excelにコメントを入れて」は同じ語彙を使うため切り分けられず、不具合報告が
# Excel分析へ吸われ、書式の質問には「コードを直せ」という誤った依頼文を作った。
# 宣言方式なら判定が要らない。行き先を決めるのは利用者である。
KILO_TRIGGER_PATTERN = re.compile(r"^\s*kilo\s*[:：]", re.IGNORECASE)

# Kilo宛はコードブロックへ出すので、URLに載せる文より長くてよい
KILO_REQUEST_MAX_CHARS = 400
# 検証に落ちたときの作り直し回数。使い切ったら確定版へ退避する
KILO_REQUEST_TRIES = 3
# 資料の例文の丸写しとみなす一致長
KILO_COPY_WINDOW = 24

# 依頼文の中でファイル名とみなす形。日本語も拾う(実績.xlsx のような創作を
# 逃さないため)。そのぶん「修正後にtest_x.py」のように前の語まで1語として
# 拾ってしまうので、既知のファイル名を含むかどうかで判定する(下の検証を参照)
FILE_TOKEN_PATTERN = re.compile(
    r"[\w./\\-]+\.(?:py|md|ts|xlsx|xlsm|docx|jsonc?|ya?ml)", re.IGNORECASE
)

# 入力欄へ入れる文の上限。長いとURLに載らず、送る前に目で確認もできない
REQUEST_TEXT_MAX_CHARS = 200

# 検索に頼らず決め打ちで出す手順。
#
# ひな型の編集は「どのファイルを開き、何をして、どう反映するか」が全部言えないと
# 役に立たない。ところがREADMEは見出しと本文が別々の断片に割れており、
# 見出しだけの96文字が検索に当たって手順もコマンドも渡らないことがある
# (2026-08-18に実測)。検索運に左右される情報ではないので、ここに固定で持つ。
# 手順を変えたときは templates/gui/README.md と一緒に直すこと。
GUIDES = [
    {
        "key": "excel_comment_style",
        # 話題(Excel・コメント)と、書式まわりの語の両方が出たとき
        "topic": r"コメント|excel|エクセル|xlsx|xlsm",
        "about": r"文字サイズ|フォント|書式|見た目|太字|斜体|下線|色|罫線|塗り|配置|ひな型|雛形|テンプレ",
        "title": "Excelコメントの書式を変える",
        "steps": (
            r"**このファイルをExcelで開く**"
            "\n>    `D:\\minutes-pipeline\\templates\\gui\\excel_comment_gui_template.xlsx`",
            "**`comment_template` という名前のセルの書式を変える**"
            "\n>    (場所が分からないときは 数式タブ → 名前の管理 で確認できます)",
            "**上書き保存する。作業はこれで終わりです**"
            "\n>    コードの変更・再起動・反映コマンドは要りません",
            "**「Excel分析」でコメントを入れ直すと、新しい書式で出ます**",
        ),
        "notes": (
            "反映される: フォント・サイズ・色・太字/斜体/下線・折返し・配置・罫線・塗りつぶし",
            "反映されない: 列幅・行高(セルの書式ではなく列・行の属性のため。"
            "書き込み先のExcelで直接調整してください)",
        ),
    },
    {
        "key": "minutes_template",
        "topic": r"議事録|会議録|docx|word|ワード",
        "about": r"ひな型|雛形|テンプレ|書式|フォント|文字サイズ|見た目|色|タイトル|レイアウト",
        "title": "議事録(Word)のひな型を変える",
        "steps": (
            r"**このファイルをWordで開く**"
            "\n>    `D:\\minutes-pipeline\\templates\\gui\\minutes_gui_template.docx`",
            "**書式だけ変えて保存する**"
            "\n>    **行の追加・削除はしないでください。**レンダラーが行をひな型として使うため壊れます",
            "**リポジトリ直下で、上から順に実行する**"
            "\n>    ```\n"
            ">    text-processing-bridge\\.venv\\Scripts\\python.exe tools\\office_template.py check   templates\\gui\\minutes_gui_template.docx\n"
            ">    text-processing-bridge\\.venv\\Scripts\\python.exe tools\\office_template.py preview templates\\gui\\minutes_gui_template.docx\n"
            ">    text-processing-bridge\\.venv\\Scripts\\python.exe tools\\office_template.py deploy  templates\\gui\\minutes_gui_template.docx\n"
            ">    ```",
            "**preview の結果を目で見てから deploy する**"
            "\n>    preview は架空データのサンプルを `output\\minutes_template_preview.docx` に作ります",
        ),
        "notes": (
            "check は行の欠落など構造の破壊を見つけます。通らなければ deploy できません",
            "deploy は Open WebUI へ即時反映されます(再起動は要りません)",
            r"壊したときは `D:\minutes-pipeline\templates\original\` の原本を "
            "`minutes_gui_template.docx` へコピーし直せば戻ります",
        ),
    },
]

HELP_MESSAGE = (
    "院内AIシステムの使い方を答えます。次のようなことが聞けます。\n\n"
    "- **できることを探す**: 「会議の録音から議事録を作れますか」\n"
    "- **手順**: 「Excelにコメントを入れる手順を教えて」\n"
    "- **場所**: 「コメント生成はどのファイルに書かれていますか」\n"
    "- **困ったとき**: 「結果が返ってこないときは何を見ればいい?」\n"
    "- **開発を頼む文を作る**: 先頭に `kilo:` を付けて用件を書くと、"
    "VS CodeのKiloへ貼れる依頼文を作ります\n\n"
    "答えの根拠にしたファイル名は必ず一緒に出します。"
)

# LLMへ渡す指示。事実はすべて検索結果の中にあり、LLMは書き換えない。
INSTRUCTION = (
    "あなたは院内ローカルAIシステムの案内係です。必ず日本語で、"
    "相手が非エンジニアでも分かる言葉で答えます。\n"
    "**あなた自身は作業をしません。**議事録の作成やExcelへの書き込みは、"
    "それぞれ担当のモデル(「文章処理」「Excel分析」)に切り替えて行います。"
    "手順を答えるときは、どのモデルに切り替えるかを必ず書いてください。"
    "この画面のまま処理が進むかのような書き方はしないでください。\n"
    "以下の【資料】は、このシステムのドキュメントとプログラム本体から"
    "質問に関係する箇所を抜き出したものです。**資料に書かれている内容だけ**を"
    "根拠にしてください。資料に無いことは推測せず、"
    "「資料に記載がありません。VS CodeのKiloで調べてください」と答えます。\n"
    "答え方:\n"
    "- 「〇〇できるか」と聞かれたら、「できます」「今はありません」を先に言い、"
    "続けて手順か代案を出します。\n"
    "- 「どこにある」と聞かれたら、ファイルのパスと、その中のどの関数かまで示します。\n"
    "- 手順は資料にあるコマンドをそのまま書きます。創作してはいけません。\n"
    "- 資料のファイル名(【資料N】の見出し)を本文中で挙げて、根拠を示します。"
)


# 依頼文を作るときだけ INSTRUCTION と差し替える。答えではなく「送れる文」を作らせる。
REQUEST_INSTRUCTION = (
    "あなたは院内ローカルAIシステムの案内係です。"
    "利用者は「どう頼めばよいか」を聞いています。"
    "担当のモデルへ**そのまま送れる依頼文**を1つだけ書いてください。\n"
    "守ること:\n"
    "- 出力は依頼文の本文だけ。前置き・見出し・箇条書き・かぎ括弧は付けない\n"
    "- 3文以内、200字以内。です・ます調の日本語で書く\n"
    "- 以下の【資料】にある機能の範囲だけで書く。資料に無い操作を書かない\n"
    "- 添付が要る作業なら「添付した」と書く。"
    "出力形式(Word・Excelなど)が資料にあれば入れる\n"
    "- 質問に条件(シート名・期間・様式など)があれば必ず反映する\n"
    "- 依頼文は担当のモデルへ**宛てた**文にする。"
    "「〜モデルに切り替えて」「〜に依頼してください」のような利用者への案内は"
    "入れない(切り替えの案内は画面側で出している)\n"
    "- あなた自身は処理をしません。依頼文の中で作業を実行しようとしないでください"
)


# 宛先がKilo(このシステムを直す担当)のときの指示。
# 「資料の例文を写すな」「質問に無いファイル名を書くな」は指示にも書くが、
# 指示だけでは守られないため下の検証で機械的に落とす(2026-08-18の実測による)。
KILO_REQUEST_INSTRUCTION = REQUEST_INSTRUCTION + (
    "\n- 宛先はVS CodeのKiloです。利用者の用件を、実装担当が動ける形に書き直す\n"
    "- 直す対象のファイルが資料から分かるなら書く。**資料に無いファイル名は書かない**\n"
    "- 資料に載っている依頼文の例をそのまま写さない。用件はあくまで質問の内容\n"
    "- 変更を頼むときは、確認方法(テストを流す・doctorを見る)まで入れる"
)


class Pipe:
    class Valves(BaseModel):
        KNOWLEDGE_NAME: str = Field(
            default="院内ツールヘルプ",
            description="検索対象のナレッジ名(open_webui_help_sync.py が作る)",
        )
        OLLAMA_BASE_URL: str = Field(
            default="http://host.docker.internal:11434",
            description="OllamaのベースURL",
        )
        EMBEDDING_MODEL: str = Field(
            default="bge-m3:latest",
            description="質問をベクトル化するモデル(ナレッジ登録時と同じものにする)",
        )
        ANSWER_MODEL: str = Field(
            default="gemma4:26b", description="回答を書くモデル"
        )
        TOP_K: int = Field(default=10, description="検索で取り出す断片の数")
        MAX_CONTEXT_CHARS: int = Field(
            default=24000, description="LLMへ渡す資料の合計文字数の上限"
        )
        TIMEOUT_SECONDS: int = Field(default=900, description="LLM呼び出しの待ち時間")
        WEB_UI_URL: str = Field(
            default="http://localhost:3000",
            description="利用者のブラウザから見たOpen WebUIのURL(案内リンクに使う)",
        )
        REPO_ROOT_PATH: str = Field(
            default="D:\\minutes-pipeline",
            description="資料の置き場。参照元をフルパスで出すために使う(空にすると平坦な名前のまま)",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    # ------------------------------------------------------------------
    # 入力
    # ------------------------------------------------------------------
    @staticmethod
    def _content_to_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                str(item.get("text"))
                for item in content
                if isinstance(item, dict) and item.get("type") == "text" and item.get("text")
            )
        return ""

    def _latest_user_message(self, body: dict) -> str:
        for message in reversed(body.get("messages") or []):
            if isinstance(message, dict) and message.get("role") == "user":
                text = self._content_to_text(message.get("content"))
                if text.strip():
                    return text
        return ""

    # ------------------------------------------------------------------
    # 担当の振り分け(LLMに決めさせない)
    # ------------------------------------------------------------------
    @staticmethod
    def _not_a_job(question: str) -> bool:
        """名詞は当たるが、頼まれているのが作業ではない質問(書式・置き場所)。"""
        return any(re.search(pattern, question, re.IGNORECASE) for pattern in NOT_A_JOB)

    @staticmethod
    def _route(question: str) -> Optional[dict]:
        """
        質問がどのモデルの仕事かを決める。当てはまらなければNone。

        「議事録作りたいんやけどどうしたらいい」のような疑問形でも、
        やりたいのは作業なので誘導する(疑問形かどうかでは切らない)。
        切るのは NOT_A_JOB に当たるとき。
        """
        if Pipe._not_a_job(question):
            return None
        best: Optional[dict] = None
        best_hits = 0
        for route in ROUTES:
            hits = len(set(re.findall(route["pattern"], question, re.IGNORECASE)))
            if hits > best_hits:
                best, best_hits = route, hits
        return best

    @staticmethod
    def _source_path(name: str, root: str) -> str:
        """
        ナレッジ内の平坦な名前を、エクスプローラーへ貼れるフルパスへ戻す。

        同期側(open_webui_help_sync.upload_name)が相対パスの `/` を `__` に
        している。ただし `__init__.py` のように名前そのものに `__` を含む
        ファイルは元に戻せない(区切りか名前かを見分けられない)。
        その場合は平坦な名前のまま出す。**間違ったパスを出すくらいなら
        出さないほうがよい** — 存在しない場所を探させることになるため。

        ブラウザは http のページから file:// へ移動できない(黙って無視される)
        ので、押して開くリンクにはしない。貼り付けられる文字列として出す。
        """
        if not root:
            return name
        parts = name.split("__")
        if any(not part for part in parts):
            return name
        return root.rstrip("\\/") + "\\" + "\\".join(parts)

    @staticmethod
    def _clean_request_text(text: str, limit: int = REQUEST_TEXT_MAX_CHARS) -> str:
        """
        生成された依頼文を、入力欄へそのまま入れられる1行の文へ整える。

        頼んでいなくても前置き(「以下のように頼んでください:」)やコードブロック、
        かぎ括弧を付けてくることがある。?q= に載せる文字列なので改行は畳み、
        長さも切る。整形して何も残らなければ空を返し、呼び出し側がいつもの説明へ
        落とす(空の文を入力欄へ入れると、押した先で何も起きない)。
        """
        lines = [line.strip() for line in text.strip().splitlines()]
        # コードブロックの囲い行は落とす(前置きの次の行に来ることが多い)
        lines = [
            line for line in lines if line and not re.fullmatch(r"```[a-zA-Z]*", line)
        ]
        # 「〜:」で終わる行は前置き。本文が残るときだけ落とす
        while len(lines) > 1 and re.search(r"[::]$", lines[0]):
            lines.pop(0)
        cleaned = " ".join(lines).strip(REQUEST_TEXT_QUOTES + "`")
        return cleaned[:limit]

    @staticmethod
    def _guides(question: str) -> list[dict]:
        """
        決め打ちの手順を出すか決める。

        話題(Excel・議事録)と、書式まわりの語の両方が出たらその1件。
        どちらのひな型か分からない聞き方(「ひな型どこにあるっけ」)なら
        両方出す。探しているものが必ずどちらかに入っているようにする。
        """
        matched = [
            guide
            for guide in GUIDES
            if re.search(guide["topic"], question, re.IGNORECASE)
            and re.search(guide["about"], question, re.IGNORECASE)
        ]
        if matched:
            return matched
        if re.search(r"ひな型|雛形|テンプレ", question) and re.search(
            r"どこ|場所|いじ|変え|直し|編集", question
        ):
            return list(GUIDES)
        return []

    @staticmethod
    def _guide_banner(guides: list[dict]) -> str:
        """LLMには書かせない手順。押す場所と打つコマンドを省略せずに出す。"""
        blocks: list[str] = []
        for guide in guides:
            lines = [
                f"> **{guide['title']}**",
                "> この画面では変えられません。下のファイルを直接編集します。",
                ">",
            ]
            for number, step in enumerate(guide["steps"], start=1):
                lines.append(f"> {number}. {step}")
            if guide.get("notes"):
                lines.append(">")
                lines.extend(f"> - {note}" for note in guide["notes"])
            blocks.append("\n".join(lines))
        return "\n>\n".join(blocks) + "\n"

    @staticmethod
    def _verify_kilo_request(text: str, asked: str, places: list[str]) -> Optional[str]:
        """
        生成された依頼文を機械で検める。不合格ならその理由を返す。

        2026-08-18に実測した2つの壊れ方を落とす:
          - 資料に載っている依頼文の例(ROUTESの固定文)の丸写し
          - 質問にも検索結果にも無いファイル名の創作
        指示で禁じても守られなかったため、ここで機械的に落として作り直させる
        (excel_comment の「検証して不合格なら再試行、最後は下書きへ退避」と同じ形)。
        """
        known = (asked + " " + " ".join(places)).lower().replace(chr(92), "/")
        for token in FILE_TOKEN_PATTERN.findall(text):
            name = token.lower().replace(chr(92), "/").lstrip("./")
            # 既知のファイル名を含む名前(test_x.py に対する x.py など)は認める。
            # 派生ファイルを頼むのは正当で、創作とは別物
            if name in known:
                continue
            # 既知のファイル名を含む名前(x.py に対する test_x.py など)は認める。
            # 派生ファイルを頼むのは正当で、創作とは別物
            if any(Path(place).name.lower() in name for place in places if place):
                continue
            return f"質問にも資料にも無いファイル名が入っています: {token}"
        for route in ROUTES:
            sample = route["prompt"]
            windows = len(sample) - KILO_COPY_WINDOW + 1
            for start in range(max(1, windows)):
                if sample[start : start + KILO_COPY_WINDOW] in text:
                    return "資料に載っている依頼文の例を写しています"
        return None

    def _kilo_request(self, asked: str, found: list[tuple[str, str]]) -> str:
        """
        Kilo宛の依頼文を、LLMを使わずに組み立てる。

        文面をLLMに書かせると、資料に載っている例文をそのまま写す
        (2026-08-18に実発生: 「コメントの文章を変えたい」に対して、
        HELP_KILO_BEGINNER.md にあるExcel分析用の定型文を丸写しし、
        存在しないブック名まで足して返した)。用件は利用者の文をそのまま使い、
        手がかりは検索結果をそのまま並べる。創作の入り込む余地を無くす。
        Kiloは用件から自分で調べられる相手なので、作り込む必要もない。
        """
        root = self.valves.REPO_ROOT_PATH
        places: list[str] = []
        for name, _ in found:
            path = self._source_path(name, root)
            if path not in places:
                places.append(path)
        lines = [asked, "", "関係しそうな場所(使い方ヘルプの検索結果):"]
        lines += [f"- {path}" for path in places[:5]]
        lines += ["", "変更したら、テストと python tools\\doctor.py を流して確認してください。"]
        return "\n".join(lines)

    @staticmethod
    def _kilo_handover() -> str:
        """
        Kiloへは貼り付けで渡す。リンクにはできない(VS Codeの拡張であって
        Open WebUIのモデルではないため、?models= の行き先が無い)。
        """
        return (
            "> **これはVS CodeのKilo(開発担当)に頼む内容です。**"
            "この画面からは実行できません。\n>\n"
            "> 1. VS Codeを開いてKiloのチャットを出す\n"
            "> 2. 下の文をコピーして貼る\n"
            "> 3. 差分の承認を求められたら、中身を見てから承認する\n"
        )

    def _handover(self, route: dict, prompt: Optional[str] = None) -> str:
        """
        切り替え先を先頭で言い切る案内。LLMには書き換えさせない。

        ?models= でモデルを選び、?q= で入力欄に文面を入れる。submit=false は
        送信しない指定。添付を先にしてもらう必要があるため必ず付ける。
        """
        query = urllib.parse.urlencode(
            {
                "models": route["model_id"],
                "q": prompt or route["prompt"],
                "submit": "false",
            }
        )
        link = f"{self.valves.WEB_UI_URL}/?{query}"
        return (
            f"> **これは「{route['model_name']}」でやります。**"
            "この使い方ヘルプの画面では処理できません。\n>\n"
            f"> 1. [ここを押して「{route['model_name']}」を開く]({link})\n"
            f"> 2. {route['attach']}\n"
            "> 3. 入力欄に入っている文を送る\n"
        )

    # ------------------------------------------------------------------
    # 検索(ここがこのPipeの本体。LLMに探させない)
    # ------------------------------------------------------------------
    async def _embed(self, text: str) -> list[float]:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{self.valves.OLLAMA_BASE_URL}/api/embed",
                json={"model": self.valves.EMBEDDING_MODEL, "input": text},
            )
            response.raise_for_status()
            return response.json()["embeddings"][0]

    async def _search(self, question: str) -> list[tuple[str, str]]:
        """
        (ファイル名, 本文) の並びを、関連の高い順に返す。

        ベクトルはナレッジIDと同じ名前のコレクションに入っている
        (file-<id> ではない。版によって違うため実物で確認した)。
        """
        from open_webui.models.knowledge import Knowledges
        from open_webui.retrieval.vector.factory import VECTOR_DB_CLIENT

        rows = await Knowledges.get_knowledge_bases()
        knowledge = next(
            (row for row in rows if row.name == self.valves.KNOWLEDGE_NAME), None
        )
        if knowledge is None:
            raise RuntimeError(
                f"ナレッジ「{self.valves.KNOWLEDGE_NAME}」がありません。"
                "tools\\open_webui_help_sync.py --apply で作成してください"
            )
        # has_collection は当てにしない。この版のOpen WebUIは
        # コレクション名(文字列)とコレクション実体を比較していて常にFalseになる。
        # 検索を直接試し、失敗したときに作り直しを案内する
        vector = await self._embed(question)
        try:
            result = VECTOR_DB_CLIENT.search(
                collection_name=knowledge.id, vectors=[vector], limit=self.valves.TOP_K
            )
        except Exception as error:
            raise RuntimeError(
                f"ナレッジのベクトルを検索できません({error})。"
                "tools\\open_webui_help_sync.py --apply --reset で作り直してください"
            ) from error
        documents = (getattr(result, "documents", None) or [[]])[0]
        metadatas = (getattr(result, "metadatas", None) or [[]])[0]
        found: list[tuple[str, str]] = []
        for index, document in enumerate(documents):
            meta = metadatas[index] if index < len(metadatas) else {}
            name = str((meta or {}).get("name") or (meta or {}).get("source") or "資料")
            if document:
                found.append((name, str(document)))
        return found

    def _build_prompt(
        self,
        question: str,
        found: list[tuple[str, str]],
        instruction: str = INSTRUCTION,
    ) -> str:
        """検索結果を番号付きの【資料】にして質問と一緒に渡す。"""
        sections: list[str] = []
        used = 0
        for number, (name, document) in enumerate(found, start=1):
            block = f"【資料{number}】{name}\n{document}"
            if used + len(block) > self.valves.MAX_CONTEXT_CHARS:
                break
            sections.append(block)
            used += len(block)
        return "\n\n".join(
            (instruction, "\n\n".join(sections), f"【質問】\n{question}")
        )

    async def _answer(self, prompt: str) -> str:
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{self.valves.OLLAMA_BASE_URL}/api/chat",
                json={
                    "model": self.valves.ANSWER_MODEL,
                    "stream": False,
                    "keep_alive": "30m",
                    # gemma4は思考(thinking)を出すモデル。指定しないと本文を書く前に
                    # 思考を生成し、長い資料で num_ctx の残りを使い切って
                    # done_reason=length で本文が0文字のまま返る(2026-08-17に実測、
                    # tools/excel_comment_build.py の前例と同じ)。抑制しないと
                    # 回答が毎回思考分に遅延する(2026-08-18に実測)。
                    "think": False,
                    "options": {"temperature": 0, "num_ctx": 32768},
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            response.raise_for_status()
            payload = response.json()
        return str((payload.get("message") or {}).get("content") or "").strip()

    @staticmethod
    async def _emit_status(
        emitter: Optional[EventEmitter], description: str, done: bool
    ) -> None:
        if emitter is None:
            return
        try:
            await emitter(
                {
                    "type": "status",
                    "data": {"description": description, "done": done, "hidden": False},
                }
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 本体
    # ------------------------------------------------------------------
    async def pipe(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __request__: Any = None,
        __files__: Optional[list] = None,
        __event_emitter__: Optional[EventEmitter] = None,
        __task__: Optional[str] = None,
        __chat_id__: Optional[str] = None,
    ) -> str:
        if __task__:
            return "使い方ヘルプ"

        question = self._latest_user_message(body).strip()
        if not question:
            return HELP_MESSAGE

        await self._emit_status(__event_emitter__, "資料を検索しています", False)
        try:
            found = await self._search(question)
        except Exception as error:
            await self._emit_status(__event_emitter__, "検索に失敗しました", True)
            return f"❌ 資料を検索できませんでした: {error}"

        if not found:
            await self._emit_status(__event_emitter__, "該当なし", True)
            return (
                "資料の中に、この質問に関係する記述が見つかりませんでした。\n"
                "言い方を変えて聞き直すか、VS CodeのKiloで直接調べてください。"
            )

        # 振り分けは回答より先に決める。「どう頼めばいいか」の質問は、担当が
        # あるときだけ依頼文づくりへ切り替える(担当が無い=頼む相手がいない
        # 質問なので、いつもの説明を返すのが正しい)
        route = self._route(question)
        # 「kilo:」で始まる質問は、利用者が行き先を宣言している。判定より優先する。
        # ただし書式・置き場所の質問は、宣言があっても答えが変わらない
        # (ひな型を直すだけでコード変更は不要)ので、いつもの説明へ落とす
        as_kilo = bool(KILO_TRIGGER_PATTERN.match(question)) and not self._not_a_job(question)
        as_request_text = route is not None and bool(REQUEST_TEXT_PATTERN.search(question))
        # 宣言の合図そのものは資料と関係がないので、LLMへ渡す前に落とす
        asked = KILO_TRIGGER_PATTERN.sub("", question).strip() if as_kilo else question

        if as_kilo:
            await self._emit_status(
                __event_emitter__, "Kiloへの依頼文を作っています", False
            )
            root = self.valves.REPO_ROOT_PATH
            names: list[str] = []
            for name, _ in found:
                if name not in names:
                    names.append(name)
            places = [self._source_path(name, root) for name in names]
            # LLMに書かせ、機械で検め、通らなければ作り直す。使い切ったら
            # 用件と検索結果だけの確定版へ退避する(創作が本番へ出るよりよい)
            request_text = ""
            reason: Optional[str] = None
            for _ in range(KILO_REQUEST_TRIES):
                prompt = self._build_prompt(asked, found, KILO_REQUEST_INSTRUCTION)
                if reason:
                    prompt += f"\n\n【前回の不合格】{reason}\n同じ間違いを繰り返さないでください。"
                try:
                    raw = await self._answer(prompt)
                except Exception as error:
                    reason = f"生成に失敗しました({error})"
                    break
                request_text = self._clean_request_text(raw, KILO_REQUEST_MAX_CHARS)
                if not request_text:
                    reason = "本文が空です"
                    continue
                reason = self._verify_kilo_request(request_text, asked, places)
                if reason is None:
                    break
            note = ""
            if reason is not None:
                request_text = self._kilo_request(asked, found)
                note = (
                    f"\n(作った文が検査に通らなかったため、用件と手がかりをそのまま"
                    f"並べています: {reason})\n"
                )
            await self._emit_status(__event_emitter__, "完了", True)
            sources = "\n".join(f"- `{path}`" for path in places[:8])
            return (
                f"{self._kilo_handover()}\n"
                f"下をコピーして、Kiloのチャットへ貼ってください。{note}\n\n"
                f"```\n{request_text}\n```\n\n"
                f"---\n**参照した資料**\n{sources}"
            )
        await self._emit_status(
            __event_emitter__,
            "依頼文を作っています"
            if as_request_text
            else f"{len(found)}件の資料をもとに回答を作っています",
            False,
        )
        try:
            answer = await self._answer(
                self._build_prompt(
                    asked,
                    found,
                    REQUEST_INSTRUCTION if as_request_text else INSTRUCTION,
                )
            )
        except Exception as error:
            await self._emit_status(__event_emitter__, "回答の生成に失敗しました", True)
            return f"❌ 回答を作れませんでした: {error}"

        await self._emit_status(__event_emitter__, "完了", True)
        if not answer:
            return "回答が空でした。もう一度お試しください。"

        # 根拠を必ず添える。どのファイルを見たかが分かれば自分で確認できる
        names: list[str] = []
        for name, _ in found:
            if name not in names:
                names.append(name)
        root = self.valves.REPO_ROOT_PATH
        sources = "\n".join(f"- `{self._source_path(name, root)}`" for name in names[:8])
        if as_request_text:
            request_text = self._clean_request_text(answer)
            if request_text:
                return (
                    f"{self._handover(route, request_text)}\n"
                    "リンクを押すと、この文が入力欄に入った状態で開きます。"
                    "手で送るときは下をコピーしてください。\n\n"
                    f"```\n{request_text}\n```\n\n"
                    f"---\n**参照した資料**\n{sources}"
                )

        body_text = f"{answer}\n\n---\n**参照した資料**\n{sources}"

        # ひな型の編集は、検索で手順が丸ごと渡らないことがある(見出しだけの
        # 断片が当たる)。決め打ちの手順を一番上に出して、探し回らせない
        guides = self._guides(question)
        if guides:
            return f"{self._guide_banner(guides)}\n{body_text}"

        # 担当があるなら、切り替え先を一番上に出す。説明を読む前に
        # 「どこへ行けばいいか」が分かる状態にする
        if route is None:
            return body_text
        return f"{self._handover(route)}\n{body_text}"
