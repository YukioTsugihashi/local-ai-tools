"""
見張り役の判定境界のテスト。

再起動は生成中の応答を失う操作なので、「Ollamaが原因のときだけ」に絞る。
緩めると正常な高負荷でも落としに行き、厳しすぎると2026-08-14の障害を見逃す。
"""

from __future__ import annotations

import json

import ollama_watchdog as watchdog


def test_actual_incident_is_judged_critical() -> None:
    # 2026-08-14の実測値: コミット99.8%、ollama.exe単体で194GB
    state = watchdog.State(commit_used_gb=230.1, commit_limit_gb=230.5, ollama_commit_gb=194.2)

    assert state.is_critical
    assert "194.2GB" in state.summary()


def test_high_commit_without_ollama_is_left_alone() -> None:
    """
    コミットが高くてもOllamaが原因でなければ再起動しない。

    別プロセスが原因のときにOllamaを落としても解決せず、作業だけ失う。
    """
    state = watchdog.State(commit_used_gb=230.1, commit_limit_gb=230.5, ollama_commit_gb=0.5)

    assert not state.is_critical


def test_large_ollama_with_headroom_is_left_alone() -> None:
    """大きなモデルを載せているだけの状態を落としに行かない。"""
    state = watchdog.State(commit_used_gb=30.0, commit_limit_gb=200.0, ollama_commit_gb=50.0)

    assert not state.is_critical


def test_zero_limit_does_not_divide_by_zero() -> None:
    state = watchdog.State(commit_used_gb=0.0, commit_limit_gb=0.0, ollama_commit_gb=0.0)

    assert state.commit_ratio == 0.0
    assert not state.is_critical


def test_log_is_appended_as_json_lines(tmp_path, monkeypatch) -> None:
    target = tmp_path / "logs" / "watchdog.log"
    monkeypatch.setattr(watchdog, "LOG_PATH", target)

    watchdog.log({"time": "2026-08-14T15:00:00+09:00", "action": "none"})
    watchdog.log({"time": "2026-08-14T15:15:00+09:00", "action": "restarted"})

    entries = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert [item["action"] for item in entries] == ["none", "restarted"]


def test_log_failure_does_not_raise(monkeypatch) -> None:
    # 記録できないだけで見張りを止めてはいけない
    monkeypatch.setattr(watchdog, "LOG_PATH", watchdog.Path("Z:/存在しない/watchdog.log"))

    watchdog.log({"action": "none"})
