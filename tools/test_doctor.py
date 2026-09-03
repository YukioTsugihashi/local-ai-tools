from __future__ import annotations

import pytest
from datetime import datetime, timezone
from pathlib import Path

import doctor
from doctor import (
    Finding,
    backup_gap_findings,
    git_backup_findings,
    git_state_findings,
    uncommitted_registration_findings,
    check_configs,
    check_http,
    check_log_directory,
    check_required_files,
    exit_code,
    format_report,
    open_webui_url_from_port_output,
)


def _refuse(monkeypatch, method: str) -> None:
    """
    接続拒否をhttpx側で起こす。実際に閉じたポートへ繋ぎにいかない。

    この業務PCでは、閉じたポートへの接続が拒否として返るまで一律2.0秒かかる。
    ポートにもホスト名にも依存せず、成功する接続は0.01秒未満で返る
    (2026-08-18に実測。Apex Oneの挙動監視が失敗した接続を握っていると見ている)。
    該当テストだけで9.4秒になり、pre-commitが毎回それを払っていた。

    ここで確かめたいのは「接続できないときに異常と対処へ変換されるか」であって
    OSが拒否を返すこと自体ではない。実際に繋ぎにいく確認は
    test_connection_refused_returns_actionable_error を1件だけ残してある。
    """
    import httpx

    def refuse(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, method, refuse)


def test_repo_configs_are_valid() -> None:
    findings = check_configs()

    assert findings
    assert all(finding.level == "OK" for finding in findings)


def test_broken_config_is_reported_with_advice(tmp_path: Path) -> None:
    config_dir = tmp_path / "text-processing-bridge" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "kebatori.yaml").write_text(
        "profiles:\n  default: [壊れた\n", encoding="utf-8"
    )

    findings = check_configs(tmp_path)

    assert findings[0].level == "異常"
    assert "kebatori.yaml" in findings[0].title
    assert findings[0].advice


def test_connection_refused_returns_actionable_error() -> None:
    # ポート9は未使用前提。接続拒否が「異常+対処」へ変換されることを見る
    finding = check_http("試験対象", "http://127.0.0.1:9/health", "起動してください")

    assert finding.level == "異常"
    assert "接続できません" in finding.title
    assert finding.advice == "起動してください"


def test_open_webui_url_uses_published_docker_port() -> None:
    output = "0.0.0.0:3000\n[::]:3000\n"

    assert open_webui_url_from_port_output(output) == "http://localhost:3000"
    assert open_webui_url_from_port_output("") is None


def test_missing_required_files_are_flagged(tmp_path: Path) -> None:
    findings = check_required_files(tmp_path)

    # 空ディレクトリではリポジトリ内の資材が全て「見つかりません」になる
    missing = [f for f in findings if "見つかりません" in f.title]
    assert missing
    assert all(f.level in ("注意", "異常") for f in missing)


def test_log_directory_states(tmp_path: Path) -> None:
    assert check_log_directory(tmp_path)[0].level == "注意"

    log_dir = tmp_path / "text-processing-bridge" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "operations.jsonl").write_text("{}\n", encoding="utf-8")
    assert check_log_directory(tmp_path)[0].level == "OK"


def test_exit_code_and_report_summary() -> None:
    findings = [
        Finding("OK", "何か"),
        Finding("注意", "少し気になる"),
        Finding("異常", "壊れている", advice="直す"),
    ]

    report = format_report(findings)

    assert "結果: 異常 1件 / 注意 1件 / 情報 0件 / OK 1件" in report
    assert "対処: 直す" in report
    assert exit_code(findings) == 1
    assert exit_code([Finding("OK", "全部OK"), Finding("注意", "軽微")]) == 0


def test_deep_check_reports_unreachable_bridge(monkeypatch) -> None:
    from doctor import check_deep

    _refuse(monkeypatch, "post")
    findings = check_deep(base_url="http://127.0.0.1:9")

    assert findings[0].level == "異常"
    assert "接続できません" in findings[0].title
    assert findings[0].advice


def test_render_check_reports_unreachable_bridge(monkeypatch) -> None:
    from doctor import check_render

    _refuse(monkeypatch, "post")
    findings = check_render(base_url="http://127.0.0.1:9")

    assert findings[0].level == "異常"
    assert findings[0].advice


