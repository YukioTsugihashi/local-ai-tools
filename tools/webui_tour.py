"""
Open WebUI の画面に重ねる操作案内(ツアー)の検査・確認・配備。

画面を暗くして押す場所だけ明るく丸で囲み、吹き出しで説明する、という
チュートリアルを Open WebUI の上から被せる。本体は改造しない。

**差し込み先は index.html である。static/ には置かない。**
`open_webui/config.py` は読み込まれるたびにトップレベルで
`STATIC_DIR` の中身を全消しして初期状態を書き戻す。つまり
**コンテナ内で open_webui を import する新しいプロセスが立つたびに
static/ 配下は初期化される**(doctor.py の内部API確認がまさにこれで、
2026-08-18に実測・再現。custom.css も Excelガードも一緒に消えていた)。
index.html は `/app/build` にあり、この初期化の対象外。

そのため画面のカスタマイズ(操作案内・custom.css・Excelアップロードガード)を
まとめて index.html へ直接埋め込む。1か所にあるほうが消えたことも分かりやすい。

安全のための決めごと:
  - 画面の隅に置くのは「?」ボタン1つだけ。押すまで案内は始まらない
    (URLへ直接 `#tour=<id>` を付けて始めることもできる)
  - 覆いは pointer-events: none。案内中も画面はそのまま操作できる
  - 自動クリックはしない(誤操作の取り返しがつかないため見せるだけ)
  - 目印が見つからないときは案内を中止する。業務は止めない

使い方(リポジトリ直下で):
  python tools\\webui_tour.py check              台本と実行本体の検査
  python tools\\webui_tour.py preview            見え方の確認用HTMLを作る
  python tools\\webui_tour.py deploy             反映せず差分だけ出す(既定)
  python tools\\webui_tour.py deploy --apply     コンテナへ反映して検証
  python tools\\webui_tour.py remove --apply     元へ戻す

Open WebUI を作り直す(docker rm→run)と index.html も戻るため、
`deploy --apply` をやり直すこと。OPEN_WEBUI_UPGRADE.md にも記載。
消えていないかは tools/webui_static_guard.py が5分ごとに見張る。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOUR_DIR = ROOT / "webui_tour"
RUNTIME = TOUR_DIR / "tour.js"
TOURS_DIR = TOUR_DIR / "tours"
PREVIEW_OUT = ROOT / "output" / "webui_tour_preview.html"

CONTAINER = "open-webui"
# 初期化されない場所。static/ は open_webui の import で毎回消える
INDEX_PATH = "/app/build/index.html"

# 一緒に埋め込む画面カスタマイズ。static/ に置くと消えるため同じ扱いにする
CUSTOM_CSS = ROOT / "custom.css"
UPLOAD_GUARD = ROOT / "open_webui_excel_upload_guard.js"

# index.html へ入れる目印。remove はこの範囲だけを取り除く
MARK_BEGIN = "<!-- >>> minutes-pipeline webui customizations >>> -->"
MARK_END = "<!-- <<< minutes-pipeline webui customizations <<< -->"

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class TourError(RuntimeError):
    """利用者へそのまま見せてよい失敗理由。"""


# ---------------------------------------------------------------------------
# 台本と組み立て(dockerに触らない部分。テストはここを直接呼ぶ)
# ---------------------------------------------------------------------------


def validate_tour(data: object, source: str) -> list[str]:
    """台本1件を検査して、日本語の問題点を並べて返す。"""
    problems: list[str] = []
    if not isinstance(data, dict):
        return [f"{source}: 中身がオブジェクトではありません"]
    for key in ("id", "title"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            problems.append(f"{source}: {key} がありません")
    models = data.get("models")
    if models is not None:
        if not isinstance(models, list):
            problems.append(f"{source}: models は配列で書きます")
        else:
            for position, model in enumerate(models):
                if not isinstance(model, str) or not model.strip():
                    problems.append(f"{source}: models[{position}] が空です")
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        problems.append(f"{source}: steps が1件もありません")
        return problems
    for index, step in enumerate(steps):
        where = f"{source}: steps[{index}]"
        if not isinstance(step, dict):
            problems.append(f"{where} がオブジェクトではありません")
            continue
        if not isinstance(step.get("text"), str) or not step["text"].strip():
            problems.append(f"{where}: text がありません")
        anchors = step.get("anchors", [])
        if not isinstance(anchors, list):
            problems.append(f"{where}: anchors は配列で書きます")
            continue
        for position, anchor in enumerate(anchors):
            if not isinstance(anchor, str) or not anchor.strip():
                problems.append(f"{where}: anchors[{position}] が空です")


    return problems


def load_tours(tours_dir: Path = TOURS_DIR) -> tuple[dict[str, dict], list[str]]:
    """tours/*.json を読み込む。壊れていても例外にせず理由を返す。"""
    scripts: dict[str, dict] = {}
    problems: list[str] = []
    paths = sorted(tours_dir.glob("*.json"))
    if not paths:
        return scripts, [f"台本が1件もありません: {tours_dir}"]
    for path in paths:
        source = path.name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            problems.append(f"{source}: 読めません: {error}")
            continue
        found = validate_tour(data, source)
        if found:
            problems.extend(found)
            continue
        tour_id = data["id"]
        if tour_id in scripts:
            problems.append(f"{source}: id が重複しています: {tour_id}")
            continue
        scripts[tour_id] = data
    return scripts, problems


def build_bundle(runtime_source: str, scripts: dict[str, dict]) -> str:
    """台本を埋め込んだ配布用JSを組み立てる。取得のための通信を起こさない。"""
    data = json.dumps(scripts, ensure_ascii=False, indent=1, sort_keys=True)
    return (
        "/* 生成物。直接編集しない。\n"
        "   正本: webui_tour/tour.js と webui_tour/tours/*.json\n"
        "   作り直し: python tools\\webui_tour.py deploy --apply */\n"
        f"window.__WEBUI_TOUR_SCRIPTS__ = {data};\n"
        # 画面から版を確かめられるようにする(#tour=debug で出る)。
        # 「直したのに変わらない」がキャッシュなのか未反映なのかを切り分けるため
        f"window.__WEBUI_TOUR_VERSION__ = {json.dumps(_version_of(runtime_source, data))};\n"
        f"{runtime_source}"
    )


