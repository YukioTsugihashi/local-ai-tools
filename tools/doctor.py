"""
オフライン運用の自己診断ツール(読み取り専用)。

使い方(リポジトリ直下で。素のpythonでもvenvへ自動で実行し直す):
  python tools\\doctor.py
  python tools\\doctor.py --out output\\doctor_report.txt
  python tools\\doctor.py --deep            (架空の短文をbridge→Ollamaへ実際に流す)
  python tools\\doctor.py --deep --render   (加えてdocx生成と再読込まで確認)

- 通常実行は外部状態を一切変更しない(サービスの起動・再起動・書込みをしない)
- --deep も架空の固定文しか送らない(実会議データは使わない)
- 「どこが悪い・次に何をすればいい」を日本語で出す
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
# 本番実行環境のvenv。パッケージ検査はこのPythonを対象にする
VENV_PYTHON = ROOT / "text-processing-bridge" / ".venv" / "Scripts" / "python.exe"
# 作業の帰り先。ここ以外にいること自体は問題ないが、戻し忘れだけは見えるようにする
MAIN_BRANCH = "main"
# 履歴のバックアップ先(D:/offline-kit 下のbareリポジトリ)。オフラインでもpushできる
GIT_BACKUP_REMOTE = "local"


def should_reexec(current: Path, venv_python: Path) -> bool:
    """venv外のPythonで起動されたら真(venvのPythonで実行し直す)。"""
    if not venv_python.exists():
        return False  # venv自体が無い問題は check_python が報告する
    try:
        return current.resolve() != venv_python.resolve()
    except OSError:
        return False


def resolve_out_path(raw: str) -> Path:
    """
    --out の相対パスはリポジトリ直下基準で解決する。

    実行場所がtools/等でも、レポートが迷子(tools/output/など)にならず
    必ずリポジトリのoutput/へ集まるようにするため。
    """
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path

# 議事録パイプラインが使う生成モデル。無いと本処理が動かない
REQUIRED_MODELS = {"gemma4:26b": "議事録パイプラインの生成モデル"}
# Kilo(保守)側のモデル。無くても議事録処理は動くが保守ができない
MAINTENANCE_MODELS = {
    "qwen3.8-128k:latest": "Kiloの実装・計画担当",
    "qwen3.6-128k:latest": "予備(切り戻し用の前世代)",
    "gpt-oss-64k:latest": "予備(旧計画担当)",
    "qwen3-vl-64k:latest": "画像対応",
}


OLLAMA_BASE_URL = "http://localhost:11434"
# Ollamaは待ち行列の長さをAPIへ出さない。滞留は実リクエスト履歴から推定する
OLLAMA_LOG_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "Ollama"
OLLAMA_LOG_TAIL_BYTES = 2_000_000
OLLAMA_BACKLOG_WINDOW_MINUTES = 10
# 直近ウィンドウでこの件数を超えたら、何かが大量投入していると見なす
OLLAMA_BUSY_REQUESTS = 1000
OLLAMA_WAIT_WARNING = 5
OLLAMA_SLOW_RESPONSE_SECONDS = 2.0

_OLLAMA_REQUEST_LINE = re.compile(
    r"\[GIN\]\s+(\d{4}/\d{2}/\d{2}) - (\d{2}:\d{2}:\d{2})\s*\|\s*(\d{3})\s*\|"
    r"\s*([^|]+?)\s*\|[^|]*\|\s*\w+\s+\"([^\"]+)\""
)
# 1件がこの秒数を超えたら、件数が伸びていなくても詰まっている
OLLAMA_SLOW_REQUEST_SECONDS = 60.0
OLLAMA_SLOW_REQUEST_WARNING = 3
# 2xx以外がこの割合を超えたら異常(/api/me はオフライン時の認証失敗なので除く)
OLLAMA_ERROR_RATE_WARNING = 0.3
# コミット(仮想メモリ)がこの割合を超えるとページング地獄に入る。
# 2026-08-14に99.8%まで行き、PC全体が数分単位で応答しなくなった
COMMIT_CRITICAL_RATIO = 0.90
COMMIT_WARNING_RATIO = 0.75


_OLLAMA_WAIT_LINE = re.compile(
    r'time=(\S+).*msg="waiting for llama-server to become available"'
)
# ダウンロード中は完了までGIN行が出ないため、件数だけでは滞留を検出できない
_OLLAMA_DOWNLOAD_LINE = re.compile(r'time=(\S+).*msg="downloading ')


def parse_gin_duration(text: str) -> float:
    """`3m32s` `514.5µs` `1.09s` のような表記を秒へ直す。"""
    total = 0.0
    for value, unit in re.findall(r"([\d.]+)\s*(ms|µs|us|ns|m|s|h)", text.strip()):
        number = float(value)
        total += {
            "h": number * 3600,
            "m": number * 60,
            "s": number,
            "ms": number / 1_000,
            "µs": number / 1_000_000,
            "us": number / 1_000_000,
            "ns": number / 1_000_000_000,
        }[unit]
    return total


@dataclass
class Finding:
    level: str  # "OK" | "情報" | "注意" | "異常"
    title: str
    advice: str = ""
    detail: str = ""


@dataclass
class OllamaTraffic:
    """直近ウィンドウのOllamaリクエスト状況。"""

    total: int = 0
    by_path: dict[str, int] = field(default_factory=dict)
    queue_rejected: int = 0
    server_waits: int = 0
    downloads: int = 0
    # 件数が伸びなくても詰まる場合がある。遅さとエラー率も見る
    slow: int = 0
    slowest_seconds: float = 0.0
    errors: int = 0
    judged: int = 0


# pythonw(コンソール無し)から子プロセスを普通に起動すると黒い窓が開く。
# hub.py 等と同じ理由・同じ扱いにする
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_command(command: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        creationflags=NO_WINDOW,
    )


def check_python() -> list[Finding]:
    findings = [
        Finding("OK", f"Python {sys.version.split()[0]}", detail=sys.executable)
    ]
    if not VENV_PYTHON.exists():
        findings.append(
            Finding(
                "異常",
                "本番用venvがありません(text-processing-bridge/.venv)",
                advice=(
                    "venvを再構築してください: cd text-processing-bridge; "
                    "& \"$env:LOCALAPPDATA\\Programs\\Python\\Python312\\python.exe\" -m venv .venv; "
                    ".\\.venv\\Scripts\\python.exe -m pip install --no-index "
                    "--find-links wheelhouse-win -r requirements.txt -r requirements-office.txt"
                ),
            )
        )
    missing = []
    for module in ("fastapi", "docx", "openpyxl", "yaml", "httpx"):
        try:
            __import__(module)
        except Exception:
            missing.append(module)
    if missing:
        findings.append(
            Finding(
                "異常",
                f"必要パッケージが読み込めません: {', '.join(missing)}",
                advice=(
                    "venvを再構築してください: cd text-processing-bridge; "
                    ".\\.venv\\Scripts\\python.exe -m pip install --no-index "
                    "--find-links wheelhouse-win -r requirements.txt -r requirements-office.txt"
                ),
            )
        )
    else:
        findings.append(Finding("OK", "必要パッケージ(fastapi/docx/openpyxl/yaml/httpx)"))
    return findings


def check_configs(root: Path = ROOT) -> list[Finding]:
    import yaml

    findings: list[Finding] = []
    config_dir = root / "text-processing-bridge" / "config"
    paths = sorted(config_dir.glob("*.yaml")) + sorted(config_dir.glob("*.yml"))
    if not paths:
        return [Finding("異常", f"設定ファイルがありません: {config_dir}")]
    for path in paths:
        try:
            yaml.safe_load(path.read_text(encoding="utf-8"))
            findings.append(Finding("OK", f"設定の構文: {path.name}"))
        except Exception as error:
            findings.append(
                Finding(
                    "異常",
                    f"設定が壊れています: {path.name}",
                    advice="直近の変更を確認し、git diff で壊れた行を戻してください",
                    detail=str(error).splitlines()[0] if str(error) else "",
                )
            )
    return findings


def git_state_findings(head: str, branch: str, dirty: int) -> list[Finding]:
    """
    Gitの現在地を判定する(純粋関数)。

    ブランチを切って作業したまま戻し忘れると、次の依頼が意図しない
    ブランチの上へ積まれる。オフラインでは気づく手段がGitの表示しか無いため、
    main以外にいることを健康診断へ出す。
    """
    where = branch or "(切り離しHEAD)"
    findings = [
        Finding("OK", "Git", detail=f"{where} / コミット {head} / 未コミット変更 {dirty}件")
    ]
    if branch == MAIN_BRANCH:
        return findings
    if not branch:
        findings.append(
            Finding(
                "注意",
                "どのブランチにも乗っていません(切り離しHEAD)",
                advice=(
                    "このままコミットしてもどのブランチにも残りません。"
                    f"git switch {MAIN_BRANCH} で戻してください"
                ),
            )
        )
    else:
        findings.append(
            Finding(
                "情報",
                f"作業ブランチ {branch} で作業中です",
                advice=(
                    f"終わったら git switch {MAIN_BRANCH} で戻り、"
                    f"git merge --no-ff {branch} で取り込みます(GIT_BRANCH_WORKFLOW.md)"
                ),
            )
        )
    return findings


def git_backup_findings(remote_url: str, unpushed: int) -> list[Finding]:
    """
    履歴のバックアップ先へ送れているかを判定する(純粋関数)。

    オフラインではGitHubのような預け先が無く、.git が壊れると全履歴が消える。
    push忘れは誰も気づかないため、遅れを見えるようにしておく。コミット直後は
    必ず遅れるので、数件までは「情報」に留めて騒がない。
    """
    if not remote_url:
        return [
            Finding(
                "注意",
                f"履歴のバックアップ先(remote {GIT_BACKUP_REMOTE})がありません",
                advice=(
                    "git init --bare D:/offline-kit/git/minutes-pipeline.git のあと "
                    f"git remote add {GIT_BACKUP_REMOTE} D:/offline-kit/git/minutes-pipeline.git"
                ),
            )
        ]
    if unpushed <= 0:
        return [Finding("OK", "履歴のバックアップ", detail=f"{remote_url} と同じ")]
    # コミット直後の1件で注意を出すと毎回鳴るため、溨まってきてから知らせる
    level = "注意" if unpushed >= 5 else "情報"
    return [
        Finding(
            level,
            f"バックアップ先へ未送信のコミットが{unpushed}件あります",
            advice=f"git push {GIT_BACKUP_REMOTE} で送ってください",
            detail=remote_url,
        )
    ]


def check_git(root: Path = ROOT) -> list[Finding]:
    head = _run_command(["git", "-C", str(root), "rev-parse", "--short", "HEAD"])
    if head.returncode != 0:
        return [Finding("注意", "Gitの状態を取得できません", detail=head.stderr.strip())]
    status = _run_command(["git", "-C", str(root), "status", "--porcelain"])
    dirty = len([line for line in status.stdout.splitlines() if line.strip()])
    branch = _run_command(["git", "-C", str(root), "branch", "--show-current"])
    return git_state_findings(head.stdout.strip(), branch.stdout.strip(), dirty)


def check_git_backup(root: Path = ROOT) -> list[Finding]:
    remote = _run_command(["git", "-C", str(root), "remote", "get-url", GIT_BACKUP_REMOTE])
    if remote.returncode != 0:
        return git_backup_findings("", 0)
    # 追跡設定に依存させず、全ブランチを対象にバックアップ先に無いコミットを数える
    counted = _run_command(
        [
            "git", "-C", str(root), "rev-list", "--count",
            "--branches", "--not", f"--remotes={GIT_BACKUP_REMOTE}",
        ]
    )
    if counted.returncode != 0:
        return [
            Finding(
                "注意",
                "バックアップ先との差を数えられません",
                detail=counted.stderr.strip(),
            )
        ]
    return git_backup_findings(remote.stdout.strip(), int(counted.stdout.strip() or 0))


def check_disk() -> list[Finding]:
    findings = []
    for drive in ("C:\\", "D:\\"):
        try:
            free_gb = shutil.disk_usage(drive).free / (1024**3)
        except OSError:
            continue
        if free_gb < 3:
            level, advice = "異常", "空き容量が危険水準です。output/の古いファイル等を整理してください"
        elif free_gb < 10:
            level, advice = "注意", "空き容量が少なくなっています"
        else:
            level, advice = "OK", ""
        findings.append(Finding(level, f"ディスク {drive} 空き {free_gb:.0f}GB", advice=advice))
    return findings


def check_http(title: str, url: str, advice_when_down: str, level_when_down: str = "異常") -> Finding:
    import httpx

    try:
        response = httpx.get(url, timeout=3)
        return Finding("OK", f"{title} (HTTP {response.status_code})", detail=url)
    except Exception as error:
        return Finding(
            level_when_down,
            f"{title}へ接続できません",
            advice=advice_when_down,
            detail=f"{url} / {type(error).__name__}",
        )


def open_webui_url_from_port_output(port_output: str) -> str | None:
    """`docker port open-webui 8080/tcp` の結果からホスト側URLを作る。"""
    for line in port_output.splitlines():
        match = re.search(r":(\d+)\s*$", line.strip())
        if match:
            return f"http://localhost:{match.group(1)}"
    return None


def resolve_open_webui_url() -> str:
    """実コンテナの公開ポートを優先し、取得不能時だけ従来値へ戻す。"""
    result = _run_command(
        ["docker", "port", "open-webui", "8080/tcp"], timeout=10
    )
    if result.returncode == 0:
        discovered = open_webui_url_from_port_output(result.stdout)
        if discovered:
            return discovered
    return "http://localhost:8080"


def _ollama_running_models(base_url: str) -> list[str]:
    """/api/ps の稼働中モデル。接続判定は check_ollama_connection が済ませている。"""
    import httpx

    try:
        response = httpx.get(f"{base_url}/api/ps", timeout=3)
        return [
            str(model.get("name", ""))
            for model in response.json().get("models", [])
            if model.get("name")
        ]
    except Exception:
        return []


def check_ollama_connection(base_url: str = OLLAMA_BASE_URL) -> list[Finding]:
    """
    Ollamaへ届くかだけを見る。

    モデル在庫は check_ollama_models、処理の滞留は check_ollama_backlog が見る。
    「つながらない」と「つながるが詰まっている」は対処が全く違うため分けてある。
    """
    import httpx

    started = time.perf_counter()
    try:
        response = httpx.get(f"{base_url}/api/version", timeout=5)
        version = str(response.json().get("version", ""))
    except httpx.TimeoutException as error:
        # 応答が返らないのは「止まっている」ではなく「塞がっている」。
        # 再起動を促すと、モデルのダウンロード中に巻き添えで落とすことになる
        return [
            Finding(
                "異常",
                "Ollamaが応答しません(接続はできるが処理が返らない)",
                advice=(
                    "次の「処理の滞留」項目を先に見てください。"
                    "モデルのダウンロード中(ollama pull)や大量リクエストでもこうなります。"
                    "滞留が無いのに応答しない場合だけOllamaを再起動してください"
                ),
                detail=f"{base_url} / {type(error).__name__}",
            )
        ]
    except Exception as error:
        return [
            Finding(
                "異常",
                "Ollamaへ接続できません",
                advice="スタートメニューから Ollama を起動し、もう一度実行してください",
                detail=f"{base_url} / {type(error).__name__}",
            )
        ]
    elapsed = time.perf_counter() - started
    detail = f"{base_url} / 応答 {elapsed:.2f}秒"
    running = _ollama_running_models(base_url)
    if running:
        detail += f" / 稼働中: {'、'.join(running)}"
    if elapsed >= OLLAMA_SLOW_RESPONSE_SECONDS:
        return [
            Finding(
                "注意",
                f"Ollamaの応答が遅いです({elapsed:.1f}秒)",
                advice="次の「処理の滞留」項目を確認してください。滞留が無いのに遅い場合はOllamaを再起動してください",
                detail=detail,
            )
        ]
    title = f"Ollamaへ接続できました (v{version})" if version else "Ollamaへ接続できました"
    return [Finding("OK", title, detail=detail)]


def _read_ollama_log_tail(
    log_dir: Path, tail_bytes: int = OLLAMA_LOG_TAIL_BYTES
) -> list[str]:
    """
    Ollamaログの末尾だけを読む。

    server-3.log が70MBを超えることがあるため全読みはしない。
    ローテーション直後に窓が空にならないよう2世代ぶんを見る。
    """
    lines: list[str] = []
    for name in ("server-1.log", "server.log"):
        path = log_dir / name
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                handle.seek(max(0, size - tail_bytes))
                chunk = handle.read()
        except OSError:
            continue
        text = chunk.decode("utf-8", errors="replace")
        if size > tail_bytes:
            text = text.split("\n", 1)[-1]  # 先頭の欠けた行は捨てる
        lines.extend(text.splitlines())
    return lines


def summarize_ollama_traffic(lines: list[str], since: datetime) -> OllamaTraffic:
    """
    Ollamaログから直近のリクエスト状況を集計する。

    Ollamaは待ち行列の長さをAPIへ出さない。そのため実際に流れた件数と
    キュー満杯時の503から滞留を推定するしかない。
    """
    traffic = OllamaTraffic()
    for line in lines:
        request = _OLLAMA_REQUEST_LINE.search(line)
        if request:
            day, clock, status, duration, path = request.groups()
            try:
                stamp = datetime.strptime(f"{day} {clock}", "%Y/%m/%d %H:%M:%S").astimezone()
            except ValueError:
                continue
            if stamp < since:
                continue
            traffic.total += 1
            traffic.by_path[path] = traffic.by_path.get(path, 0) + 1
            seconds = parse_gin_duration(duration)
            if seconds >= OLLAMA_SLOW_REQUEST_SECONDS:
                traffic.slow += 1
                traffic.slowest_seconds = max(traffic.slowest_seconds, seconds)
            # /api/me はオフライン時に必ず失敗するため、成否の判定から除く
            if path != "/api/me":
                traffic.judged += 1
                if not status.startswith("2"):
                    traffic.errors += 1
                if status == "503":
                    traffic.queue_rejected += 1
            continue
        wait = _OLLAMA_WAIT_LINE.search(line)
        if wait:
            try:
                stamp = datetime.fromisoformat(wait.group(1))
            except ValueError:
                continue
            if stamp >= since:
                traffic.server_waits += 1
            continue
        download = _OLLAMA_DOWNLOAD_LINE.search(line)
        if download:
            try:
                stamp = datetime.fromisoformat(download.group(1))
            except ValueError:
                continue
            if stamp >= since:
                traffic.downloads += 1
    return traffic


def ollama_backlog_finding(
    traffic: OllamaTraffic, window_minutes: int = OLLAMA_BACKLOG_WINDOW_MINUTES
) -> Finding:
    """集計結果を「詰まっているか」の判定へ落とす。"""
    busiest = "、".join(
        f"{path} {count}件"
        for path, count in sorted(traffic.by_path.items(), key=lambda item: -item[1])[:3]
    )
    detail = f"直近{window_minutes}分: {traffic.total}件"
    if busiest:
        detail += f" / {busiest}"
    if traffic.server_waits:
        detail += f" / モデル応答待ち {traffic.server_waits}回"
    if traffic.downloads:
        detail += f" / ダウンロード進行 {traffic.downloads}回"
    if traffic.slow:
        detail += f" / {OLLAMA_SLOW_REQUEST_SECONDS:.0f}秒超 {traffic.slow}件(最長 {traffic.slowest_seconds:.0f}秒)"
    if traffic.errors:
        detail += f" / 失敗 {traffic.errors}件"

    # 1件あたりが極端に遅い場合、件数は伸びないので流量では捕まらない。
    # 2026-08-14の障害では1件4分・件数控えめで、閾値をすり抜けた
    if traffic.slow >= OLLAMA_SLOW_REQUEST_WARNING:
        return Finding(
            "異常",
            f"Ollamaの応答が極端に遅くなっています({traffic.slow}件が{OLLAMA_SLOW_REQUEST_SECONDS:.0f}秒超、最長{traffic.slowest_seconds:.0f}秒)",
            advice=(
                "メモリのコミットが限界に達している可能性があります。"
                "上の「メモリのコミット」項目を確認し、"
                "python tools\\ollama_watchdog.py で状態を見てください"
            ),
            detail=detail,
        )
    error_rate = traffic.errors / traffic.judged if traffic.judged else 0.0
    if traffic.judged >= 10 and error_rate > OLLAMA_ERROR_RATE_WARNING:
        return Finding(
            "異常",
            f"Ollamaへの依頼が失敗し続けています({traffic.errors}/{traffic.judged}件)",
            advice=(
                "同じ依頼を投げ続けている側を止めてください。"
                "保存先の無い索引作成など、成功しない処理の再試行ループが典型です"
            ),
            detail=detail,
        )
    if traffic.queue_rejected:
        return Finding(
            "異常",
            f"Ollamaが待ち行列満杯で依頼を断っています({traffic.queue_rejected}件)",
            advice=(
                "大量投入している側を先に止めてください"
                "(Kiloのコードベースインデックス作成 /api/embed が主因のことが多い)。"
                "止めてから数分待って、もう一度実行してください"
            ),
            detail=detail,
        )
    if traffic.downloads:
        # 完了までGIN行が出ないため、件数が少なくてもOKにしてはいけない
        return Finding(
            "注意",
            "モデルのダウンロード中です(この間ほかの処理は待たされます)",
            advice=(
                "完了を待ってからもう一度実行してください。"
                "急ぐ場合は該当の ollama pull を止めてください"
            ),
            detail=detail,
        )
    if traffic.total >= OLLAMA_BUSY_REQUESTS:
        return Finding(
            "注意",
            f"Ollamaへ大量のリクエストが流れています(直近{window_minutes}分で{traffic.total}件)",
            advice=(
                "この状態では議事録処理やコメント生成が順番待ちで固まる。"
                "件数の多いエンドポイントの発生源を止めてから実行してください"
            ),
            detail=detail,
        )
    if traffic.server_waits >= OLLAMA_WAIT_WARNING:
        return Finding(
            "注意",
            f"モデルの応答待ちが頻発しています({traffic.server_waits}回)",
            advice=(
                "VRAM不足でモデルの入れ替えが繰り返されている可能性がある。"
                "使っていないモデルを ollama stop で降ろしてください"
            ),
            detail=detail,
        )
    return Finding(
        "OK", f"Ollamaの処理待ちなし(直近{window_minutes}分 {traffic.total}件)", detail=detail
    )


def commit_charge_finding(used_gb: float, limit_gb: float, worst: str = "") -> Finding:
    """
    コミット(仮想メモリ)の逼迫を判定する。

    原因が何であれ、ここが埋まるとWindowsがページングし続けてPC全体が止まる。
    2026-08-14に99.8%まで達し、モデル一覧の取得すら4分かかる状態になった。
    物理メモリの空きだけ見ても捕まらない(ファイルキャッシュと区別できない)。
    """
    ratio = used_gb / limit_gb if limit_gb else 0.0
    detail = f"使用 {used_gb:.1f}GB / 上限 {limit_gb:.1f}GB ({ratio * 100:.0f}%)"
    if worst:
        detail += f" / 最大の占有: {worst}"
    if ratio >= COMMIT_CRITICAL_RATIO:
        return Finding(
            "異常",
            f"メモリのコミットが限界に近づいています({ratio * 100:.0f}%)",
            advice=(
                "このままPC全体が数分単位で応答しなくなります。"
                "python tools\\ollama_watchdog.py --fix でOllamaを再起動して解放できます"
            ),
            detail=detail,
        )
    if ratio >= COMMIT_WARNING_RATIO:
        return Finding(
            "注意",
            f"メモリのコミットが増えています({ratio * 100:.0f}%)",
            advice="大量のembedやモデル切替が続いていないか、下の項目を確認してください",
            detail=detail,
        )
    return Finding("OK", f"メモリのコミット {ratio * 100:.0f}%", detail=detail)


def check_commit_charge() -> list[Finding]:
    """コミット逼迫とその最大占有プロセスを見る(読み取り専用)。"""
    script = (
        "$os = Get-CimInstance Win32_OperatingSystem;"
        "$p = Get-CimInstance Win32_Process | Sort-Object PageFileUsage -Descending |"
        " Select-Object -First 1 Name, PageFileUsage;"
        "'{0}|{1}|{2}|{3}' -f (($os.TotalVirtualMemorySize-$os.FreeVirtualMemory)/1MB),"
        " ($os.TotalVirtualMemorySize/1MB), $p.Name, ($p.PageFileUsage/1MB)"
    )
    result = _run_command(["powershell", "-NoProfile", "-Command", script], timeout=30)
    parts = (result.stdout or "").strip().splitlines()
    if not parts or parts[-1].count("|") != 3:
        return [Finding("情報", "メモリのコミット状況を取得できませんでした")]
    used, limit, name, worst_gb = parts[-1].split("|")
    try:
        return [
            commit_charge_finding(
                float(used), float(limit), f"{name} {float(worst_gb):.1f}GB"
            )
        ]
    except ValueError:
        return [Finding("情報", "メモリのコミット状況を解釈できませんでした")]


def check_ollama_backlog(
    log_dir: Path | None = None, now: datetime | None = None
) -> list[Finding]:
    """処理が溜まって固まっていないかを見る(読み取り専用)。"""
    directory = log_dir if log_dir is not None else OLLAMA_LOG_DIR
    lines = _read_ollama_log_tail(directory)
    if not lines:
        return [
            Finding(
                "情報",
                "Ollamaのログが読めないため処理の滞留を判定できません",
                detail=str(directory),
            )
        ]
    since = (now or datetime.now().astimezone()) - timedelta(
        minutes=OLLAMA_BACKLOG_WINDOW_MINUTES
    )
    return [ollama_backlog_finding(summarize_ollama_traffic(lines, since))]


def check_ollama_models() -> list[Finding]:
    """モデル在庫だけを見る。接続不能は check_ollama_connection が報告済みのため黙る。"""
    import httpx

    try:
        response = httpx.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=3)
        names = {model.get("name", "") for model in response.json().get("models", [])}
    except Exception:
        return []
    findings = [Finding("OK", f"Ollama (モデル{len(names)}個)")]
    for model, purpose in REQUIRED_MODELS.items():
        if model not in names:
            findings.append(
                Finding("異常", f"必須モデルがありません: {model}", detail=purpose,
                        advice="D:\\offline-kit\\ollama のバックアップから復元してください")
            )
    for model, purpose in MAINTENANCE_MODELS.items():
        if model not in names:
            findings.append(Finding("注意", f"保守用モデルがありません: {model}", detail=purpose))
    return findings


# ログオン時に自動起動するタスク。ハブと文章処理ブリッジを1プロセスで持つ
LOCAL_SERVICES_TASK = "minutes-pipeline-local-services"
_START_SERVICES_ADVICE = (
    "ローカルサービスが起動していません。"
    f"タスク {LOCAL_SERVICES_TASK} を実行するか、"
    "text-processing-bridge\\.venv\\Scripts\\python.exe tools\\start_local_services.py "
    "を実行してください"
)


def check_local_services() -> list[Finding]:
    """
    Windows上のローカルサービス(ハブ・文章処理ブリッジ)の稼働確認。

    2026-08-14 に Dify を撤去した際、ブリッジの相手がWindows側のOllamaだけに
    なったためコンテナから降ろした。以後どちらもこのPC上の1プロセスで動く。
    """
    return [
        check_http("ハブ(port 8010)", "http://localhost:8010/health", _START_SERVICES_ADVICE),
        check_http("文章処理ブリッジ(port 8008)", "http://localhost:8008/health", _START_SERVICES_ADVICE),
    ]


def check_docker() -> list[Finding]:
    """Open WebUIだけがコンテナで動く。他はWindows上のプロセス。"""
    listing = _run_command(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"], timeout=20)
    if listing.returncode != 0:
        return [
            Finding(
                "異常",
                "Dockerが利用できません",
                advice="Docker Desktop を起動し、もう一度実行してください",
                detail=listing.stderr.strip().splitlines()[0] if listing.stderr.strip() else "",
            )
        ]
    status = dict(
        line.split("\t", 1) for line in listing.stdout.splitlines() if "\t" in line
    )
    advice = "Docker Desktop から open-webui を起動してください"
    if "open-webui" not in status:
        return [Finding("異常", "コンテナが動いていません: open-webui", advice=advice)]
    if "unhealthy" in status["open-webui"]:
        return [
            Finding("異常", "コンテナが不調です: open-webui", detail=status["open-webui"], advice=advice)
        ]
    return [Finding("OK", "コンテナ稼働: open-webui", detail=status["open-webui"])]


# Pipe/Toolが使っているOpen WebUIの内部API。公開APIではないため、
# 本体のバージョンを上げると予告なく消える・名前が変わることがある。
# ここが欠けるとPipeは「登録は成功しているのに実行時だけ落ちる」状態になる。
WEBUI_INTERNAL_PROBE = """
import inspect, json, os
os.environ.setdefault("WEBUI_SECRET_KEY", "doctor-probe")
missing = []
for module, name in (
    ("open_webui.models.users", "Users"),
    ("open_webui.models.files", "Files"),
    ("open_webui.storage.provider", "Storage"),
    ("open_webui.routers.files", "upload_file_handler"),
    ("starlette.datastructures", "UploadFile"),
    ("starlette.datastructures", "Headers"),
):
    try:
        loaded = __import__(module, fromlist=[name])
        getattr(loaded, name)
    except Exception as error:
        missing.append(f"{module}.{name} ({type(error).__name__})")
