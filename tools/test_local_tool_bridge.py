import base64
from pathlib import Path

import pytest

from local_tool_bridge import app as bridge
from local_tool_bridge import hub, page
from local_tool_bridge.connectors import excel_comment as connector


PACKAGE = Path(__file__).resolve().parents[1] / "local_tool_bridge"
# ハブは「受け取って渡すだけ」の役である。処理本体を持たせると
# Kilo側の実装と分裂し、経路ごとに結果が変わる(過去に実際そうなった)
FORBIDDEN_IMPORTS = ("openpyxl", "pandas", "docx", "pptx", "urllib.request", "httpx", "requests")
# スクリプト実行と生死確認は hub.py だけに許す。接続役へ持たせると
# そこから業務処理を直接呼べてしまい、tools/ 側と実装が分裂する
HUB_ONLY_IMPORTS = ("subprocess", "socket")


def test_hub_does_not_hold_business_logic() -> None:
    for path in PACKAGE.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for forbidden in FORBIDDEN_IMPORTS:
            assert f"import {forbidden}" not in source, f"{path.name} が {forbidden} を使っている"
        if path.name != "hub.py":
            for restricted in HUB_ONLY_IMPORTS:
                assert f"import {restricted}" not in source, (
                    f"{path.name} が直接 {restricted} を呼んでいる"
                )


def test_every_connector_declares_a_valid_spec() -> None:
    specs = hub.load_specs()

    assert "excel_comment" in specs
    for name, spec in specs.items():
        assert spec.name == name
        assert spec.summary
        assert all(suffix.startswith(".") for suffix in spec.accepts)
        assert callable(spec.run)


def test_tools_endpoint_lists_connected_tools() -> None:
    listed = {item.name: item for item in bridge.tools()}

    assert "excel_comment" in listed
    assert ".xlsm" in listed["excel_comment"].accepts


def test_safe_filename_rejects_unsupported_extension() -> None:
    assert bridge.safe_filename("【ダミー】集計.xlsm", (".xlsx", ".xlsm")) == "ダミー_集計.xlsm"
    with pytest.raises(ValueError, match="対応形式"):
        bridge.safe_filename("memo.txt", (".xlsx", ".xlsm"))


def test_safe_filename_keeps_long_vowel_marks() -> None:
    """長音符(ー)を落とすと「データ」が「デ_タ」になり、利用者が自分の
    ファイルを見分けられなくなる(2026-08-18に実際に発生)。

    ー(U+30FC)は カタカナ範囲 ァ-ヶ(U+30A1-U+30F6)の外にあるため、
    範囲指定だけでは落ちる。
    """
    keep = "テスト用データ_左表ダミー_数値_修正版.xlsx"
    assert bridge.safe_filename(keep, (".xlsx",)) == keep
    assert bridge.safe_filename("ヶ月々の推移ｰ集計.xlsx", (".xlsx",)) == "ヶ月々の推移ｰ集計.xlsx"
    # 置ける名前にするための置換自体は今までどおり効くこと
    assert bridge.safe_filename("表 集計*.xlsx", (".xlsx",)) == "表_集計.xlsx"


def test_unknown_tool_is_reported_with_the_available_names() -> None:
    with pytest.raises(ValueError, match="繋がっていないツールです"):
        bridge.run_tool(
            bridge.RunRequest(
                tool="存在しない",
                files=[bridge.InputFile(filename="a.xlsx", content_b64=base64.b64encode(b"x").decode())],
            )
        )


def _fake_script(monkeypatch, tmp_path, *, skipped: list[str]) -> Path:
    """接続役が呼ぶスクリプトを差し替え、成果物だけ作る。"""
    produced = tmp_path / "sample_updated.xlsx"
    produced.write_bytes(b"processed")

    def fake_run(context, script, args):
        assert script == "tools/excel_comment_build.py"
        import json as _json

        return _json.dumps(
            {"workbook": str(produced), "report": None, "skipped": skipped},
            ensure_ascii=False,
        )

    monkeypatch.setattr(connector, "run_repo_script", fake_run)
    return produced


def test_run_returns_the_processed_file_without_touching_the_upload(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bridge, "WORK_ROOT", tmp_path / "work")
    _fake_script(monkeypatch, tmp_path, skipped=[])

    response = bridge.run_tool(
        bridge.RunRequest(
            tool="excel_comment",
            files=[bridge.InputFile(filename="sample.xlsx", content_b64=base64.b64encode(b"original").decode())],
            instruction="全シート",
        )
    )

    assert base64.b64decode(response.files[0].content_b64) == b"processed"
    assert response.files[0].filename == "sample_updated.xlsx"
    # 作業ディレクトリは実行後に残さない
    assert not any((tmp_path / "work").glob("*"))


def test_skipped_sheets_reach_the_caller(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bridge, "WORK_ROOT", tmp_path / "work")
    _fake_script(monkeypatch, tmp_path, skipped=["糖内（案）: コメント記入例が見つかりません"])

    response = bridge.run_tool(
        bridge.RunRequest(
            tool="excel_comment",
            files=[bridge.InputFile(filename="sample.xlsx", content_b64=base64.b64encode(b"x").decode())],
        )
    )

    assert response.skipped == ["糖内（案）: コメント記入例が見つかりません"]
    assert "1件" in response.message


