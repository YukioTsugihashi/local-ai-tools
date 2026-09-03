"""
Open WebUIの画面カスタマイズが消えていないか見張り、消えていたら戻す。

**なぜ要るか(2026-08-18に原因まで特定):**
`open_webui/config.py` はトップレベルで `STATIC_DIR` の中身を全消しし、
初期状態を書き戻す。つまり**コンテナ内で open_webui を import する新しい
プロセスが立つたびに static/ 配下は初期化される**。
`doctor.py` の内部API確認(コンテナ内で python を起動して import する)が
まさにこれで、**doctorを走らせるたびに custom.css・Excelガード・操作案内が
消えていた**。実験で再現済み。

そのため今は static/ を使わず index.html へ直接埋め込んでいる
(tools/webui_tour.py)。index.html は `/app/build` にあり初期化の対象外。
それでもコンテナの作り直しや別の書き換えで消えることはあるので、ここで見張る。

  python tools\\webui_static_guard.py          確認のみ(既定・読み取りだけ)
  python tools\\webui_static_guard.py --fix    消えていたら戻す
  python tools\\webui_static_guard.py --log    logs/ へ記録する(自動実行用)

自動実行: タスク minutes-pipeline-webui-static-guard(5分ごと)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import webui_tour

ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "logs" / "webui_static_guard.log"
BASE_URL = "http://localhost:3000"

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class Requirement:
    """配信されている画面(index.html)に必ず入っているべきもの。"""

    label: str
    needle: str


def requirements() -> list[Requirement]:
    """
    手元の内容から「入っているはずのもの」を組み立てる。

    版まで見る。古い版のまま配信されている状態も「直すべき」として扱う。
    """
    runtime = webui_tour.RUNTIME.read_text(encoding="utf-8")
    scripts, _ = webui_tour.load_tours()
    bundle = webui_tour.build_bundle(runtime, scripts)
    found = [
        Requirement("画面カスタマイズの差し込み", webui_tour.MARK_BEGIN),
        Requirement("操作案内の本体", "__WEBUI_TOUR_SCRIPTS__"),
        Requirement(f"操作案内の版({webui_tour.bundle_version(bundle)})", webui_tour.bundle_version(bundle)),
    ]
    if webui_tour.CUSTOM_CSS.is_file():
        # 全文ではなく先頭だけ見る。改行の扱いで空振りしないため
        head = webui_tour.CUSTOM_CSS.read_text(encoding="utf-8").strip()[:60]
        if head:
            found.append(Requirement("画面の見た目(custom.css)", head))
    if webui_tour.UPLOAD_GUARD.is_file():
        found.append(Requirement("ExcelのRAG処理防止ガード", "__MP_EXCEL_UPLOAD_GUARD__"))
    return found


def fetch(url: str, timeout: int = 20) -> str | None:
    """取れなければ None。落とさない(見張りが本処理を止めては意味がない)。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                return None
            return response.read().decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        # ValueError も拾う。URLに非ASCIIが混じると UnicodeEncodeError になり、
        # 見張りのほうが先に落ちる
        return None


def problems(page: str | None, needed: list[Requirement] | None = None) -> list[tuple[Requirement, str]]:
    """配信されている画面を見て、直すべきものと理由を返す(通信しない)。"""
    needed = needed if needed is not None else requirements()
    if page is None:
        return [(item, "画面を取得できません") for item in needed]
    return [(item, "画面に入っていません") for item in needed if item.needle not in page]


def check(base_url: str = BASE_URL) -> list[tuple[Requirement, str]]:
    return problems(fetch(base_url.rstrip("/") + "/"))


def _run(command: list[str], label: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"{label}: 実行できません: {error}"
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-300:]
        return False, f"{label}: 失敗しました: {detail}"
    return True, f"{label}: 戻しました"


def restore() -> list[str]:
    """
    index.html へまとめて入れ直す。

    操作案内・custom.css・Excelガードは1か所に同居しているので、
    直し方も1つで済む(以前は Apply-WebUiTweaks.ps1 と2本立てだった)。
    """
    ok, message = _run(
        [sys.executable, str(ROOT / "tools" / "webui_tour.py"), "deploy", "--apply"],
        "画面カスタマイズ",
    )
    return [message]


def write_log(lines: list[str]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(f"{stamp} {line}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Open WebUIの画面カスタマイズが消えていないか見張る"
    )
    parser.add_argument("--fix", action="store_true", help="消えていたら戻す(既定は確認のみ)")
    parser.add_argument("--log", action="store_true", help="logs/ へ記録する(自動実行用)")
    parser.add_argument("--base-url", default=BASE_URL, help="Open WebUIのURL")
    args = parser.parse_args(argv)

    found = check(args.base_url)
    lines: list[str] = []
    if not found:
        lines.append("異常なし(画面カスタマイズは配信されています)")
    else:
        for item, reason in found:
            lines.append(f"消えています: {item.label} — {reason}")
        if args.fix:
            lines.extend(restore())
            remaining = check(args.base_url)
            lines.append(
                "戻し切れませんでした" if remaining else "すべて戻しました。ブラウザは Ctrl+Shift+R"
            )
        else:
            lines.append("戻すには --fix を付けてください")

    for line in lines:
        print(line)
    if args.log:
        write_log(lines)
    return 0 if not found or args.fix else 1


if __name__ == "__main__":
    raise SystemExit(main())
