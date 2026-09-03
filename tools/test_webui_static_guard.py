"""
画面カスタマイズの見張りのテスト。

Open WebUI へは接続しない。「配信されている画面」を差し替えて判定だけ見る。

いちばん怖いのは **画面は200で返るのにカスタマイズだけ消えている** 状態で、
これを見逃すと誰も気づかないまま失われる(2026-08-18に実際そうなった。
doctor.py の内部API確認がコンテナ内で open_webui を import し、
その副作用で static/ が毎回初期化されていた)。
"""

from __future__ import annotations

import pytest

import webui_static_guard as guard
import webui_tour


def _requirements() -> list[guard.Requirement]:
    return [guard.Requirement("見本", "MARKER")]


def test_目印があれば無事と判定する() -> None:
    assert guard.problems("<html>MARKER</html>", _requirements()) == []


def test_目印が無ければ見つける() -> None:
    found = guard.problems("<html>まっさら</html>", _requirements())
    assert len(found) == 1
    assert "画面に入っていません" in found[0][1]


def test_画面が取れなければ全部を直す対象にする() -> None:
    found = guard.problems(None, _requirements())
    assert len(found) == 1
    assert "取得できません" in found[0][1]


def test_実物の必須項目に版と各カスタマイズが含まれる() -> None:
    labels = [item.label for item in guard.requirements()]
    assert any("差し込み" in label for label in labels)
    assert any("操作案内の本体" in label for label in labels)
    assert any("版" in label for label in labels)
    if webui_tour.CUSTOM_CSS.is_file():
        assert any("custom.css" in label for label in labels)
    if webui_tour.UPLOAD_GUARD.is_file():
        assert any("ガード" in label for label in labels)


def test_実物を差し込んだ画面は必須項目をすべて満たす() -> None:
    """配備するものと、見張りが求めるものが食い違っていないこと。"""
    runtime = webui_tour.RUNTIME.read_text(encoding="utf-8")
    scripts, _ = webui_tour.load_tours()
    bundle = webui_tour.build_bundle(runtime, scripts)
    page = webui_tour.patch_index(
        "<html><head></head><body></body></html>", webui_tour.page_block(bundle)
    )
    assert guard.problems(page) == []


def test_版が古いままなら直す対象にする() -> None:
    """入っていても中身が古い状態を見逃さない。"""
    page = webui_tour.patch_index(
        "<html><head></head><body></body></html>", webui_tour.page_block("古い中身")
    )
    labels = [item.label for item, _ in guard.problems(page)]
    assert any("版" in label for label in labels)


def test_確認だけのときは戻さない(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        guard, "check", lambda base_url=guard.BASE_URL: [(guard.Requirement("見本", "x"), "画面に入っていません")]
    )
    monkeypatch.setattr(guard, "restore", lambda: pytest.fail("確認だけなのに戻している"))
    assert guard.main(["--base-url", "http://例"]) == 1


def test_fixなら戻して結果を出す(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    calls: list[str] = []
    states = [[(guard.Requirement("見本", "x"), "画面に入っていません")], []]
    monkeypatch.setattr(guard, "check", lambda base_url=guard.BASE_URL: states.pop(0))
    monkeypatch.setattr(guard, "restore", lambda: (calls.append("restore"), ["戻しました"])[1])
    assert guard.main(["--fix"]) == 0
    assert calls == ["restore"]
    assert "すべて戻しました" in capsys.readouterr().out


def test_異常が無ければ0で終わる(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard, "check", lambda base_url=guard.BASE_URL: [])
    assert guard.main([]) == 0


def test_通信できなくても落ちない() -> None:
    """見張りが例外で止まると、本処理より先に見張りが壊れる。"""
    assert guard.fetch("http://127.0.0.1:9/seiteki", timeout=2) is None


def test_直し方は1本にまとまっている(monkeypatch: pytest.MonkeyPatch) -> None:
    """以前は Apply-WebUiTweaks.ps1 と2本立てだった。今は index.html へ一括。"""
    seen: list[list[str]] = []
    monkeypatch.setattr(guard, "_run", lambda command, label: (seen.append(command), (True, label))[1])
    guard.restore()
    assert len(seen) == 1
    assert "webui_tour.py" in " ".join(seen[0])
    assert "deploy" in seen[0] and "--apply" in seen[0]
