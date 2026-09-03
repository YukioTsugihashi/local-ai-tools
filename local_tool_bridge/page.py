"""
ハブの状態表示ページ。

「今どのツールが繋がっているか」を目で見るためだけのもので、操作はしない。
オフラインPCで開くため、外部のCSS・JS・フォントを一切読み込まない自己完結HTMLにする。
"""

from __future__ import annotations

from html import escape

from local_tool_bridge.hub import ROOT, ToolSpec, backing_script

STYLE = """
:root {
  --bg: #f6f7f9; --panel: #ffffff; --line: #dfe3e8; --text: #1b1f24;
  --muted: #5c6670; --ok: #1a7f4b; --ng: #b3261e; --chip: #eef1f4;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14171a; --panel: #1c2024; --line: #2c3238; --text: #e6e9ec;
    --muted: #9aa4ae; --ok: #4cc38a; --ng: #f2705f; --chip: #262b31;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 20px; background: var(--bg); color: var(--text);
  font-family: "Segoe UI", "Yu Gothic UI", "Meiryo", system-ui, sans-serif;
  line-height: 1.7;
}
main { max-width: 900px; margin: 0 auto; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 28px 0 10px; color: var(--muted); font-weight: 600; }
.sub { color: var(--muted); font-size: 13px; margin: 0 0 20px; }
.panel {
  background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; padding: 16px 18px; margin-bottom: 12px;
}
.state { display: flex; align-items: center; gap: 10px; font-weight: 600; }
.dot { width: 10px; height: 10px; border-radius: 50%; background: var(--ok); flex: none; }
.dot.down { background: var(--ng); }
.row { display: flex; align-items: center; gap: 10px; padding: 5px 0; }
.row + .row { border-top: 1px solid var(--line); }
.rowname { font-weight: 600; font-size: 14px; }
.rowstate { margin-left: auto; font-size: 13px; color: var(--muted); }
.name { font-weight: 600; font-size: 15px; }
.summary { color: var(--muted); font-size: 13px; margin: 2px 0 10px; }
.meta { display: flex; flex-wrap: wrap; gap: 6px; }
.chip {
  background: var(--chip); border: 1px solid var(--line); border-radius: 999px;
  padding: 2px 10px; font-size: 12px; color: var(--muted);
  font-family: Consolas, "Courier New", monospace;
}
.chip.ng { color: var(--ng); border-color: var(--ng); }
.error { border-color: var(--ng); }
.error .name { color: var(--ng); }
.empty { color: var(--muted); font-size: 13px; }
code {
  background: var(--chip); border-radius: 5px; padding: 1px 6px;
  font-family: Consolas, "Courier New", monospace; font-size: 12px;
}
footer { color: var(--muted); font-size: 12px; margin-top: 28px; }
"""


def _tool_panel(spec: ToolSpec) -> str:
    script = backing_script(spec)
    exists = bool(script) and (ROOT / script).is_file()
    chips = [f'<span class="chip">{escape(suffix)}</span>' for suffix in spec.accepts]
    if script:
        state = "" if exists else " ng"
        label = escape(script) if exists else f"{escape(script)}(見つからない)"
        chips.append(f'<span class="chip{state}">{label}</span>')
    return (
        '<div class="panel">'
        f'<div class="name">{escape(spec.name)}</div>'
        f'<div class="summary">{escape(spec.summary)}</div>'
        f'<div class="meta">{"".join(chips)}</div>'
        "</div>"
    )


def _neighbour_panel(services: list[tuple[str, int, bool]]) -> str:
    rows = "".join(
        f'<div class="row">'
        f'<span class="dot {"up" if alive else "down"}"></span>'
        f'<span class="rowname">{escape(name)}</span>'
        f'<span class="chip">port {port}</span>'
        f'<span class="rowstate">{"稼働中" if alive else "停止"}</span>'
        f"</div>"
        for name, port, alive in services
    )
    return f'<div class="panel">{rows}</div>'


def render(
    specs: dict[str, ToolSpec],
    errors: list[str],
    neighbours: list[tuple[str, int, bool]] | None = None,
) -> str:
    """繋がっているツールと、読み込めなかったファイルを1枚にまとめる。"""
    tools_html = (
        "".join(_tool_panel(spec) for _, spec in sorted(specs.items()))
        or '<div class="panel empty">繋がっているツールがありません。'
        'local_tool_bridge/connectors/ へ接続役を1枚置いてください。</div>'
    )
    errors_html = ""
    if errors:
        items = "".join(
            f'<div class="panel error"><div class="name">{escape(reason)}</div></div>'
            for reason in errors
        )
        errors_html = f"<h2>読み込めなかったファイル</h2>{items}"

    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>Kilo ローカルツールハブ</title>
<style>{STYLE}</style>
</head>
<body>
<main>
  <h1>Kilo ローカルツールハブ</h1>
  <p class="sub">Open WebUI から Windows 上の Kilo ツールを呼ぶ受付。15秒ごとに自動更新。</p>

  <div class="panel state"><span class="dot"></span>稼働中 — port 8010 / 繋がっているツール {len(specs)}件</div>

  <h2>繋がっているツール</h2>
  {tools_html}
  {errors_html}

  <h2>周辺サービス（ハブを通らない経路）</h2>
  {_neighbour_panel(neighbours or [])}

  <footer>
    文章処理はハブを経由せず、Open WebUI から直接ブリッジ(port 8008)を呼ぶ。
    この欄は生死の確認だけで、ハブからは操作しない。<br>
    ツールを増やすには <code>local_tool_bridge/connectors/</code> へファイルを1枚置く
    （手順は <code>.kilo/rules/09-hub-connector.md</code>）。<br>
    このページは表示専用。実行は <code>POST /v1/run</code>、一覧のJSONは <code>GET /v1/tools</code>。<br>
    起動ログ: <code>logs/local_tool_bridge.log</code>
  </footer>
</main>
</body>
</html>"""
