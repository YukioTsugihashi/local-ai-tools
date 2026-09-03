"""
Open WebUI へ重ねる操作案内(ツアー)のテスト。

いちばん怖いのは「戻したつもりで元と違う状態が残る」ことなので、
loader.js の書き換え→取り消しが1バイト残らず元へ戻ることを重点的に見る。
実際のコンテナへは触らず、docker の入出力を差し替えて確かめる。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import webui_tour as tour

ROOT = Path(__file__).resolve().parents[1]
EXTRACTED_NODE = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs" / "PFiles64" / "nodejs" / "node.exe"
)


def _node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    return str(EXTRACTED_NODE) if EXTRACTED_NODE.is_file() else None


def _tour(**overrides) -> dict:
    data = {
        "id": "sample",
        "title": "見本",
        "steps": [{"label": "入力欄", "text": "ここに入力します", "anchors": ["#chat-input-container"]}],
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# 台本の検査
# ---------------------------------------------------------------------------


def test_正しい台本は問題なしと判定する() -> None:
    assert tour.validate_tour(_tour(), "見本.json") == []


def test_台本の欠けを日本語の理由で返す() -> None:
    problems = tour.validate_tour({"steps": []}, "壊れ.json")
    assert "壊れ.json: id がありません" in problems
    assert "壊れ.json: title がありません" in problems
    assert "壊れ.json: steps が1件もありません" in problems


def test_手順の中身の欠けも見つける() -> None:
    broken = _tour(steps=[{"text": "", "anchors": ["#a"]}, {"text": "x", "anchors": [""]}])
    problems = tour.validate_tour(broken, "見本.json")
    assert "見本.json: steps[0]: text がありません" in problems
    assert "見本.json: steps[1]: anchors[0] が空です" in problems


def test_台本のidが重複していたら取り込まない(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text(json.dumps(_tour()), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps(_tour(title="別")), encoding="utf-8")
    scripts, problems = tour.load_tours(tmp_path)
    assert list(scripts) == ["sample"]
    assert any("id が重複" in message for message in problems)


def test_壊れたJSONがあっても例外にせず理由を返す(tmp_path: Path) -> None:
    (tmp_path / "broken.json").write_text("{ これはJSONではない", encoding="utf-8")
    scripts, problems = tour.load_tours(tmp_path)
    assert scripts == {}
    assert any("broken.json" in message for message in problems)


def test_台本が1件も無ければ理由を返す(tmp_path: Path) -> None:
    scripts, problems = tour.load_tours(tmp_path)
    assert scripts == {}
    assert any("1件もありません" in message for message in problems)


def test_リポジトリの実物の台本と実行本体が検査を通る() -> None:
    runtime = tour.RUNTIME.read_text(encoding="utf-8")
    scripts, problems = tour.load_tours()
    assert problems == []
    assert "excel-comment" in scripts
    bundle = tour.build_bundle(runtime, scripts)
    # 台本が埋め込まれ、取りに行く通信が起きない形になっていること
    assert "window.__WEBUI_TOUR_SCRIPTS__" in bundle
    assert "Excelにコメントを入れる" in bundle
    assert runtime in bundle


# ---------------------------------------------------------------------------
# index.html の書き換えと取り消し
#
# static/ ではなく index.html へ入れる。open_webui を import する新しい
# プロセスが立つたびに static/ は初期化されるため(2026-08-18に実測)。
# ---------------------------------------------------------------------------

PAGE = "<html><head><title>見本</title></head><body>本文</body></html>"


def _block(text: str = "var tour = 1;") -> str:
    return tour.page_block(text)


@pytest.mark.parametrize(
    "original",
    [
        PAGE,
        "<html><head></head><body></body></html>",
        "<head></head>",
        PAGE + "\n",
    ],
)
def test_入れて取り消すと1バイトも変わらず元へ戻る(original: str) -> None:
    patched = tour.patch_index(original, _block())
    assert patched != original
    assert tour.strip_index(patched) == original


def test_元のindexの中身は消さない() -> None:
    patched = tour.patch_index(PAGE, _block())
    assert "<title>見本</title>" in patched
    assert "<body>本文</body>" in patched


def test_二度入れても増えない() -> None:
    once = tour.patch_index(PAGE, _block())
    twice = tour.patch_index(once, _block())
    assert once == twice
    assert twice.count(tour.MARK_BEGIN) == 1


def test_版が変わったら中身だけ差し替える() -> None:
    old = tour.patch_index(PAGE, _block("var version = 111;"))
    new = tour.patch_index(old, _block("var version = 222;"))
    assert new.count(tour.MARK_BEGIN) == 1
    assert "var version = 111;" not in new
    assert "var version = 222;" in new


def test_終わりの目印が消えていても残骸を残さない() -> None:
    damaged = "<head>" + tour.MARK_BEGIN + "\n壊れた途中\n"
    assert tour.strip_index(damaged) == "<head>"


def test_自分の入れた範囲が無ければ何も触らない() -> None:
    assert tour.strip_index(PAGE) == PAGE


def test_headが無いindexは書き換えず理由を出す() -> None:
    with pytest.raises(tour.TourError, match="</head>"):
        tour.patch_index("<html><body>頭が無い</body></html>", _block())


def test_差し込む中身に画面カスタマイズが同居する() -> None:
    """custom.css と Excelガードも static/ に置くと消えるので一緒に入れる。"""
    block = _block()
    assert block.startswith(tour.MARK_BEGIN)
    assert block.rstrip("\n").endswith(tour.MARK_END)
    if tour.CUSTOM_CSS.is_file():
        assert "<style>" in block
    if tour.UPLOAD_GUARD.is_file():
        assert "__MP_EXCEL_UPLOAD_GUARD__" in block, "二重読み込み対策ごと入っていない"


def test_スクリプトの閉じタグでHTMLを壊さない() -> None:
    """JS本文に </script> があるとそこでタグが閉じ、以降が本文になる。"""
    block = tour.page_block("var s = '</script>';")
    assert "'</script>'" not in block
    assert "<\\/script>" in block


def test_中身が変われば版も変わる() -> None:
    assert tour.bundle_version("a") != tour.bundle_version("b")
    assert tour.bundle_version("a") == tour.bundle_version("a")


# ---------------------------------------------------------------------------
# 確認用HTML
# ---------------------------------------------------------------------------


def test_確認用HTMLは単体で開けて外部を読まない() -> None:
    runtime = tour.RUNTIME.read_text(encoding="utf-8")
    scripts, _ = tour.load_tours()
    html = tour.build_preview(tour.build_bundle(runtime, scripts), "excel-comment")
    assert "window.__WEBUI_TOUR_SCRIPTS__" in html, "台本が埋め込まれていない"
    assert "src=\"http" not in html and "href=\"http" not in html, "外部リソースを読んでいる"
    # 実物と同じ目印が作り物の画面にあること(ここが違うと確認にならない)
    for anchor in ("model-selector-0-button", "input-menu-button", "chat-input-container", "send-message-button"):
        assert f'id="{anchor}"' in html


# ---------------------------------------------------------------------------
# 配備(dockerは差し替えて確かめる)
# ---------------------------------------------------------------------------


class FakeContainer:
    """docker cp の代わり。中身をメモリに持つだけ。"""

    def __init__(self, index: str = PAGE) -> None:
        self.files: dict[str, str] = {tour.INDEX_PATH: index}
        self.commands: list[tuple[str, ...]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(tour, "read_container_file", lambda path, container=tour.CONTAINER: self.files.get(path, ""))
        monkeypatch.setattr(
            tour,
            "write_container_file",
            lambda path, text, container=tour.CONTAINER: self.files.__setitem__(path, text),
        )
        monkeypatch.setattr(tour, "run_docker", self._run)

    def _run(self, *arguments: str, timeout: int = 120) -> str:
        self.commands.append(arguments)
        if arguments[:2] == ("exec",) + (tour.CONTAINER,) or (arguments and arguments[0] == "exec"):
            if "rm" in arguments:
                self.files.pop(arguments[-1], None)
        return ""


def test_applyを付けなければコンテナへ書き込まない(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeContainer()
    fake.install(monkeypatch)
    assert tour.command_deploy(apply=False, container=tour.CONTAINER) == 0
    assert fake.files == {tour.INDEX_PATH: PAGE}, "dry-runなのに書き換わっている"
    assert fake.commands == []


def test_配備してから戻すと元の状態に戻る(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeContainer()
    fake.install(monkeypatch)

    assert tour.command_deploy(apply=True, container=tour.CONTAINER) == 0
    patched = fake.files[tour.INDEX_PATH]
    assert tour.MARK_BEGIN in patched
    assert "<title>見本</title>" in patched, "元のindexの中身が消えた"
    assert "__WEBUI_TOUR_SCRIPTS__" in patched, "台本が入っていない"

    assert tour.command_remove(apply=True, container=tour.CONTAINER) == 0
    assert fake.files[tour.INDEX_PATH] == PAGE, "index.html が元へ戻っていない"


def test_配備を二度行っても差し込みは1つ(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeContainer()
    fake.install(monkeypatch)
    tour.command_deploy(apply=True, container=tour.CONTAINER)
    tour.command_deploy(apply=True, container=tour.CONTAINER)
    assert fake.files[tour.INDEX_PATH].count(tour.MARK_BEGIN) == 1


def test_staticには一切置かない(monkeypatch: pytest.MonkeyPatch) -> None:
    """static/ は open_webui の import で初期化される。そこへ置いたら意味がない。"""
    fake = FakeContainer()
    fake.install(monkeypatch)
    tour.command_deploy(apply=True, container=tour.CONTAINER)
    assert list(fake.files) == [tour.INDEX_PATH], "static/ へ書き込んでいる"


def test_差し込んだindexには台本が入っている(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeContainer()
    fake.install(monkeypatch)
    tour.command_deploy(apply=True, container=tour.CONTAINER)
    page = fake.files[tour.INDEX_PATH]
    assert "excel-comment" in page
    assert "#send-message-button" in page


def test_checkとpreviewが実物で通る(tmp_path: Path) -> None:
    assert tour.command_check() == 0
    out = tmp_path / "preview.html"
    assert tour.command_preview(None, out) == 0
    assert out.is_file() and out.stat().st_size > 5000


def test_無い台本を指定したら理由を出して止まる(tmp_path: Path) -> None:
    with pytest.raises(tour.TourError, match="その台本はありません"):
        tour.command_preview("存在しない", tmp_path / "x.html")


# ---------------------------------------------------------------------------
# 実行本体(JavaScript)の実行時テスト
# ---------------------------------------------------------------------------


def test_ツアー実行本体のnodeテストが通る() -> None:
    node = _node()
    if node is None:
        pytest.skip("nodeが見つかりません(D:\\offline-kit\\installers のmsiから導入できます)")
    tests = sorted((ROOT / "webui_tour").glob("*.test.mjs"))
    assert tests, "実行本体のテストが1件もありません"
    result = subprocess.run(
        [node, "--test", *(str(path) for path in tests)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    assert result.returncode == 0, "tour.js のテストが失敗しました:\n" + "\n".join(
        (result.stdout + result.stderr).splitlines()[-40:]
    )


# ---------------------------------------------------------------------------
# 「画面に何も出ない」ときの切り分け
# ---------------------------------------------------------------------------


def test_差し込む中身はURLが書き換わる前にidを控える() -> None:
    # Open WebUI は起動時にURLを書き換えることがある。本体が動き出す頃には
    # #tour= が消えている場合があり、控えておかないと案内が始まらない
    block = tour.page_block("var tour = 1;")
    assert "__WEBUI_TOUR_WANTED__" in block
    assert "location.hash" in block
    assert block.index("__WEBUI_TOUR_WANTED__") < block.index("var tour = 1;"), (
        "控えるのが本体より後回しになっている"
    )


def _current_bundle() -> tuple[str, str]:
    runtime = tour.RUNTIME.read_text(encoding="utf-8")
    scripts, _ = tour.load_tours()
    bundle = tour.build_bundle(runtime, scripts)
    return bundle, tour.bundle_version(bundle)


def _served_page() -> dict[str, tuple[int, str]]:
    bundle, _ = _current_bundle()
    return {"http://webui/": (200, tour.patch_index(PAGE, tour.page_block(bundle)))}


def test_verifyは配信内容がそろっていれば通る(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = _served_page()
    monkeypatch.setattr(tour, "fetch", lambda url, timeout=20: pages[url])
    assert tour.command_verify("http://webui") == 0


def test_verifyは差し込みが消えていたら落ちる(monkeypatch: pytest.MonkeyPatch) -> None:
    """staticが初期化されても index.html は残る。逆に index が戻ったら気づく必要がある。"""
    pages = {"http://webui/": (200, PAGE)}
    monkeypatch.setattr(tour, "fetch", lambda url, timeout=20: pages[url])
    assert tour.command_verify("http://webui") == 1


def test_verifyは配信中の版が古ければ落ちる(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {"http://webui/": (200, tour.patch_index(PAGE, tour.page_block("古い中身")))}
    monkeypatch.setattr(tour, "fetch", lambda url, timeout=20: pages[url])
    assert tour.command_verify("http://webui") == 1


def test_verifyはOpenWebUIが落ちていたら落ちる(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {"http://webui/": (0, "")}
    monkeypatch.setattr(tour, "fetch", lambda url, timeout=20: pages[url])
    assert tour.command_verify("http://webui") == 1
