"""
行数をカウントするPipe
id: line_count

model_description: 貼り付けた文章の行数を数えます
suggestion: 行数を数える | 文章の行数を確認します | 行数を教えてください
"""

import re
from typing import Dict, Any


class Pipe:
    def __init__(self):
        pass
    
    def _latest_user_message(self, body: dict) -> str:
        """最新のユーザーからのメッセージを取得"""
        for message in reversed(body.get("messages") or []):
            if isinstance(message, dict) and message.get("role") == "user":
                content = message.get("content", "")
                if isinstance(content, str):
                    return content
                elif isinstance(content, list):
                    texts = []
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            value = item.get("text")
                            if value:
                                texts.append(str(value))
                    return "\n".join(texts)
        return ""
    
    async def pipe(self, body: dict, __user__: dict = None, __request__: object = None, __files__: list = None, __event_emitter__: object = None, __task__: str = None, __chat_id__: str = None) -> str:
        # タイトル生成などのバックグラウンドタスクには定型で応答し、
        # ブリッジ処理を走らせない。
        if __task__:
            return "行数カウント"
        
        # メッセージから本文を取得
        message_text = self._latest_user_message(body)
        if not message_text.strip():
            return "処理対象の文章がありません。本文を貼り付けてください。"
        
        # 行数を数える
        lines = message_text.splitlines()
        non_empty_lines = [line for line in lines if line.strip()]
        count = len(non_empty_lines)
        
        result = f"文章の行数は {count} 行です。\n"
        result += f"合計行数: {len(lines)} 行\n"
        result += f"本文プレビュー: {message_text[:100]}{'...' if len(message_text) > 100 else ''}"
        
        return result


# デプロイ用の登録コード
if __name__ == "__main__":
    # このファイルを直接実行した場合に備えて、デプロイ方法の確認を表示
    print("行数カウントPipeの作成完了")
    print("このファイルをOpen WebUIに登録するには、次のコマンドを使用してください:")
    print("python tools\\open_webui_deploy.py tools\\open_webui_line_count_pipe.py --apply")