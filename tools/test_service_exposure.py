"""ローカルサービスの露出(台帳 D-16)のテスト。

**ハブは端末内からだけ届けばよい。** 0.0.0.0 で待つと院内LANの他端末から
呼べてしまい、追加ツールが増えるほど「LANから任意のツールを実行できる受付」になる。

台帳には「127.0.0.1 にすると Open WebUI(コンテナ)から繋がらないので採れない」と
書いていたが、2026-09-02 の実測で**誤り**と分かった(5-12 を訂正)。
Docker Desktop は host.docker.internal 宛てを折り返しへ中継する。
"""

from __future__ import annotations

import socket
import threading

import doctor
import start_local_services as services


def listen_on(host: str) -> tuple[socket.socket, int]:
    """指定したアドレスで待ち受け、(ソケット, ポート)を返す。"""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, 0))
    server.listen(4)
    threading.Thread(target=_accept_forever, args=(server,), daemon=True).start()
    return server, server.getsockname()[1]


def _accept_forever(server: socket.socket) -> None:
    while True:
        try:
            connection, _ = server.accept()
            connection.close()
        except OSError:
            return


# ---------------------------------------------------------------------------
# 既定は端末内のみ
# ---------------------------------------------------------------------------
def test_既定は折り返しで待つ() -> None:
    """**既定を広い側にしない。** 広げるのは明示したときだけ。"""
    assert services.DEFAULT_HOST == "127.0.0.1"


def test_広げるには明示が要る() -> None:
    import inspect

    signature = inspect.signature(services._serve)
    assert signature.parameters["host"].default == services.DEFAULT_HOST


# ---------------------------------------------------------------------------
# 検査が実際の露出を捉えるか
# ---------------------------------------------------------------------------
def test_LANから届くなら注意を出す() -> None:
    addresses = doctor.own_lan_addresses()
    if not addresses:
        return  # LAN側アドレスが無い環境では試験できない
    server, port = listen_on("0.0.0.0")
    try:
        findings = doctor.check_service_exposure(ports=(port,))
    finally:
        server.close()
    assert [f.level for f in findings] == ["注意"]
    assert "LANから届きます" in findings[0].title
    assert str(port) in findings[0].detail


def test_折り返しだけならOKと言う() -> None:
    server, port = listen_on("127.0.0.1")
    try:
        findings = doctor.check_service_exposure(ports=(port,))
    finally:
        server.close()
    assert [f.level for f in findings] == ["OK"]


def test_誰も待っていなければOK() -> None:
    # 空きポートを1つ確保してすぐ閉じる(誰も待っていない状態を作る)
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    assert [f.level for f in doctor.check_service_exposure(ports=(port,))] == ["OK"]


def test_自分のアドレスに折り返しを混ぜない() -> None:
    """127.x や 169.254.x を混ぜると、繋がって当たり前なので判定にならない。"""
    for address in doctor.own_lan_addresses():
        assert not address.startswith(("127.", "169.254."))


def test_検査は読み取りだけで何も変えない() -> None:
    """繋いでみるだけ。**要求は送らない**(業務の処理を動かさない)。"""
    import inspect

    source = inspect.getsource(doctor.check_service_exposure)
    assert "connect_ex" in source
    assert "sendall" not in source and "request" not in source


# ---------------------------------------------------------------------------
# 承認の決まりを自己診断で見る(Codexレビュー2026-09-02)
# ---------------------------------------------------------------------------
def store_at(tmp_path):
    import toolpack_store as ts

    instance = ts.ToolpackStore(tmp_path / "additional-tools")
    instance.ensure_layout()
    return instance


def test_設定が無ければ既定としてOK(tmp_path, monkeypatch) -> None:
    store = store_at(tmp_path)
    monkeypatch.setattr(doctor, "_toolpack_store", lambda root: store)
    findings = doctor.check_approval_policy(tmp_path)
    assert [f.level for f in findings] == ["OK"]
    assert "承認2人 / 取消2人" in findings[0].title


def test_壊れた設定は自己診断で分かる(tmp_path, monkeypatch) -> None:
    """承認ボタンを押すまで気づけないのでは遅い(開発担当者がいない運用)。"""
    store = store_at(tmp_path)
    store.policy_path.write_text('{"approvers": "壊れた値"}', encoding="utf-8")
    monkeypatch.setattr(doctor, "_toolpack_store", lambda root: store)
    findings = doctor.check_approval_policy(tmp_path)
    assert [f.level for f in findings] == ["注意"]
    assert "承認機能を利用できません" in findings[0].title
    assert "approvers" in findings[0].detail
    assert "消してください" in findings[0].advice


def test_人数の設定を読めたらそのまま出す(tmp_path, monkeypatch) -> None:
    import json

    store = store_at(tmp_path)
    store.policy_path.write_text(
        json.dumps({"approvals_required": 1, "revocations_required": 3}),
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor, "_toolpack_store", lambda root: store)
    findings = doctor.check_approval_policy(tmp_path)
    assert "承認1人 / 取消3人" in findings[0].title


def test_確定していない票を知らせる(tmp_path, monkeypatch) -> None:
    """人数を下げたあと、届いているのに未確定のまま残る状態を拾う。"""
    import json

    store = store_at(tmp_path)
    store.register("duty_summary", "1.0.0", package_hash="h")
    store.add_vote("duty_summary", "1.0.0", "甲野", revoke=False)
    store.policy_path.write_text(
        json.dumps({"approvals_required": 1}), encoding="utf-8"
    )
    monkeypatch.setattr(doctor, "_toolpack_store", lambda root: store)
    findings = doctor.check_approval_policy(tmp_path)
    assert [f.level for f in findings] == ["注意"]
    assert "確定していません" in findings[0].title
    assert "duty_summary 1.0.0" in findings[0].detail


def test_基盤を使っていなければ何も言わない(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_toolpack_store", lambda root: None)
    assert doctor.check_approval_policy(tmp_path) == []


def test_自己診断に承認の検査が入っている() -> None:
    import inspect

    assert "check_approval_policy" in inspect.getsource(doctor.collect_findings)


def test_承認状態を確認できなければ正常扱いしない(tmp_path, monkeypatch) -> None:
    """握り潰すと、台帳が壊れていても「承認の決まりはOK」と出てしまう
    (Codex指摘・2026-09-02)。**未確認を正常扱いしない**という全体方針と同じ。"""
    store = store_at(tmp_path)

    def broken(*args, **kwargs):
        raise ValueError("registry.json が壊れています")

    monkeypatch.setattr(store, "load_registry", broken)
    monkeypatch.setattr(doctor, "_toolpack_store", lambda root: store)
    findings = doctor.check_approval_policy(tmp_path)
    assert [f.level for f in findings] == ["注意"]
    assert "確認できません" in findings[0].title
    assert "registry.json が壊れています" in findings[0].detail


def test_版ごとの確認で落ちても正常扱いしない(tmp_path, monkeypatch) -> None:
    store = store_at(tmp_path)
    store.register("duty_summary", "1.0.0", package_hash="h")

    def broken(*args, **kwargs):
        raise KeyError("versions")

    monkeypatch.setattr(store, "approval_state", broken)
    monkeypatch.setattr(doctor, "_toolpack_store", lambda root: store)
    findings = doctor.check_approval_policy(tmp_path)
    assert [f.level for f in findings] == ["注意"]
    assert "確認できません" in findings[0].title