def test_out_path_resolves_relative_to_repo_root() -> None:
    from doctor import ROOT, resolve_out_path

    # 実行場所がtools/等でも、相対--outはリポジトリ直下のoutput/へ集まる
    assert (
        resolve_out_path("output\\doctor_report.txt")
        == ROOT / "output" / "doctor_report.txt"
    )
    absolute = Path("C:/tmp/report.txt")
    assert resolve_out_path(str(absolute)) == absolute


OLLAMA_WINDOW_START = datetime(2026, 8, 14, 10, 40, 0).astimezone()
OLLAMA_LOG_LINES = [
    '[GIN] 2026/08/14 - 10:42:21 | 200 |     1.0909186s |       127.0.0.1 | POST     "/v1/chat/completions"',
    '[GIN] 2026/08/14 - 10:42:25 | 200 |      12.3416ms |       127.0.0.1 | POST     "/api/embed"',
    '[GIN] 2026/08/14 - 10:43:02 | 503 |       514.5µs |       127.0.0.1 | POST     "/api/me"',
    '[GIN] 2026/08/14 - 09:27:40 | 200 |     48.331637s |       127.0.0.1 | POST     "/api/chat"',
]


def test_ollama_traffic_counts_only_the_recent_window() -> None:
    from doctor import summarize_ollama_traffic

    traffic = summarize_ollama_traffic(OLLAMA_LOG_LINES, OLLAMA_WINDOW_START)

    # 09:27 の1件は窓の外なので数えない
    assert traffic.total == 3
    assert traffic.by_path["/api/embed"] == 1
    assert "/api/chat" not in traffic.by_path


def test_offline_auth_503_is_not_treated_as_queue_full() -> None:
    from doctor import summarize_ollama_traffic

    traffic = summarize_ollama_traffic(OLLAMA_LOG_LINES, OLLAMA_WINDOW_START)

    # /api/me の503はollama.comへ出られないだけで、待ち行列とは無関係
    assert traffic.queue_rejected == 0


def test_queue_full_503_is_counted_and_reported_as_abnormal() -> None:
    from doctor import ollama_backlog_finding, summarize_ollama_traffic

    lines = OLLAMA_LOG_LINES + [
        '[GIN] 2026/08/14 - 10:44:10 | 503 |       210.0µs |       127.0.0.1 | POST     "/api/embed"'
    ]

    traffic = summarize_ollama_traffic(lines, OLLAMA_WINDOW_START)
    finding = ollama_backlog_finding(traffic)

    assert traffic.queue_rejected == 1
    assert finding.level == "異常"
    assert "待ち行列満杯" in finding.title
    assert finding.advice


def test_model_wait_lines_are_counted() -> None:
    from doctor import summarize_ollama_traffic

    line = (
        "time=2026-08-14T09:51:20.610+09:00 level=INFO source=llama_server.go:1348 "
        'msg="waiting for llama-server to become available" status="llm server error"'
    )

    traffic = summarize_ollama_traffic(
        [line], datetime.fromisoformat("2026-08-14T09:00:00+09:00")
    )

    assert traffic.server_waits == 1


def test_flood_of_requests_warns_with_busiest_endpoint() -> None:
    from doctor import OllamaTraffic, ollama_backlog_finding

    traffic = OllamaTraffic(total=3000, by_path={"/api/embed": 2900, "/api/chat": 100})

    finding = ollama_backlog_finding(traffic)

    assert finding.level == "注意"
    assert "3000件" in finding.title
    assert "/api/embed 2900件" in finding.detail


def test_repeated_model_waits_warn() -> None:
    from doctor import OllamaTraffic, ollama_backlog_finding

    finding = ollama_backlog_finding(OllamaTraffic(total=12, server_waits=9))

    assert finding.level == "注意"
    assert "応答待ち" in finding.title
    assert "ollama stop" in finding.advice