def test_legacy_excel_endpoint_still_answers(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bridge, "WORK_ROOT", tmp_path / "work")
    _fake_script(monkeypatch, tmp_path, skipped=["糖内（案）: 理由"])

    # 登録済みPipeは旧形式で呼んでくる。壊すと現場が止まる
    response = bridge.excel_comment(
        bridge.ExcelCommentRequest(
            filename="sample.xlsx",
            workbook_b64=base64.b64encode(b"original").decode(),
        )
    )

    assert base64.b64decode(response.workbook_b64) == b"processed"
    assert response.filename == "sample_updated.xlsx"
    assert "糖内（案）" in response.message


def test_broken_connector_is_reported_instead_of_killing_the_hub(monkeypatch) -> None:
    import importlib

    real_import = importlib.import_module

    def fail_for_excel_comment(name, *args, **kwargs):
        if name.endswith("connectors.excel_comment"):
            raise ImportError("SPEC を書き間違えた想定")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(hub.importlib, "import_module", fail_for_excel_comment)
    specs, errors = hub.discover()

    # 壊れた1枚で受付ごと落ちると、原因を追う手段がなくなる
    assert "excel_comment" not in specs
    assert any("excel_comment.py" in reason for reason in errors)


def test_dashboard_shows_connected_tools_without_external_resources() -> None:
    html = bridge.dashboard().body.decode("utf-8")

    assert "excel_comment" in html
    assert "Excelの表を読み" in html
    assert "tools/excel_comment_build.py" in html
    # オフラインPCで開くため、外部から読み込む要素があってはいけない
    assert "http://" not in html.replace("http-equiv", "")
    assert "https://" not in html
    assert "<script" not in html


def test_dashboard_surfaces_load_errors() -> None:
    html = page.render({}, ["壊れた.py: SPEC がありません"])

    assert "読み込めなかったファイル" in html
    assert "壊れた.py: SPEC がありません" in html
    assert "繋がっているツールがありません" in html


def test_dashboard_shows_services_that_do_not_go_through_the_hub() -> None:
    html = page.render({}, [], [("文章処理ブリッジ", 8008, True), ("Ollama", 11434, False)])

    # 文章処理はハブ経由ではないが、生死は同じ画面で見えないと探しに行けない
    assert "周辺サービス" in html
    assert "文章処理ブリッジ" in html and "port 8008" in html
    assert "稼働中" in html and "停止" in html


def test_port_probe_reports_closed_port() -> None:
    # ポート9は未使用前提
    assert hub.port_is_open("127.0.0.1", 9) is False


def test_status_endpoint_returns_tools_and_errors() -> None:
    body = bridge.status()

    assert any(item.name == "excel_comment" for item in body.tools)
    assert body.errors == []


def test_子プロセスの警告を成果物の出力へ混ぜない(monkeypatch, tmp_path) -> None:
    """
    接続役は最終行をJSONとして読む。stderrを連結すると最終行が警告になり壊れる。

    グラフを含むブックでopenpyxlがUserWarningを出し、WebUI経路の
    コメント生成が全滅した(2026-08-17)。警告は本処理の失敗ではない。
    """
    import subprocess as sp

    monkeypatch.setattr(
        sp,
        "run",
        lambda *a, **k: sp.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"workbook":"output/a_updated.xlsx"}\n',
            stderr="UserWarning: Unable to read chart rId2 ... A data source must be provided\n",
        ),
    )
    context = hub.RunContext(
        root=tmp_path,
        python=Path("python.exe"),
        work_dir=tmp_path,
        inputs=[],
        instruction="",
    )

    output = hub.run_repo_script(context, "tools/excel_comment_build.py", [])

    assert output.splitlines()[-1] == '{"workbook":"output/a_updated.xlsx"}'


def test_saved_paths_picks_up_tool_output() -> None:
    output = "保存しました(元ファイルは無変更): output\\a_updated.xlsm\n保存しました: output\\b.xlsx"

    found = hub.saved_paths(output, Path("D:/repo"))

    assert [path.name for path in found] == ["a_updated.xlsm", "b.xlsx"]


def test_補足は処理できなかったものと混ぜない() -> None:
    """成功したうえでの補足を skipped へ混ぜると、Open WebUI側で
    「処理できなかったもの」として表示され、失敗したように見える
    (2026-08-18に利用者から指摘。書式テンプレートの適用通知が該当した)。
    """
    result = hub.RunResult(
        message="全対象シートへコメントを生成・挿入しました",
        notes=["書式テンプレートを適用します: excel_comment_gui_template.xlsx(…)"],
    )
    assert result.skipped == [], "補足が処理できなかったものへ混ざっている"
    assert result.notes, "補足が失われている"


def test_接続役は補足と失敗を別々に返す() -> None:
    """excel_comment の宣言が notes と skipped を取り違えていないこと。"""
    import inspect

    from local_tool_bridge.connectors import excel_comment

    source = inspect.getsource(excel_comment.run)
    assert "notes=notes" in source, "補足を返していない"
    assert "skipped += " not in source, "補足を skipped へ足し込んでいる"