def _version_of(runtime_source: str, data: str) -> str:
    """束ねた中身から決まる短い版。"""
    return hashlib.sha256((runtime_source + data).encode("utf-8")).hexdigest()[:12]


def bundle_version(bundle: str) -> str:
    """
    束ねた中身に埋めた版を取り出す。

    埋め込む値と、配備・確認で照合する値が別々のハッシュだと、
    「入れたのに古い版だと言われる」ことになる(実際そうなった)。
    版はここ1か所から取る。
    """
    found = re.search(r'__WEBUI_TOUR_VERSION__ = "([0-9a-f]{6,})"', bundle)
    if found:
        return found.group(1)
    return hashlib.sha256(bundle.encode("utf-8")).hexdigest()[:12]


def _inline_script(source: str) -> str:
    """
    HTMLの<script>へ埋めても壊れない形にする。

    JS本文に `</script>` が現れるとそこでタグが閉じてしまう。中身を
    変えずに閉じタグとして読まれないようにする(定石)。
    """
    return source.replace("</", "<\\/")


def page_block(bundle: str) -> str:
    """
    index.html へ入れる画面カスタマイズ一式(前後を目印で挟む)。

    ツアーだけでなく custom.css と Excelアップロードガードも同居させる。
    static/ に置くと open_webui の import のたびに消えるため、
    消えない場所へまとめる。手元のファイルが無ければその分は入れない。
    """
    parts = [MARK_BEGIN]
    if CUSTOM_CSS.is_file():
        parts.append(f"<style>\n{CUSTOM_CSS.read_text(encoding='utf-8')}\n</style>")
    if UPLOAD_GUARD.is_file():
        guard = _inline_script(UPLOAD_GUARD.read_text(encoding="utf-8"))
        parts.append(f"<script>\n{guard}\n</script>")
    # ツアーidはURLが書き換わる前に控える。Open WebUI(SvelteKit)は
    # 起動時にURLを変えることがあり、後から読むと取り逃す
    parts.append(
        "<script>\n"
        "try{var m=/[#?&]tour=([A-Za-z0-9_-]+)/.exec(location.hash+location.search);"
        "if(m)window.__WEBUI_TOUR_WANTED__=m[1];}catch(e){}\n"
        "</script>"
    )
    parts.append(f"<script>\n{_inline_script(bundle)}\n</script>")
    parts.append(MARK_END)
    return "\n".join(parts) + "\n"


def strip_index(current: str) -> str:
    """index.html から自分の入れた範囲だけを取り除く。他は触らない。"""
    begin = current.find(MARK_BEGIN)
    if begin < 0:
        return current
    end = current.find(MARK_END, begin)
    if end < 0:
        # 終わりの目印が消えている。始まり以降は自分の残骸とみなす
        return current[:begin]
    end += len(MARK_END)
    if current[end : end + 1] == "\n":
        end += 1
    return current[:begin] + current[end:]