def test_model_download_in_progress_is_not_reported_as_idle() -> None:
    from doctor import ollama_backlog_finding, summarize_ollama_traffic

    line = (
        "time=2026-08-14T11:08:00.719+09:00 level=INFO source=download.go:181 "
        'msg="downloading 30e51a7cb1cf in 52 1 GB part(s)"'
    )

    traffic = summarize_ollama_traffic(
        [line], datetime.fromisoformat("2026-08-14T11:00:00+09:00")
    )
    finding = ollama_backlog_finding(traffic)

    # ダウンロード中は完了までGIN行が出ない。件数が0でもOKにしてはいけない
    assert traffic.downloads == 1
    assert finding.level == "注意"
    assert "ダウンロード中" in finding.title


def test_timeout_is_distinguished_from_unreachable(monkeypatch) -> None:
    import httpx

    from doctor import check_ollama_connection

    def timeout(*args, **kwargs):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(httpx, "get", timeout)

    findings = check_ollama_connection()

    # 応答が返らないだけ(pull中など)を「未接続」と報告すると、
    # ダウンロードの巻き添えでOllamaを再起動させてしまう
    assert findings[0].level == "異常"
    assert "応答しません" in findings[0].title
    assert "滞留" in findings[0].advice


def test_quiet_ollama_is_ok() -> None:
    from doctor import OllamaTraffic, ollama_backlog_finding

    finding = ollama_backlog_finding(OllamaTraffic(total=4, by_path={"/api/chat": 4}))

    assert finding.level == "OK"
    assert "処理待ちなし" in finding.title


# 2026-08-14の障害時に実際に出ていた行。件数は伸びず、1件が3〜4分かかっていた
STALL_LOG_LINES = [
    '[GIN] 2026/08/14 - 15:19:52 | 400 |         3m32s |       127.0.0.1 | POST     "/api/embed"',
    '[GIN] 2026/08/14 - 15:19:58 | 400 |         3m37s |       127.0.0.1 | POST     "/api/embed"',
    '[GIN] 2026/08/14 - 15:20:24 | 400 |          4m3s |       127.0.0.1 | POST     "/api/embed"',
    '[GIN] 2026/08/14 - 15:26:55 | 500 |          4m1s |       127.0.0.1 | GET      "/api/tags"',
]


def test_gin_duration_is_parsed_in_every_unit() -> None:
    from doctor import parse_gin_duration

    assert parse_gin_duration("3m32s") == 212
    assert parse_gin_duration("  4m3s ") == 243
    assert parse_gin_duration("1.0909186s") == pytest.approx(1.09, abs=0.01)
    assert parse_gin_duration("514.5µs") == pytest.approx(0.0005145, abs=1e-6)


def test_stalled_requests_are_detected_even_when_the_count_is_low() -> None:
    """
    2026-08-14の障害はこれを捕まえられずに素通りした。

    1件3〜4分かかるため件数が伸びず、流量の閾値(1000件)に届かなかった。
    """
    from doctor import ollama_backlog_finding, summarize_ollama_traffic

    traffic = summarize_ollama_traffic(STALL_LOG_LINES, OLLAMA_WINDOW_START)
    finding = ollama_backlog_finding(traffic)

    assert traffic.slow == 4
    assert traffic.slowest_seconds == 243
    assert finding.level == "異常"
    assert "極端に遅く" in finding.title


def test_repeated_failures_are_detected() -> None:
    from doctor import ollama_backlog_finding, summarize_ollama_traffic

    # 速いが失敗し続ける場合(保存先の無い索引の再試行ループなど)
    lines = [
        f'[GIN] 2026/08/14 - 10:4{i % 10}:0{i % 10} | 400 |      12.3ms |       127.0.0.1 | POST     "/api/embed"'
        for i in range(12)
    ]
    traffic = summarize_ollama_traffic(lines, OLLAMA_WINDOW_START)
    finding = ollama_backlog_finding(traffic)

    assert traffic.errors == 12 and traffic.slow == 0
    assert finding.level == "異常"
    assert "失敗し続けています" in finding.title


def test_offline_auth_failures_do_not_count_as_errors() -> None:
    from doctor import summarize_ollama_traffic

    lines = [
        f'[GIN] 2026/08/14 - 10:45:0{i} | 503 |      514.5µs |       127.0.0.1 | POST     "/api/me"'
        for i in range(9)
    ]
    traffic = summarize_ollama_traffic(lines, OLLAMA_WINDOW_START)

    # /api/me はオフラインでは必ず失敗する。数えると常時異常になる
    assert traffic.errors == 0 and traffic.judged == 0 and traffic.queue_rejected == 0


