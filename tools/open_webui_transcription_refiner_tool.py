"""
title: Transcription Refiner
id: transcription_refiner
description: チャットの最新メッセージからフィラー除去のリライト指示文を組み立てて返す。

Open WebUIの管理画面から直接貼られていたToolを、2026-08-14 にリポジトリへ回収し、
同日に登録を解除した。**このファイルは記録であり、再登録しない。**

解除した理由:
  1. `re` をimportしておらず、実行すると必ず NameError になる。
     実際に呼ぶと「エラーが発生しました: name 're' is not defined」しか返らず、
     一度も動いたことがなかった
  2. やっていることは「LLMへケバ取りを頼む指示文を返す」だけで、
     ルールベースで確実に処理する `/v1/text/kebatori` の下位互換だった
     (削除語がLLM任せ・削りすぎの歯止めなし・数値ガードなし)

回収時点の内容をそのまま残している(不具合も含めて)。
"""


class Tools:
    def __init__(self):
        pass

    async def transcription_refiner(self, __event__: dict = None) -> str:
        """
        チャットの最新メッセージを自動的に読み取り、フィラーを除去してリエンコードします。

        Args:
            __event__: Open WebUIから渡されるイベントデータ（メッセージ履歴が含まれます）
        """
        try:
            # 1. チャットのメッセージ履歴から、最新のユーザー発言を探す
            # __event__ の中には、チャットの全履歴が入っています。
            messages = __event__.get("body", {}).get("messages", [])
            if not messages:
                return "エラー: 処理すべきメッセージが見つかりません。"

            # 最後に送られたユーザーのメッセージを取得
            last_message = messages[-1]["content"]

            if not last_message or len(last_message.strip()) == 0:
                return "エラー: 解析するテキストが空です。テキストを入力してからツールを呼び出してください。"

            # 2. テキストの解析（文字数カウント）
            raw_char_count = len(last_message)
            clean_char_count = len(re.sub(r"\s+", "", last_message))  # noqa: F821 (回収時のまま)

            # 3. リライト指示文（プロンプト）の組み立て
            prompt = (
                f"【解析レポート】\n"
                f"総文字数: {raw_char_count} 文字\n"
                f"実質文字数（空白除外）: {clean_char_count} 文字\n\n"
                f"--- 【リライト指示】 ---\n"
                f"以下のテキストは音声認識の結果です。文脈や情報を維持したまま、"
                f"「えー」「あのー」などのフィラーを除去し、読みやすい文章に整えてください。\n\n"
                f"{last_message}"
            )

            return prompt

        except Exception as e:
            return f"エラーが発生しました: {str(e)}"
