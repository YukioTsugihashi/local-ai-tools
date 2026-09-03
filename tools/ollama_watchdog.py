"""
Ollamaの暴走を検知して、必要なら再起動する見張り役。

  python tools\\ollama_watchdog.py          状態を見るだけ(何も変更しない)
  python tools\\ollama_watchdog.py --fix    危険域なら Ollama を再起動する

doctor.py は「外部状態を一切変更しない」約束なので、実際に手を出す役はこちらへ分ける。

2026-08-14の障害が発端。保存先(qdrant)の無い索引作成がembedを投げ続け、
`ollama serve` のコミットが194GBまで膨らんでコミット率99.8%に到達。
Windowsがページングし続け、モデル一覧の取得すら4分かかる状態になった。
物理メモリの空きだけでは捕まらない(ファイルキャッシュと区別がつかない)ため、
**コミット率**と**Ollamaプロセス単体のコミット量**の両方で判定する。

再起動は安全側の操作(モデルは次の依頼で読み直される)だが、
生成中の応答は失われるため、両方の条件を満たしたときだけ実行する。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "logs" / "ollama_watchdog.log"

# 両方を満たしたときだけ再起動する。片方だけでは正常な高負荷と区別できない
COMMIT_CRITICAL_RATIO = 0.90
OLLAMA_COMMIT_CRITICAL_GB = 40.0
OLLAMA_APP = Path.home() / "AppData" / "Local" / "Programs" / "Ollama" / "ollama app.exe"
OLLAMA_URL = "http://127.0.0.1:11434"


@dataclass
class State:
    commit_used_gb: float
    commit_limit_gb: float
    ollama_commit_gb: float

    @property
    def commit_ratio(self) -> float:
        return self.commit_used_gb / self.commit_limit_gb if self.commit_limit_gb else 0.0

    @property
    def is_critical(self) -> bool:
        return (
            self.commit_ratio >= COMMIT_CRITICAL_RATIO
            and self.ollama_commit_gb >= OLLAMA_COMMIT_CRITICAL_GB
        )

    def summary(self) -> str:
        return (
            f"コミット {self.commit_used_gb:.1f}/{self.commit_limit_gb:.1f}GB "
            f"({self.commit_ratio * 100:.0f}%) / ollama.exe {self.ollama_commit_gb:.1f}GB"
        )


def _powershell(script: str, timeout: int = 60) -> str:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return (result.stdout or "").strip()


def read_state() -> State:
    output = _powershell(
        "$os = Get-CimInstance Win32_OperatingSystem;"
        "$o = Get-CimInstance Win32_Process -Filter \"Name='ollama.exe'\" |"
        " Measure-Object PageFileUsage -Sum;"
        "'{0}|{1}|{2}' -f (($os.TotalVirtualMemorySize-$os.FreeVirtualMemory)/1MB),"
        " ($os.TotalVirtualMemorySize/1MB), ([double]$o.Sum/1MB)"
    )
    used, limit, ollama = (output.splitlines() or ["0|0|0"])[-1].split("|")
    return State(float(used), float(limit), float(ollama or 0))


def restart_ollama() -> list[str]:
    """Ollamaを止めて上げ直す。取り残されるllama-serverも片付ける。"""
    steps = []
    _powershell(
        "Get-Process ollama,'ollama app',llama-server -ErrorAction SilentlyContinue |"
        " Stop-Process -Force"
    )
    steps.append("ollama / llama-server を停止")
    time.sleep(5)
    if OLLAMA_APP.is_file():
        subprocess.Popen([str(OLLAMA_APP)])
        steps.append(f"再起動: {OLLAMA_APP.name}")
    else:
        steps.append(f"再起動できません(見つからない: {OLLAMA_APP})")
        return steps
    for _ in range(30):
        time.sleep(2)
        if _powershell(
            f"try {{ (Invoke-RestMethod '{OLLAMA_URL}/api/version' -TimeoutSec 3).version }}"
            " catch {{ '' }}"
        ):
            steps.append("応答を確認")
            return steps
    steps.append("再起動したが応答を確認できず")
    return steps


def log(entry: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 記録できなくても本処理は続ける


def main() -> None:
    parser = argparse.ArgumentParser(description="Ollamaの暴走を検知して必要なら再起動する")
    parser.add_argument(
        "--fix", action="store_true", help="危険域なら再起動する(既定は確認のみ)"
    )
    args = parser.parse_args()

    state = read_state()
    entry = {
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "commit_ratio": round(state.commit_ratio, 3),
        "commit_used_gb": round(state.commit_used_gb, 1),
        "ollama_commit_gb": round(state.ollama_commit_gb, 1),
        "critical": state.is_critical,
    }
    print(state.summary())

    if not state.is_critical:
        print("判定: 正常範囲")
        log({**entry, "action": "none"})
        raise SystemExit(0)

    print(
        f"判定: 危険域(コミット{COMMIT_CRITICAL_RATIO * 100:.0f}%以上 かつ "
        f"ollama.exe {OLLAMA_COMMIT_CRITICAL_GB:.0f}GB以上)"
    )
    if not args.fix:
        print("--fix を付けると Ollama を再起動して解放します")
        log({**entry, "action": "detected"})
        raise SystemExit(1)

    steps = restart_ollama()
    for step in steps:
        print(f"  - {step}")
    after = read_state()
    print(f"再起動後: {after.summary()}")
    log({**entry, "action": "restarted", "steps": steps, "after_ratio": round(after.commit_ratio, 3)})
    raise SystemExit(0 if not after.is_critical else 1)


if __name__ == "__main__":
    main()