def test_commit_charge_levels() -> None:
    from doctor import commit_charge_finding

    critical = commit_charge_finding(230.1, 230.5, "ollama.exe 194.2GB")
    assert critical.level == "異常"
    assert "194.2GB" in critical.detail
    assert critical.advice

    assert commit_charge_finding(160.0, 200.0).level == "注意"
    assert commit_charge_finding(30.0, 200.0).level == "OK"


def test_ollama_connection_reports_unreachable_with_advice(monkeypatch) -> None:
    from doctor import check_ollama_connection

    _refuse(monkeypatch, "get")
    findings = check_ollama_connection(base_url="http://127.0.0.1:9")

    assert findings[0].level == "異常"
    assert "接続できません" in findings[0].title
    assert findings[0].advice


def test_backlog_check_is_informational_without_logs(tmp_path: Path) -> None:
    from doctor import check_ollama_backlog

    findings = check_ollama_backlog(log_dir=tmp_path)

    # ログが無いだけで異常扱いにはしない(判定できないことを伝える)
    assert findings[0].level == "情報"


def test_backlog_check_reads_log_tail(tmp_path: Path) -> None:
    from doctor import check_ollama_backlog

    (tmp_path / "server.log").write_text("\n".join(OLLAMA_LOG_LINES), encoding="utf-8")

    findings = check_ollama_backlog(
        log_dir=tmp_path, now=datetime(2026, 8, 14, 10, 45, 0).astimezone()
    )

    assert findings[0].level == "OK"
    assert "3件" in findings[0].title


def test_reexec_only_when_other_python_and_venv_exists(tmp_path: Path) -> None:
    from doctor import should_reexec

    venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    other_python = tmp_path / "system" / "python.exe"

    # venvが無ければ再実行しない(check_pythonが異常として報告する)
    assert not should_reexec(other_python, venv_python)

    venv_python.parent.mkdir(parents=True)
    venv_python.write_bytes(b"")
    other_python.parent.mkdir(parents=True)
    other_python.write_bytes(b"")

    # 別のPythonから起動された場合だけ再実行する
    assert should_reexec(other_python, venv_python)
    assert not should_reexec(venv_python, venv_python)


def test_登録済みなのに未コミットのものを知らせる() -> None:
    """本番で動いているコードがGitに無い状態は、全戻しで復元できなくなる。"""
    findings = uncommitted_registration_findings(
        {"hospital_help": "tools/open_webui_help_pipe.py"},
        {"tools/open_webui_help_pipe.py", "AGENTS.md"},
    )
    assert len(findings) == 1
    assert findings[0].level == "注意"
    assert "hospital_help" in findings[0].title
    assert "git checkout" in findings[0].advice


def test_コミット済みなら何も言わない() -> None:
    assert uncommitted_registration_findings(
        {"hospital_help": "tools/open_webui_help_pipe.py"}, {"AGENTS.md"}
    ) == []


def test_バックアップに無いモデルを知らせる() -> None:
    """バックアップは取った日で止まっても誰も気づかない(2026-08-18に実発生)。"""
    findings = backup_gap_findings({"qwen3.8", "gemma4"}, {"gemma4"}, [])
    assert len(findings) == 1
    assert findings[0].level == "注意"
    assert "qwen3.8" in findings[0].title


def test_削除済みがバックアップに残っていても騒がない() -> None:
    """消したモデルの温存は安全側。オフライン後は取り直せないため。"""
    findings = backup_gap_findings({"gemma4"}, {"gemma4", "qwen3-coder"}, [])
    assert [f.level for f in findings] == ["OK"]


def test_設定のバックアップが違えば知らせる() -> None:
    findings = backup_gap_findings({"gemma4"}, {"gemma4"}, ["Kiloのグローバル設定"])
    assert findings[0].level == "注意"
    assert "Kiloのグローバル設定" in findings[0].title


def test_mainにいるときは現在地だけ出す() -> None:
    findings = git_state_findings("abc1234", "main", 0)
    assert [f.level for f in findings] == ["OK"]
    assert "main" in findings[0].detail


