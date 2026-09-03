"""
Toolの処理結果をチャットの埋め込み欄へ表示するHTMLページを組み立てる。

Open WebUIの通常のTool返り値はローカルLLMへ戻され、最終回答の生成時に
短縮・要約されることがある。結果をHTMLとして埋め込み欄へ直接出すことで、
LLMに本文を渡さず全文を確実に表示する。ページの見た目を変えるときは
このファイルだけを直せばよい(Tool側の貼り直しは不要)。
"""

from __future__ import annotations

import html


def build_result_page(
    *,
    operation_label: str,
    result: str,
    source_chars: int,
    result_chars: int,
    source_type: str,
    workflow_run_id: str,
    warnings: list[str],
    result_chars_crlf: int | None = None,
    attached_filename: str = "",
) -> str:
    escaped_result = html.escape(result)
    escaped_operation = html.escape(operation_label)
    escaped_source_type = html.escape(source_type)
    escaped_run_id = html.escape(workflow_run_id or "取得なし")

    warnings_html = ""
    if warnings:
        escaped_warnings = html.escape(" / ".join(warnings))
        warnings_html = f'<div class="warnings">{escaped_warnings}</div>'

    # 改行をCRLFとして数えるソフトでの見え方も併記する。
    chars_label = f"{result_chars}文字"
    if result_chars_crlf is not None and result_chars_crlf != result_chars:
        chars_label += f" (改行CRLF換算 {result_chars_crlf}文字)"

    attached_html = ""
    if attached_filename:
        attached_html = (
            '<div class="attached">結果ファイル '
            f"<strong>{html.escape(attached_filename)}</strong> を添付しました"
            "</div>"
        )

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>文章処理結果</title>
<style>
    * {{
        box-sizing: border-box;
    }}
    body {{
        margin: 0;
        padding: 12px;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                     "Noto Sans JP", sans-serif;
        background: transparent;
        color: inherit;
    }}
    .meta {{
        display: flex;
        flex-wrap: wrap;
        gap: 8px 14px;
        margin-bottom: 10px;
        font-size: 13px;
        line-height: 1.4;
    }}
    .meta span {{
        white-space: nowrap;
    }}
    .warnings {{
        margin-bottom: 8px;
        font-size: 13px;
        color: #b45309;
    }}
    .attached {{
        margin-bottom: 8px;
        font-size: 13px;
        opacity: 0.85;
    }}
    .toolbar {{
        display: flex;
        gap: 8px;
        margin-bottom: 8px;
    }}
    button {{
        padding: 6px 12px;
        border: 1px solid #8885;
        border-radius: 6px;
        background: #8882;
        color: inherit;
        cursor: pointer;
    }}
    button:hover {{
        background: #8883;
    }}
    textarea {{
        width: 100%;
        min-height: 420px;
        height: 68vh;
        resize: vertical;
        padding: 12px;
        border: 1px solid #8885;
        border-radius: 8px;
        background: transparent;
        color: inherit;
        font: 15px/1.75 "Noto Sans JP", sans-serif;
        white-space: pre-wrap;
    }}
    #notice {{
        margin-left: 4px;
        font-size: 13px;
        opacity: 0.8;
    }}
</style>
</head>
<body>
<div class="meta">
    <span><strong>処理:</strong> {escaped_operation}</span>
    <span><strong>取得元:</strong> {escaped_source_type}</span>
    <span><strong>入力:</strong> {source_chars}文字</span>
    <span><strong>出力:</strong> {chars_label}</span>
    <span><strong>Workflow:</strong> {escaped_run_id}</span>
</div>
{attached_html}
{warnings_html}
<div class="toolbar">
    <button type="button" onclick="copyResult()">全文をコピー</button>
    <span id="notice"></span>
</div>

<textarea id="result" readonly>{escaped_result}</textarea>

<script>
async function copyResult() {{
    const area = document.getElementById("result");
    const notice = document.getElementById("notice");

    try {{
        await navigator.clipboard.writeText(area.value);
        notice.textContent = "コピーしました";
    }} catch (error) {{
        area.focus();
        area.select();
        const ok = document.execCommand("copy");
        notice.textContent = ok ? "コピーしました" : "コピーに失敗しました";
    }}

    setTimeout(() => {{
        notice.textContent = "";
    }}, 1800);
}}

function reportHeight() {{
    const height = document.documentElement.scrollHeight;
    parent.postMessage({{type: "iframe:height", height}}, "*");
}}

window.addEventListener("load", reportHeight);
new ResizeObserver(reportHeight).observe(document.body);
</script>
</body>
</html>
"""