def patch_index(current: str, block: str) -> str:
    """
    </head> の直前へ差し込む。既に入っていれば置き換える。

    末尾ではなく </head> の前に置くのは、Open WebUI本体の描画が始まる前に
    ツアーidを控えたいため。
    """
    base = strip_index(current)
    marker = "</head>"
    position = base.find(marker)
    if position < 0:
        raise TourError("index.html に </head> がありません(形が変わった可能性)")
    return base[:position] + block + base[position:]


# 見え方の確認用。Open WebUI と同じIDだけを持たせた作り物の画面。
# 実データも実画面も使わないので、コンテナへ反映する前に何度でも試せる
PREVIEW_TEMPLATE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>操作案内の見え方確認</title>
<style>
 :root {{ color-scheme: light dark; }}
 body {{ margin:0; height:100vh; display:flex;
        font-family:system-ui,"Yu Gothic UI","Meiryo",sans-serif; background:#f8fafc; color:#0f172a; }}
 #sidebar {{ width:240px; background:#0f172a; color:#e2e8f0; padding:12px; flex:none; }}
 #sidebar-new-chat-button {{ display:block; width:100%; padding:8px; border:0; border-radius:8px;
        background:#1e293b; color:#e2e8f0; text-align:left; cursor:pointer; }}
 main {{ flex:1; display:flex; flex-direction:column; min-width:0; }}
 header {{ padding:10px 16px; border-bottom:1px solid #e2e8f0; }}
 #model-selector-0-button {{ font-size:15px; font-weight:600; background:none; border:0;
        cursor:pointer; color:inherit; }}
 #messages-container {{ flex:1; overflow:auto; padding:24px; }}
 .card {{ background:#fff; border:1px solid #e2e8f0; border-radius:12px; padding:16px; max-width:640px; }}
 #chat-input-container {{ margin:16px; border:1px solid #cbd5e1; border-radius:14px;
        background:#fff; padding:10px; display:flex; gap:8px; align-items:center; }}
 #input-menu-button, #send-message-button {{ width:36px; height:36px; border-radius:50%;
        border:0; cursor:pointer; font-size:18px; flex:none; }}
 #input-menu-button {{ background:#e2e8f0; }}
 #send-message-button {{ background:#0284c7; color:#fff; }}
 #message-input-container {{ flex:1; }}
 #message-input-container input {{ width:100%; border:0; outline:none; font-size:15px;
        background:transparent; color:inherit; }}
 #restart {{ position:fixed; left:256px; bottom:16px; z-index:10; padding:10px 16px;
        border:0; border-radius:10px; background:#0284c7; color:#fff; cursor:pointer; }}
 .note {{ color:#64748b; font-size:13px; }}
</style>
</head>
<body>
<div id="sidebar">
  <button id="sidebar-new-chat-button">＋ 新しいチャット</button>
  <p class="note" style="color:#94a3b8">これは見え方の確認用の作り物の画面です。</p>
</div>
<main>
  <header><button id="model-selector-0-button">モデルを選択 ▾</button></header>
  <div id="messages-container">
    <div id="chat-container" class="card">
      <p>ここに結果が返ります。</p>
      <p class="note">Open WebUI と同じIDだけを持たせてあります。実物と同じ目印に吹き出しが付くかを、
      コンテナへ反映する前に確認できます。</p>
    </div>
  </div>
  <div id="chat-input-container">
    <button id="input-menu-button">＋</button>
    <span id="message-input-container"><input placeholder="メッセージを入力" /></span>
    <button id="send-message-button">↑</button>
  </div>
</main>
<button id="restart">もう一度 案内を見る</button>
<script>
{bundle}
</script>
<script>
 var TOUR_ID = {tour_id};
 function begin() {{ if (window.__WEBUI_TOUR__) window.__WEBUI_TOUR__.start(TOUR_ID); }}
 document.getElementById('restart').addEventListener('click', begin);
 begin();
</script>
</body>
</html>
"""


def build_preview(bundle: str, tour_id: str) -> str:
    """作り物の画面に案内を重ねた確認用HTML(単体で開ける・通信しない)。"""
    return PREVIEW_TEMPLATE.format(bundle=bundle, tour_id=json.dumps(tour_id))


# ---------------------------------------------------------------------------
# コンテナ操作
# ---------------------------------------------------------------------------


def run_docker(*arguments: str, timeout: int = 120) -> str:
    result = subprocess.run(
        ["docker", *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=NO_WINDOW,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-500:]
        raise TourError(f"docker {arguments[0]} に失敗しました: {detail}")
    return result.stdout


def read_container_file(path: str, container: str = CONTAINER) -> str:
    """コンテナ内のテキストファイルを取り出す。無ければ空文字。"""
    with tempfile.TemporaryDirectory() as work:
        local = Path(work) / "pulled"
        try:
            run_docker("cp", f"{container}:{path}", str(local))
        except TourError:
            return ""
        if not local.is_file():
            return ""
        return local.read_text(encoding="utf-8", errors="replace")


def write_container_file(path: str, text: str, container: str = CONTAINER) -> None:
    """置いたつもりを防ぐため、書いたあと取り出してハッシュで確かめる。"""
    with tempfile.TemporaryDirectory() as work:
        local = Path(work) / "payload"
        local.write_text(text, encoding="utf-8", newline="\n")
        run_docker("cp", str(local), f"{container}:{path}")
        pulled = Path(work) / "verify"
        run_docker("cp", f"{container}:{path}", str(pulled))
        want = hashlib.sha256(local.read_bytes()).hexdigest()
        got = hashlib.sha256(pulled.read_bytes()).hexdigest()
        if want != got:
            raise TourError(f"反映後の検証に失敗しました(コンテナ内の {path} が一致しません)")


# ---------------------------------------------------------------------------
# コマンド
# ---------------------------------------------------------------------------


def _prepare() -> tuple[str, dict[str, dict]]:
    """検査を通った実行本体と台本を返す。問題があれば止める。"""
    if not RUNTIME.is_file():
        raise TourError(f"実行本体が見つかりません: {RUNTIME}")
    runtime_source = RUNTIME.read_text(encoding="utf-8")
    if not runtime_source.strip():
        raise TourError(f"実行本体が空です: {RUNTIME}")
    scripts, problems = load_tours()
    if problems:
        raise TourError("台本に問題があります:\n  - " + "\n  - ".join(problems))
    return runtime_source, scripts


def command_check() -> int:
    runtime_source, scripts = _prepare()
    bundle = build_bundle(runtime_source, scripts)
    print(f"実行本体: {RUNTIME.name}({len(runtime_source):,}文字)")
    for tour_id, script in sorted(scripts.items()):
        print(f"台本: {tour_id} … {script['title']}({len(script['steps'])}手順)")
    print(f"組み立て後: {len(bundle):,}文字 / 版 {bundle_version(bundle)}")
    print("問題はありません。")
    return 0


def command_preview(tour_id: str | None, out: Path) -> int:
    runtime_source, scripts = _prepare()
    chosen = tour_id or sorted(scripts)[0]
    if chosen not in scripts:
        raise TourError(f"その台本はありません: {chosen}(あるのは {', '.join(sorted(scripts))})")
    bundle = build_bundle(runtime_source, scripts)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_preview(bundle, chosen), encoding="utf-8")
    print(f"確認用HTMLを作りました: {out}")
    print("ブラウザで開くと、作り物の画面の上で案内が動きます(通信しません)。")
    return 0


def command_deploy(apply: bool, container: str) -> int:
    runtime_source, scripts = _prepare()
    bundle = build_bundle(runtime_source, scripts)
    version = bundle_version(bundle)
    block = page_block(bundle)
    current = read_container_file(INDEX_PATH, container)
    if not current:
        raise TourError(f"{INDEX_PATH} を読めません(コンテナ {container} は動いていますか)")
    updated = patch_index(current, block)

    print(f"対象コンテナ: {container}")
    print(f"差し込み先  : {INDEX_PATH}(static/ は open_webui の import で消えるため使わない)")
    print(f"入れるもの  : 操作案内({len(bundle):,}文字 / 版 {version})", end="")
    print(f"、custom.css" if CUSTOM_CSS.is_file() else "", end="")
    print("、Excelアップロードガード" if UPLOAD_GUARD.is_file() else "")
    print(f"台本        : {', '.join(sorted(scripts))}")
    if current == updated:
        print("index.html は既にこの内容です(変更なし)")
    else:
        print(f"index.html の </head> 直前へ {len(block):,}文字を差し込みます")
        if strip_index(current) != current:
            print("  (前の版は取り除きます)")

    if not apply:
        print("\n反映していません。実際に入れるには --apply を付けてください。")
        return 0

    write_container_file(INDEX_PATH, updated, container)
    served = read_container_file(INDEX_PATH, container)
    if MARK_BEGIN not in served or version not in served:
        raise TourError("反映後の index.html に差し込んだ内容が見当たりません")
    print("\n反映しました。")
    print(f"確認: ブラウザで http://localhost:3000/#tour={sorted(scripts)[0]} を開く")
    print("状態: http://localhost:3000/#tour=debug で何を読んでいるか出せます")
    print("戻す: python tools\\webui_tour.py remove --apply")
    return 0


def command_remove(apply: bool, container: str) -> int:
    current = read_container_file(INDEX_PATH, container)
    updated = strip_index(current)
    if current == updated:
        print("index.html に自分の入れた範囲はありません(変更なし)")
    else:
        print(f"index.html から {len(current) - len(updated):,}文字を取り除きます")
    if not apply:
        print("反映していません。実際に戻すには --apply を付けてください。")
        return 0
    if current != updated:
        write_container_file(INDEX_PATH, updated, container)
    print("戻しました。")
    return 0


def fetch(url: str, timeout: int = 20) -> tuple[int, str]:
    """localhost限定の取得。届いているものを、届く形のまま見る。"""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, ""
    except OSError as error:
        raise TourError(f"{url} へ繋がりません: {error}") from error


def command_verify(base_url: str) -> int:
    """
    ブラウザが実際に受け取る中身を確かめる。

    「画面に出ない」ときの切り分け用。ここが全部OKなら、残る原因は
    ブラウザのキャッシュだけになる。
    """
    runtime_source, scripts = _prepare()
    bundle = build_bundle(runtime_source, scripts)
    version = bundle_version(bundle)
    base = base_url.rstrip("/")
    problems: list[str] = []

    status, page = fetch(f"{base}/")
    print(f"[{'OK' if status == 200 else 'NG'}] 画面 {base}/ (HTTP {status})")
    if status != 200:
        problems.append("Open WebUI が応答していません")
        page = ""

    checks = [
        (MARK_BEGIN in page, "画面カスタマイズの差し込み", "index.html に入っていません"),
        (version in page, f"操作案内の版({version})", "古い版のままです"),
        (
            not CUSTOM_CSS.is_file() or CUSTOM_CSS.read_text(encoding="utf-8")[:80] in page,
            "custom.css",
            "index.html に入っていません",
        ),
        (
            not UPLOAD_GUARD.is_file() or "__MP_EXCEL_UPLOAD_GUARD__" in page,
            "Excelアップロードガード",
            "index.html に入っていません",
        ),
    ]
    for ok, label, reason in checks:
        print(f"[{'OK' if ok else 'NG'}] {label}")
        if not ok and status == 200:
            problems.append(f"{label}: {reason} → deploy --apply が要ります")

    if problems:
        print("\n直すこと:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    first = sorted(scripts)[0]
    print(f"\nサーバ側は正しく配信できています(版 {version})。")
    print(f"それでも画面に出ないときは、ブラウザのキャッシュです。{base}/#tour={first} を開いて")
    print("Ctrl+Shift+R(強制リロード)を1回押してください。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Open WebUI に重ねる操作案内(ツアー)の検査・確認・配備",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--container", default=CONTAINER, help="対象のOpen WebUIコンテナ名")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="実行本体と台本を検査する")
    preview = sub.add_parser("preview", help="見え方の確認用HTMLを作る")
    preview.add_argument("--tour", default=None, help="確認する台本のid")
    preview.add_argument("--out", type=Path, default=PREVIEW_OUT, help="出力先")
    deploy = sub.add_parser("deploy", help="コンテナへ反映する(既定は差分表示のみ)")
    deploy.add_argument("--apply", action="store_true", help="実際に反映する")
    remove = sub.add_parser("remove", help="反映を元へ戻す(既定は差分表示のみ)")
    remove.add_argument("--apply", action="store_true", help="実際に戻す")
    verify = sub.add_parser("verify", help="ブラウザが受け取る中身を確かめる(出ないときの切り分け)")
    verify.add_argument("--base-url", default="http://localhost:3000", help="Open WebUIのURL")
    args = parser.parse_args(argv)

    try:
        if args.command == "check":
            return command_check()
        if args.command == "preview":
            return command_preview(args.tour, args.out)
        if args.command == "deploy":
            return command_deploy(args.apply, args.container)
        if args.command == "remove":
            return command_remove(args.apply, args.container)
        if args.command == "verify":
            return command_verify(args.base_url)
    except TourError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