def test_作業ブランチにいることを知らせる() -> None:
    """戻し忘れると次の依頼が意図しないブランチの上へ積まれるため。"""
    findings = git_state_findings("abc1234", "help-pipe-timeout", 3)
    assert [f.level for f in findings] == ["OK", "情報"]
    assert "help-pipe-timeout" in findings[1].title
    assert "merge --no-ff help-pipe-timeout" in findings[1].advice


def test_切り離しHEADは注意にする() -> None:
    """このままコミットしてもどのブランチにも残らないため、情報では弱い。"""
    findings = git_state_findings("abc1234", "", 0)
    assert [f.level for f in findings] == ["OK", "注意"]
    assert "git switch main" in findings[1].advice


def test_バックアップ先が未設定なら作り方を出す() -> None:
    findings = git_backup_findings("", 0)
    assert findings[0].level == "注意"
    assert "git remote add local" in findings[0].advice


def test_バックアップ先と同じならOK() -> None:
    findings = git_backup_findings("D:/offline-kit/git/minutes-pipeline.git", 0)
    assert [f.level for f in findings] == ["OK"]


def test_コミット直後の未送信では騒がない() -> None:
    """コミットのたびに注意が出ると読み飛ばされるようになるため。"""
    findings = git_backup_findings("D:/offline-kit/git/minutes-pipeline.git", 1)
    assert [f.level for f in findings] == ["情報"]
    assert "1件" in findings[0].title


def test_未送信が溜まったら注意にする() -> None:
    findings = git_backup_findings("D:/offline-kit/git/minutes-pipeline.git", 5)
    assert [f.level for f in findings] == ["注意"]
    assert "git push local" in findings[0].advice


# ---------------------------------------------------------------------------
# 開発ツールとオフライン資材
# ---------------------------------------------------------------------------


def test_開発ツールがPATHに無ければ注意を出す(monkeypatch) -> None:
    """入っていても PATH に無いと Kilo から使えない。実際にそれで一度つまずいた。"""
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    findings = doctor.check_language_tools()
    assert findings[0].level == "注意"
    assert "node" in findings[0].detail and "java" in findings[0].detail


def test_開発ツールが全部使えればOK(monkeypatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"C:/dummy/{name}.exe")
    findings = doctor.check_language_tools()
    assert findings[0].level == "OK"


def test_オフライン資材が欠けていれば注意を出す(monkeypatch, tmp_path) -> None:
    """切断後は取り直せないので、欠けていることに気づける必要がある。"""
    monkeypatch.setattr(
        doctor, "OFFLINE_MATERIALS", [(tmp_path / "無い", "見本の資材")]
    )
    findings = doctor.check_offline_materials()
    assert findings[0].level == "注意"
    assert "見本の資材" in findings[0].detail
    assert "切断後は入手できません" in findings[0].advice


def test_オフライン資材が揃っていればOK(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(doctor, "OFFLINE_MATERIALS", [(tmp_path, "見本の資材")])
    assert doctor.check_offline_materials()[0].level == "OK"


def test_画面カスタマイズが消えていれば注意を出す(monkeypatch) -> None:
    """0バイトで200が返る状態を見逃すと、誰も気づかないまま失われる。"""
    import webui_static_guard

    item = webui_static_guard.Requirement("操作案内の本体", "__WEBUI_TOUR_SCRIPTS__")
    monkeypatch.setattr(
        webui_static_guard, "check", lambda base_url=None: [(item, "画面に入っていません")]
    )
    findings = doctor.check_webui_customizations()
    assert findings[0].level == "注意"
    assert item.label in findings[0].detail
    assert "--fix" in findings[0].advice


def test_画面カスタマイズが揃っていればOK(monkeypatch) -> None:
    import webui_static_guard

    monkeypatch.setattr(webui_static_guard, "check", lambda base_url=None: [])
    assert doctor.check_webui_customizations()[0].level == "OK"


def test_見張りが壊れていても診断は止まらない(monkeypatch) -> None:
    import webui_static_guard

    def explode(base_url=None):
        raise RuntimeError("見張りの不具合")

    monkeypatch.setattr(webui_static_guard, "check", explode)
    findings = doctor.check_webui_customizations()
    assert findings[0].level == "情報"