print("DOCTOR_PROBE " + json.dumps({"missing": missing}, ensure_ascii=False))
"""


def check_webui_internal_api() -> list[Finding]:
    """
    Pipeが依存するOpen WebUIの内部APIが、今のバージョンにも在るかを見る。

    Pipeはコンテナ内で実行されるため、ここが欠けても登録・デプロイは成功する。
    実際にファイルを作る操作を試すまで気づけないので、先に確かめる。
    """
    result = _run_command(
        # WEBUI_SECRET_KEY はプロセス起動前に要る(import時に検査される)。
        # 確認用の使い捨てで、保存も送信もしない
        [
            "docker", "exec", "-e", "WEBUI_SECRET_KEY=doctor-probe",
            "open-webui", "python", "-c", WEBUI_INTERNAL_PROBE,
        ],
        timeout=60,
    )
    output = (result.stdout or "") + (result.stderr or "")
    line = next((l for l in output.splitlines() if l.startswith("DOCTOR_PROBE ")), "")
    if not line:
        return [
            Finding(
                "情報",
                "Open WebUIの内部APIを確認できませんでした",
                detail=output.strip().splitlines()[-1][:120] if output.strip() else "",
            )
        ]
    try:
        missing = json.loads(line[len("DOCTOR_PROBE "):]).get("missing") or []
    except ValueError:
        return [Finding("情報", "Open WebUIの内部API確認の結果を解釈できませんでした")]
    if missing:
        return [
            Finding(
                "異常",
                f"Pipeが使うOpen WebUIの内部APIが見つかりません({len(missing)}件)",
                advice=(
                    "Open WebUIの更新で内部構成が変わった可能性があります。"
                    "ファイル添付を伴う操作が実行時に失敗します。"
                    "切り戻す場合は D:\\offline-kit\\docker のイメージtarから復元してください"
                ),
                detail="、".join(missing),
            )
        ]
    return [Finding("OK", "Pipeが使うOpen WebUIの内部APIは揃っています")]


def registered_plugin_findings(
    registered: dict[str, str], local: dict[str, str]
) -> list[Finding]:
    """
    Open WebUIの登録内容とリポジトリを突き合わせる。

    管理画面から直接貼れてしまうため、リポジトリに実体の無いプラグインが
    生まれる(2026-08-14に transcription_refiner で実際に起きた)。
    そうなるとレビューも復元もできないため、ここで気づけるようにする。
    """
    findings: list[Finding] = []
    orphans = sorted(set(registered) - set(local))
    if orphans:
        findings.append(
            Finding(
                "注意",
                f"リポジトリに実体の無い登録があります: {'、'.join(orphans)}",
                advice=(
                    "管理画面から直接貼られたものです。中身を tools/ へ回収するか、"
                    "不要なら登録を外してください"
                ),
            )
        )
    drifted = sorted(
        name for name, content in local.items()
        if name in registered and registered[name] != content
    )
    if drifted:
        findings.append(
            Finding(
                "注意",
                f"登録内容がリポジトリと食い違っています: {'、'.join(drifted)}",
                advice=(
                    "python tools\\open_webui_deploy.py tools\\<対象>.py で差分を確認し、"
                    "リポジトリを正とするなら --apply で反映してください"
                ),
            )
        )
    if not findings:
        unregistered = sorted(set(local) - set(registered))
        detail = (
            f"リポジトリにあるが未登録: {'、'.join(unregistered)}" if unregistered else ""
        )
        findings.append(
            Finding(
                "OK",
                f"Open WebUI登録内容とリポジトリが一致(登録{len(registered)}件)",
                detail=detail,
            )
        )
    return findings


def _normalized_plugin_source(text: str) -> str:
    """改行コードと末尾の空白を揃えてから比較する。"""
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip()


def _uncommitted_paths(root: Path) -> set[str]:
    """未コミットのファイルをリポジトリ相対のパスで返す(取得できなければ空)。"""
    result = _run_command(["git", "-C", str(root), "status", "--porcelain"])
    if result.returncode != 0:
        return set()
    paths: set[str] = set()
    for line in result.stdout.splitlines():
        if len(line) <= 3:
            continue
        name = line[3:].strip()
        if " -> " in name:  # 改名は移動先を見る
            name = name.split(" -> ", 1)[1]
        paths.add(name.strip('"'))
    return paths


def uncommitted_registration_findings(
    registered_files: dict[str, str], uncommitted: set[str]
) -> list[Finding]:
    """
    登録済みなのに、その元ファイルがコミットされていないものを知らせる。

    登録内容とファイルが一致していても、ファイルがコミットされていなければ
    「本番で動いているコードがGitのどこにも無い」状態になる。DEMO.mdの全戻し
    (git checkout -- .)で復元不能になるため、ここで気づけるようにする
    (2026-08-18に実際に発生。「登録の出所はリポジトリ」という決め事が崩れる)。
    """
    exposed = sorted(name for name, rel in registered_files.items() if rel in uncommitted)
    if not exposed:
        return []
    return [
        Finding(
            "注意",
            f"登録済みだがコミットされていないものがあります: {'、'.join(exposed)}",
            advice=(
                "いまOpen WebUIで動いている内容がGitに残っていません。"
                "中身を確認してコミットしてください(git checkout -- . で消えます)"
            ),
            detail="、".join(sorted(registered_files[name] for name in exposed)),
        )
    ]


def _toolpack_store(root: Path):
    """追加ツールの保存領域を開く。基盤が未導入なら None(従来どおり動く)。"""
    try:
        sys.path.insert(0, str(root / "tools"))
        import toolpack_store

        return toolpack_store.ToolpackStore(root / "additional-tools")
    except Exception:
        return None


def _active_generated_pipes(root: Path) -> list[Path]:
    """registry の有効版の generated/ ディレクトリを返す。"""
    store = _toolpack_store(root)
    if store is None:
        return []
    try:
        return [
            store.generated_dir(tool_id, info["version"])
            for tool_id, info in store.active_tools().items()
        ]
    except Exception:
        return []


def expected_display_name(display_name: str, approved: bool) -> str:
    """台帳の承認状態から、画面に出ているべき表示名を決める。

    承認状態は **`toolpack_store.is_approved` で判定したもの**を渡すこと。
    ここで `status` を直接読むと、承認が版ごとになったとき(D-14)に
    場所ごとで表示がずれる。判定は1か所に集める。
    """
    prefix = "【未承認】"
    base = display_name[len(prefix):] if display_name.startswith(prefix) else display_name
    return base if approved else prefix + base


def toolpack_name_findings(expected: dict[str, str], actual: dict[str, dict]) -> list[Finding]:
    """**承認の表示が書き換えられていないか**を見る(純粋関数)。

    `【未承認】` の印は管理画面から数クリックで消せる。消されると、承認を経て
    いないツールが正式なものに見える。技術的には壊れないが、
    **台帳の承認状態と画面の食い違いは必ず見えるようにする**
    (台帳 D-12。「失敗を成功として扱わない」と同じ理由)。

    actual は {tool_id: {"function": 名前, "model": 名前}}。
    """
    findings: list[Finding] = []
    for tool_id, should_be in sorted(expected.items()):
        names = actual.get(tool_id) or {}
        drifted = {
            where: name for where, name in names.items()
            if name is not None and name != should_be
        }
        if not drifted:
            continue
        detail = " / ".join(f"{where}: {name!r}" for where, name in sorted(drifted.items()))
        findings.append(Finding(
            "注意",
            f"承認の表示が台帳と違います: {tool_id}",
            detail=f"あるべき名前: {should_be!r} / 実際: {detail}",
            advice=(
                "管理画面で表示名が書き換えられた可能性があります。"
                "承認を経ていないツールが正式なものに見えるため、"
                "python tools\\open_webui_deploy.py <生成Pipe> --apply で戻してください"
            ),
        ))
    return findings


def check_toolpack_names(root: Path = ROOT) -> list[Finding]:
    """追加ツールの表示名が、台帳の承認状態と合っているかを見る(読み取りのみ)。"""
    store = _toolpack_store(root)
    if store is None or not store.registry_path.exists():
        return []
    try:
        active = store.active_tools()
        if not active:
            return []
        expected: dict[str, str] = {}
        for tool_id, info in active.items():
            meta_path = store.package_dir(tool_id, info["version"]) / "tool.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            import toolpack_store as _store

            expected[tool_id] = expected_display_name(
                str(meta.get("display_name") or tool_id),
                _store.is_approved(info["entry"], info["version"]),
            )
    except Exception as error:
        return [Finding(
            "注意", "追加ツールの表示名を確認できません",
            detail=f"{type(error).__name__}: {error}",
        )]

    try:
        sys.path.insert(0, str(root / "tools"))
        from open_webui_deploy import ApiClient, load_api_key, resolve_base_url

        client = ApiClient(base_url=resolve_base_url(), api_key=load_api_key())
        functions = {entry["id"]: entry.get("name") for entry in client.list_entries("pipe")}
        actual = {
            tool_id: {
                "Function": functions.get(tool_id),
                "モデル設定": (client.get_model(tool_id) or {}).get("name"),
            }
            for tool_id in expected
        }
    except Exception as error:
        return [Finding(
            "注意", "追加ツールの表示名をOpen WebUIで確認できません",
            detail=f"{type(error).__name__}: {error}",
            advice="未確認であり、正常とは限りません",
        )]

    findings = toolpack_name_findings(expected, actual)
    if not findings:
        findings.append(Finding("OK", "追加ツールの承認表示は台帳どおり"))
    return findings


def toolpack_consistency_findings(
    active: dict[str, dict], on_disk: set[tuple[str, str]], registered: set[str] | None,
    orphans: list[tuple[str, str]],
) -> list[Finding]:
    """registry・ディスク・Open WebUI の三面照合(純粋関数)。

    **管理アプリの外で登録されたモデルは「管理外」として報告するだけ**にし、
    変更も削除もしない(台帳 D-11)。孤立版も削除せず報告に留める(D-3)。
    `registered` が None のときは Open WebUI 側を確認できなかった場合で、
    **確認できなかったことを「正常」として扱わない。**
    """
    findings: list[Finding] = []
    for tool_id, info in sorted(active.items()):
        version = info["version"]
        if (tool_id, version) not in on_disk:
            findings.append(Finding(
                "異常", f"台帳にあるのに実体がありません: {tool_id} {version}",
                advice="追加ツールを入れ直してください(registry から外すまで繋がりません)",
            ))
        elif registered is None:
            findings.append(Finding(
                "注意", f"Open WebUIへの登録を確認できません: {tool_id} {version}",
                advice="Open WebUIの稼働と管理APIキーを確かめてください(未確認であり、正常とは限りません)",
            ))
        elif tool_id not in registered:
            findings.append(Finding(
                "注意", f"台帳にあるのにOpen WebUIへ登録されていません: {tool_id}",
                advice="管理アプリから登録し直してください(モデル一覧に出ません)",
            ))
        else:
            findings.append(Finding("OK", f"追加ツール {tool_id} {version}"))
    if orphans:
        findings.append(Finding(
            "情報",
            f"台帳の有効版でないものが残っています: "
            f"{'、'.join(f'{t} {v}' for t, v in sorted(orphans))}",
            advice="以前の版です。戻せるよう残してあります(自動では消しません)",
        ))
    return findings


def check_toolpack_consistency(root: Path = ROOT) -> list[Finding]:
    """追加ツールの registry・ディスク・Open WebUI の食い違いを見る(読み取りのみ)。"""
    store = _toolpack_store(root)
    if store is None or not store.registry_path.exists():
        return []  # 追加ツール基盤を使っていない環境では何も言わない
    try:
        active = store.active_tools()
        on_disk = {
            (tool_id, version_dir.name)
            for tool_id, info in active.items()
            for version_dir in [store.version_dir(tool_id, info["version"])]
            if version_dir.is_dir()
        }
        orphans = [(item.tool_id, item.version) for item in store.orphan_versions()]
    except Exception as error:
        return [Finding("注意", "追加ツールの台帳を読めません", detail=f"{type(error).__name__}: {error}")]

    try:
        sys.path.insert(0, str(root / "tools"))
        from open_webui_deploy import ApiClient, load_api_key, resolve_base_url

        client = ApiClient(base_url=resolve_base_url(), api_key=load_api_key())
        registered: set[str] | None = {entry["id"] for entry in client.list_entries("pipe")}
    except Exception:
        # **未確認を「登録済み」とみなさない。** 見られなかったことを見えるようにする
        registered = None

    return toolpack_consistency_findings(active, on_disk, registered, orphans)


def check_approval_policy(root: Path = ROOT) -> list[Finding]:
    """承認の決まり(approval_policy.json)が使える状態かを見る(台帳 D-14)。

    **壊れていても、承認ボタンを押すまで気づけない**のでは遅い。
    開発担当者がいない運用を目指すので、自己診断で先に知らせる。
    """
    store = _toolpack_store(root)
    if store is None:
        return []   # 追加ツール基盤を使っていない環境では何も言わない
    try:
        policy = store.load_policy()
    except Exception as error:
        return [Finding(
            "注意", "承認の決まりを読めません",
            advice="additional-tools/registry/approval_policy.json を確かめてください",
            detail=f"{type(error).__name__}: {error}",
        )]
    if not policy.usable:
        return [Finding(
            "注意", "承認機能を利用できません(設定が読めません)",
            advice=(
                "additional-tools/registry/approval_policy.json を直すか、"
                "ファイルごと消してください(消せば2人/2人・自由入力に戻ります)"
            ),
            detail="、".join(policy.problems),
        )]

    # 確定していない票が残っていないか(人数を下げたときに起きる)
    stuck: list[str] = []
    try:
        for tool_id, entry in (store.load_registry().get("tools") or {}).items():
            version = str(entry.get("active_version") or "")
            if not version:
                continue
            if store.approval_state(tool_id, version).get("ready"):
                stuck.append(f"{tool_id} {version}")
    except Exception as error:
        # **確認できなかったことを正常扱いしない**(全体の方針と同じ)。
        # ここで握り潰すと、台帳が壊れていても「承認の決まりはOK」と出てしまう
        return [Finding(
            "注意", "承認の状態を確認できません",
            advice="additional-tools/registry/registry.json を確かめてください",
            detail=f"{type(error).__name__}: {error}",
        )]
    if stuck:
        return [Finding(
            "注意", "承認の票が人数に届いたまま確定していません",
            advice="管理画面で「承認する」か「承認を取り消す」を一度押すと確定します",
            detail="、".join(stuck),
        )]

    where = "既定(2人/2人・自由入力)" if not policy.present else "設定ファイルどおり"
    return [Finding(
        "OK",
        f"承認の決まり: 承認{policy.approvals_required}人 / "
        f"取消{policy.revocations_required}人({where})",
    )]


# バックアップを取っていないと注意にする日数(台帳 D-17)
BACKUP_WARN_DAYS = 30


def check_toolpack_backup(root: Path = ROOT) -> list[Finding]:
    """追加ツール領域を外部媒体へ書き出しているかを見る(台帳 D-17)。

    `additional-tools/` は Git 管理外で `push local` に乗らず、
    その `push local` の宛先も同じドライブにある。
    **承認の記録はここにしか無い**ので、取っていないなら知らせる。
    """
    store = _toolpack_store(root)
    if store is None:
        return []
    try:
        sys.path.insert(0, str(root / "tools"))
        import toolpack_backup

        record = toolpack_backup.last_backup(store)
    except Exception as error:
        return [Finding(
            "情報", "バックアップの記録を読めません", detail=type(error).__name__,
        )]

    if record is None:
        # 何も入っていないなら、まだ取る必要は無い
        try:
            if not (store.load_registry().get("tools") or {}):
                return []
        except Exception:
            return []
        return [Finding(
            "注意", "追加ツール領域のバックアップを取っていません",
            advice=(
                "USBメモリ等へ書き出してください: "
                "python tools/toolpack_backup.py save <保存先>"
            ),
            detail="承認の記録はここにしかなく、ディスクが壊れると戻せません",
        )]

    try:
        taken = datetime.strptime(str(record["at"]), "%Y-%m-%dT%H:%M:%S")
    except (ValueError, KeyError):
        return [Finding("注意", "バックアップの記録が読めません", detail=str(record))]
    days = (datetime.now() - taken).days
    where = str(record.get("target") or "")
    if days >= BACKUP_WARN_DAYS:
        return [Finding(
            "注意", f"追加ツール領域のバックアップが {days} 日前です",
            advice="USBメモリ等へ取り直してください",
            detail=where,
        )]
    return [Finding("OK", f"追加ツール領域のバックアップ: {days} 日前", detail=where)]


LOCAL_SERVICE_PORTS = (8010, 8008)


def own_lan_addresses() -> list[str]:
    """この端末のIPv4アドレス(折り返しと自動割り当てを除く)。"""
    import socket

    found: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address.startswith(("127.", "169.254.")):
                continue
            if address not in found:
                found.append(address)
    except OSError:
        pass
    return found


def check_service_exposure(ports=LOCAL_SERVICE_PORTS) -> list[Finding]:
    """**ローカルサービスが院内LANから届いていないか**を見る(台帳 D-16)。

    自分のLAN側アドレスへ実際に繋いでみる。繋がるなら 0.0.0.0 で待っており、
    他端末から追加ツールを実行できる状態である。
    折り返しだけで待っていれば拒否される(それが正しい姿)。
    """
    import socket

    addresses = own_lan_addresses()
    if not addresses:
        return [Finding("情報", "LAN側アドレスが分からないため露出を確認できません")]

    exposed: list[str] = []
    for port in ports:
        for address in addresses:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(1.0)
                try:
                    if probe.connect_ex((address, port)) == 0:
                        exposed.append(f"{address}:{port}")
                except OSError:
                    continue
    if exposed:
        return [Finding(
            "注意",
            "ローカルサービスが院内LANから届きます",
            advice=(
                "start_local_services.py を 127.0.0.1 で待たせてください"
                "(既定はそうなっています)。0.0.0.0 で待つと、他端末から"
                "追加ツールを実行できます"
            ),
            detail="、".join(exposed),
        )]
    return [Finding("OK", "ローカルサービスは端末内からのみ")]


def check_toolpack_runner(root: Path = ROOT) -> list[Finding]:
    """追加ツール用ランナーのガードが実際に効いているかを確かめる。"""
    store = _toolpack_store(root)
    if store is None or not store.registry_path.exists():
        return []
    try:
        sys.path.insert(0, str(root / "tools"))
        import toolpack_runner

        if toolpack_runner.self_check():
            return [Finding("OK", "追加ツールの実行ガード(監査フック・低整合性)")]
        return [Finding(
            "異常", "追加ツールの実行ガードが効いていません",
            advice="禁止操作が素通りする状態です。追加ツールを止めて管理者へ連絡してください",
        )]
    except Exception as error:
        return [Finding(
            "注意", "追加ツールの実行ガードを確認できません",
            detail=f"{type(error).__name__}: {error}",
        )]


def check_registered_plugins(root: Path = ROOT) -> list[Finding]:
    """登録済みPipe/Toolとリポジトリの照合(読み取りのみ)。"""
    sys.path.insert(0, str(root / "tools"))
    try:
        from open_webui_deploy import (
            ApiClient,
            load_api_key,
            parse_frontmatter,
            resolve_base_url,
            resolve_plugin_id,
        )
    except Exception as error:
        return [Finding("情報", "登録内容を照合できません", detail=type(error).__name__)]

    try:
        client = ApiClient(base_url=resolve_base_url(), api_key=load_api_key())
        registered: dict[str, str] = {}
        for prefix in ("/api/v1/functions", "/api/v1/tools"):
            for entry in client.request("GET", f"{prefix}/") or []:
                detail = client.request("GET", f"{prefix}/id/{entry['id']}") or {}
                registered[entry["id"]] = _normalized_plugin_source(detail.get("content") or "")
    except Exception as error:
        return [
            Finding(
                "情報",
                "Open WebUIの登録内容を取得できないため照合を省略しました",
                detail=f"{type(error).__name__}: {str(error).splitlines()[0][:100]}",
            )
        ]

    local: dict[str, str] = {}
    local_files: dict[str, str] = {}
    # tools/ 直下に加えて、追加ツールの**有効版**の生成Pipeも照合対象にする
    # (registry に無い版まで拾うと、同じidの新旧2版が衝突して誤った版と比べてしまう)
    candidates = list((root / "tools").glob("open*webui*.py"))
    for generated in _active_generated_pipes(root):
        candidates.extend(sorted(generated.glob("open*webui*.py")))
    for path in sorted(candidates):
        text = path.read_text(encoding="utf-8")
        try:
            plugin_id = resolve_plugin_id(parse_frontmatter(text), None)
        except Exception:
            continue  # id: の無いファイル(デプロイ対象外)は照合しない
        local[plugin_id] = _normalized_plugin_source(text)
        local_files[plugin_id] = path.relative_to(root).as_posix()
    findings = registered_plugin_findings(registered, local)
    findings += uncommitted_registration_findings(
        {name: rel for name, rel in local_files.items() if name in registered},
        _uncommitted_paths(root),
    )
    return findings


# 復旧資材の置き場。オフライン後はここからしか戻せない
BACKUP_ROOT = Path("D:/offline-kit")


def backup_gap_findings(
    live_models: set[str], backed_models: set[str], stale_configs: list[str]
) -> list[Finding]:
    """
    復旧資材のバックアップが実体から遅れていないかを判定する(純粋関数)。

    2026-08-18に、主力モデル(qwen3.8・qwen3.6・qwen3-vl)がどこにも
    バックアップされていない状態が見つかった。バックアップは「取った日」で
    止まっていても誰も気づかない。オフライン後はモデルもプラグインも
    取り直せないため、遅れを見えるようにしておく。
    """
    findings: list[Finding] = []
    missing = sorted(live_models - backed_models)
    if missing:
        findings.append(
            Finding(
                "注意",
                f"バックアップに無いモデルがあります: {'、'.join(missing)}",
                advice=(
                    "オフライン後は取り直せません。robocopy で "
                    r"D:\offline-kit\ollama\models へ複製してください"
                ),
            )
        )
    if stale_configs:
        findings.append(
            Finding(
                "注意",
                f"バックアップが実体と違います: {'、'.join(stale_configs)}",
                advice="実体をコピーし直してください(壊れたときの戻し先です)",
            )
        )
    if not findings:
        findings.append(
            Finding("OK", f"復旧資材のバックアップは最新(モデル{len(live_models)}件)")
        )
    return findings


def check_backups() -> list[Finding]:
    """モデルとKilo設定のバックアップが実体に追いついているか(読み取りのみ)。"""
    live_dir = Path.home() / ".ollama" / "models" / "manifests" / "registry.ollama.ai" / "library"
    backup_dir = (
        BACKUP_ROOT / "ollama" / "models" / "manifests" / "registry.ollama.ai" / "library"
    )
    if not live_dir.is_dir() or not backup_dir.is_dir():
        return [Finding("情報", "バックアップの照合を省略しました(置き場が見つかりません)")]
    live = {item.name for item in live_dir.iterdir() if item.is_dir()}
    backed = {item.name for item in backup_dir.iterdir() if item.is_dir()}

    stale: list[str] = []
    pairs = [
        (
            Path.home() / ".config" / "kilo" / "kilo.jsonc",
            BACKUP_ROOT / "kilo" / "kilo.global.jsonc",
            "Kiloのグローバル設定",
        ),
    ]
    for source, backup, label in pairs:
        if not source.is_file():
            continue
        if not backup.is_file() or backup.read_bytes() != source.read_bytes():
            stale.append(label)
    return backup_gap_findings(live, backed, stale)


def check_required_files(root: Path = ROOT) -> list[Finding]:
    findings: list[Finding] = []
    checks: list[tuple[Path, str, str]] = [
        (root / "templates", "注意", "議事録テンプレート置き場(templates/)"),
        (root / "text-processing-bridge" / "samples" / "meeting.txt", "注意", "テスト用サンプル(samples/meeting.txt)"),
        (root / "text-processing-bridge" / "wheelhouse-win", "注意", "オフライン再構築用wheel(wheelhouse-win/)"),
        (root / "offline-docs", "注意", "オフラインドキュメント(offline-docs/)"),
        (Path("D:/offline-kit"), "注意", "復旧資材(D:/offline-kit)"),
    ]
    for path, level_when_missing, label in checks:
        if path.exists():
            findings.append(Finding("OK", label))
        else:
            findings.append(
                Finding(level_when_missing, f"{label} が見つかりません", detail=str(path))
            )
    return findings


# Kiloがコードを書くための道具。オフライン後は D:/offline-kit からしか戻せない。
# 「入っているか」ではなく「実行できるか」を見る(PATHが通っていないと
# tsc も playwright も動かず、実際にそれで一度つまずいた。2026-08-18)
LANGUAGE_TOOLS = [
    ("node", ["node", "--version"], "JavaScript / TypeScriptの実行"),
    ("java", ["java", "-version"], "Java(D:/offline-kit/java に展開)"),
    ("gcc", ["gcc", "--version"], "C / C++(D:/offline-kit/mingw に展開)"),
]
# 取り直せない資材。オフライン化後に欠けていると詰む
OFFLINE_MATERIALS = [
    (Path("D:/offline-kit/node-tools/node_modules"), "TypeScript・Playwright一式"),
    (Path("D:/offline-kit/playwright-browsers"), "Playwrightのブラウザ実体(約700MB)"),
    (Path("D:/offline-kit/docs/web"), "Web仕様の資料(MDN)"),
]


def check_language_tools() -> list[Finding]:
    """Kiloが扱える言語の道具が、PATH越しに実際に動くかを見る。"""
    missing: list[str] = []
    for name, command, purpose in LANGUAGE_TOOLS:
        if shutil.which(name) is None:
            missing.append(f"{name}({purpose})")
    if missing:
        return [
            Finding(
                "注意",
                f"PATHから使えない開発ツールがあります: {len(missing)}件",
                advice=(
                    "ユーザー環境変数のPATHへ実行ファイルの場所を足してください"
                    "(node・java・gcc)。入っていても PATH に無いと Kilo から使えません"
                ),
                detail="、".join(missing),
            )
        ]
    return [Finding("OK", f"開発ツールが使えます({len(LANGUAGE_TOOLS)}種)")]


def check_webui_customizations() -> list[Finding]:
    """
    画面のカスタマイズ(custom.css・Excelガード・操作案内)が配信されているか。

    staticフォルダは一括で初期状態へ戻されることがある(2026-08-18に実測。
    index.htmlは無傷のまま全ファイルが同一時刻で上書きされた)。有無だけでなく
    中身も見ないと、0バイトで200が返る状態を見逃す。判定は
    tools/webui_static_guard.py が持つ(同じ処理を2箇所に書かない)。
    """
    try:
        import webui_static_guard
    except Exception as error:  # 見張りが壊れていても診断は続ける
        return [Finding("情報", f"画面カスタマイズの確認を省略しました({error})")]
    try:
        found = webui_static_guard.check()
    except Exception as error:
        return [Finding("情報", f"画面カスタマイズを確認できませんでした({error})")]
    if found:
        return [
            Finding(
                "注意",
                f"画面のカスタマイズが消えています: {len(found)}件",
                advice=(
                    "python tools\\webui_static_guard.py --fix で戻せます"
                    "(30分ごとのタスク minutes-pipeline-webui-static-guard も自動で戻します)"
                ),
                detail=" / ".join(f"{item.label}: {reason}" for item, reason in found),
            )
        ]
    return [Finding("OK", "画面のカスタマイズは配信されています")]


def check_offline_materials() -> list[Finding]:
    """オフライン後に取り直せない資材が揃っているか(存在確認のみ)。"""
    missing = [f"{label}: {path}" for path, label in OFFLINE_MATERIALS if not path.exists()]
    if missing:
        return [
            Finding(
                "注意",
                f"オフライン用の資材が欠けています: {len(missing)}件",
                advice=(
                    "ネットに繋がっているうちに取り直してください。"
                    "切断後は入手できません"
                ),
                detail=" / ".join(missing),
            )
        ]
    return [Finding("OK", f"オフライン用の開発資材({len(OFFLINE_MATERIALS)}件)")]


def check_log_directory(root: Path = ROOT) -> list[Finding]:
    log_dir = root / "text-processing-bridge" / "logs"
    if not log_dir.exists():
        return [Finding("注意", "運用ログはまだ作成されていません(初回リクエストで自動作成)", detail=str(log_dir))]
    if not os.access(log_dir, os.W_OK):
        return [Finding("異常", "運用ログ保存先へ書き込めません", detail=str(log_dir))]
    log_file = log_dir / "operations.jsonl"
    size_kb = log_file.stat().st_size / 1024 if log_file.exists() else 0
    return [Finding("OK", "運用ログ保存先", detail=f"{log_file} ({size_kb:.0f}KB)")]


def check_deep(base_url: str = "http://localhost:8008") -> list[Finding]:
    """
    実処理の疎通(--deep)。架空の短い固定文をbridge→Ollamaへ通す。

    コンテナがUpでも実行だけ死ぬ障害はここでしか検出できない。
    実会議データは絶対に使わない。
    """
    import httpx

    print("実処理テスト中(bridge→Ollamaを実行。1分程度かかることがあります)...", flush=True)
    started = time.perf_counter()
    try:
        response = httpx.post(
            f"{base_url}/v1/text/rewrite",
            json={"text": "これは診断用の短い文章です。動作確認のため整形してください。"},
            timeout=120,
        )
    except Exception as error:
        return [
            Finding(
                "異常",
                "実処理(bridge→Ollama)へ接続できません",
                advice="上のbridge・Ollamaの各項目の対処を先に行ってください",
                detail=type(error).__name__,
            )
        ]
    elapsed = time.perf_counter() - started
    if response.status_code == 200 and (response.json().get("text") or "").strip():
        return [Finding("OK", f"実処理(bridge→Ollama) {elapsed:.1f}秒で成功")]
    advice = {
        502: (
            "ブリッジからOllamaへ届いていません。上のOllama項目と、"
            "コンテナの OLLAMA_BASE_URL(既定 http://host.docker.internal:11434)を確認してください"
        ),
        503: "生成モデルが見つかりません。上の必須モデル項目を確認してください",
        504: (
            "処理がタイムアウトしました。Ollamaがモデルをロード中か、"
            "処理が滞留している可能性があります。上の滞留項目を確認してください"
        ),
    }.get(
        response.status_code,
        "運用ログ(text-processing-bridge/logs/operations.jsonl)を確認してください",
    )
    return [
        Finding(
            "異常",
            f"実処理がHTTP {response.status_code}で失敗({elapsed:.1f}秒)",
            advice=advice,
        )
    ]


def check_render(base_url: str = "http://localhost:8008") -> list[Finding]:
    """docx生成の疎通(--render)。LLMを使わず配布ファイル生成と再読込を確かめる。"""
    import base64
    import io

    import httpx

    try:
        response = httpx.post(
            f"{base_url}/v1/render/export",
            json={"format": "docx", "content": "診断用の本文です。", "title": "診断"},
            timeout=30,
        )
        response.raise_for_status()
        docx_bytes = base64.b64decode(response.json()["data_b64"])
        from docx import Document

        Document(io.BytesIO(docx_bytes))  # 生成物が開けることまで確認する
        return [
            Finding("OK", f"docx生成(/v1/render/export) {len(docx_bytes)}バイト・再読込成功")
        ]
    except Exception as error:
        return [
            Finding(
                "異常",
                "docx生成が失敗しました",
                advice="bridgeの稼働状態と運用ログを確認してください",
                detail=type(error).__name__,
            )
        ]


def collect_findings() -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(check_python())
    findings.extend(check_configs())
    findings.extend(check_git())
    findings.extend(check_git_backup())
    findings.extend(check_disk())
    findings.extend(check_commit_charge())
    findings.extend(check_docker())
    findings.extend(check_local_services())
    findings.extend(check_ollama_connection())
    findings.extend(check_ollama_models())
    findings.extend(check_ollama_backlog())
    open_webui_url = resolve_open_webui_url()
    findings.append(
        check_http(
            "Open WebUI",
            open_webui_url,
            "Open WebUI のコンテナが停止しています。Docker Desktop から open-webui を起動してください",
            level_when_down="注意",
        )
    )
    findings.extend(check_registered_plugins())
    findings.extend(check_toolpack_consistency())
    findings.extend(check_toolpack_names())
    findings.extend(check_toolpack_runner())
    findings.extend(check_service_exposure())
    findings.extend(check_approval_policy())
    findings.extend(check_toolpack_backup())
    findings.extend(check_webui_internal_api())
    findings.extend(check_required_files())
    findings.extend(check_language_tools())
    findings.extend(check_offline_materials())
    findings.extend(check_webui_customizations())
    findings.extend(check_log_directory())
    findings.extend(check_backups())
    return findings


def format_report(findings: list[Finding]) -> str:
    lines: list[str] = []
    for finding in findings:
        lines.append(f"[{finding.level}] {finding.title}")
        if finding.advice:
            lines.append(f"  対処: {finding.advice}")
        if finding.detail:
            lines.append(f"  技術情報: {finding.detail}")
    counts = {
        level: sum(1 for f in findings if f.level == level)
        for level in ("異常", "注意", "情報", "OK")
    }
    lines.append("")
    lines.append(
        f"結果: 異常 {counts['異常']}件 / 注意 {counts['注意']}件 / "
        f"情報 {counts['情報']}件 / OK {counts['OK']}件"
    )
    return "\n".join(lines)


def exit_code(findings: list[Finding]) -> int:
    return 1 if any(f.level == "異常" for f in findings) else 0


def main() -> None:
    # 素のpythonで実行されてもvenv(本番実行環境)を検査対象にする。
    # Kiloや先生が `python tools\doctor.py` と打っても誤報しないための保険
    if should_reexec(Path(sys.executable), VENV_PYTHON):
        print(f"[情報] venvのPythonで実行し直します: {VENV_PYTHON}", flush=True)
        result = subprocess.run(
            [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
            check=False,
        )
        raise SystemExit(result.returncode)

    parser = argparse.ArgumentParser(description="オフライン運用の自己診断(読み取り専用)")
    parser.add_argument("--out", help="診断結果をUTF-8で書き出すファイルパス(output/配下を推奨)")
    parser.add_argument("--deep", action="store_true", help="架空の短文をbridge→Ollamaへ実際に流して確認する")
    parser.add_argument("--render", action="store_true", help="docx生成と再読込まで確認する(LLM不使用)")
    args = parser.parse_args()

    findings = collect_findings()
    if args.deep:
        findings.extend(check_deep())
    if args.render:
        findings.extend(check_render())
    report = format_report(findings)
    print(report)
    if args.out:
        out_path = resolve_out_path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report + "\n", encoding="utf-8")
        print(f"レポート保存先: {out_path}")
    raise SystemExit(exit_code(findings))


if __name__ == "__main__":
    main()
