from pathlib import Path

import pytest

import excel_comment_build as build


PLUGIN = Path(__file__).resolve().parents[1] / ".kilo" / "plugin" / "excel-tools.ts"


def _fake_python(updated: Path, *, skipped_json: str = "", record: dict | None = None):
    """excel_comment_context.py と office_excel.py の呼び出しを差し替える。"""

    def run(python: Path, root: Path, script: str, args: list[str]) -> str:
        if script.endswith("excel_comment_context.py"):
            contexts = (
                '{"contexts":[{"sheet":"内科（案）","target":"B2","template":"",'
                '"facts":[],"fact_records":[]}]'
            )
            return contexts + (f",{skipped_json}" if skipped_json else "") + "}"
        if script.endswith("office_excel.py"):
            if record is not None:
                record["manifest"] = Path(args[-1]).read_text(encoding="utf-8")
            updated.parent.mkdir(exist_ok=True)
            updated.write_bytes(b"processed")
            return f"保存しました(元ファイルは無変更): {updated}\n再読込検証に成功しました: 1セル"
        raise AssertionError(script)

    return run


def _completed(stdout: str, stderr: str = "", returncode: int = 0):
    import subprocess

    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _skip_polish(monkeypatch) -> None:
    """
    配管を見るテストで、文体整形だけ本物のOllamaを呼ばせない。

    process_excel_comment の use_llm は既定でTrueのため、保存先・skipped・
    JSONの形・引数の受け渡しだけを確かめるテストでもコメント1本ごとに実際の
    推論が走る。2026-08-18に、これだけで pre-commit が179秒待たされていた
    (tools全体198秒のうち90%)。整形後の文面はここでは一切検証していないので、
    待った分は何も返ってこない。

    use_llm=False ではなく差し替えにするのは、分岐そのものを飛ばさず
    同じ経路を通したまま通信だけ消すため。整形の中身は urlopen を差し替える
    test_文体整形では思考を抑制する 等が別に見ている。
    """
    monkeypatch.setattr(
        build,
        "refine_comment_with_ollama",
        lambda context, verified, *args, **kwargs: verified,
    )


def test_子プロセスの警告をJSONへ混ぜない(monkeypatch, tmp_path) -> None:
    """
    stderrをstdoutへ連結すると、呼び出し元のjson.loadsが「Extra data」で落ちる。

    グラフを含むブックでopenpyxlが出すUserWarning(chartのdata source)で
    実際にコメント生成が全滅した(2026-08-17)。警告は本処理の失敗ではない。
    """
    import json
    import subprocess as sp

    warning = (
        "openpyxl\\reader\\drawings.py:46: UserWarning: Unable to read chart rId2 "
        "from xl/drawings/drawing1.xml A data source must be provided\n"
    )
    monkeypatch.setattr(
        sp, "run", lambda *a, **k: _completed('{"contexts":[]}\n', stderr=warning)
    )

    raw = build._run_python(Path("python.exe"), tmp_path, "tools/excel_comment_context.py", [])

    assert json.loads(raw) == {"contexts": []}


def test_子プロセスが失敗したときはstderrを理由に含める(monkeypatch, tmp_path) -> None:
    # 失敗の原因はstderr側にしか出ないことが多いので、こちらでは捨てない
    import subprocess as sp

    monkeypatch.setattr(
        sp,
        "run",
        lambda *a, **k: _completed("", stderr="FileNotFoundError: 対象がありません", returncode=1),
    )

    with pytest.raises(RuntimeError, match="FileNotFoundError"):
        build._run_python(Path("python.exe"), tmp_path, "tools/excel_comment_context.py", [])


def test_result_carries_workbook_and_generated_comments(tmp_path, monkeypatch) -> None:
    source = tmp_path / "work" / "sample.xlsx"
    source.parent.mkdir()
    source.write_bytes(b"original")
    updated = tmp_path / "output" / "sample_updated.xlsx"

    monkeypatch.setattr(build, "_run_python", _fake_python(updated))
    _skip_polish(monkeypatch)
    result = build.process_excel_comment(tmp_path, Path("python.exe"), source)

    assert result.workbook == updated
    assert result.report is None
    assert result.skipped == []
    assert [item["sheet"] for item in result.comments] == ["内科（案）"]
    # 呼び出し元(ハブ・Kiloプラグイン)はこのJSONだけを見る
    assert set(result.as_json()) == {"workbook", "report", "skipped", "notes", "comments"}


def test_one_unextractable_sheet_does_not_stop_the_rest(tmp_path, monkeypatch) -> None:
    source = tmp_path / "work" / "sample.xlsx"
    source.parent.mkdir()
    source.write_bytes(b"original")
    updated = tmp_path / "output" / "sample_updated.xlsx"
    record: dict = {}

    monkeypatch.setattr(
        build,
        "_run_python",
        _fake_python(
            updated,
            skipped_json='"skipped":[{"sheet":"糖内（案）","reason":"コメント記入例が見つかりません"}]',
            record=record,
        ),
    )
    _skip_polish(monkeypatch)
    result = build.process_excel_comment(tmp_path, Path("python.exe"), source)

    # 抽出できなかった1枚のために、書ける1枚まで消してはいけない
    assert result.workbook == updated
    assert result.skipped == ["糖内（案）: コメント記入例が見つかりません"]
    assert "内科（案）" in str(record["manifest"])


def test_preview_can_limit_processing_to_one_sheet_without_llm(
    tmp_path, monkeypatch
) -> None:
    source = tmp_path / "work" / "sample.xlsx"
    source.parent.mkdir()
    source.write_bytes(b"original")
    updated = tmp_path / "output" / "sample_updated.xlsx"
    calls: list[tuple[str, list[str]]] = []

    def run(python: Path, root: Path, script: str, args: list[str]) -> str:
        calls.append((script, args))
        if script.endswith("excel_comment_context.py"):
            return (
                '{"sheet":"消内（案）","target":"AG5","template":"記入例",'
                '"facts":[],"fact_records":[]}'
            )
        if script.endswith("office_excel.py"):
            updated.parent.mkdir(exist_ok=True)
            updated.write_bytes(b"processed")
            return f"保存しました(元ファイルは無変更): {updated}"
        raise AssertionError(script)

    monkeypatch.setattr(build, "_run_python", run)
    monkeypatch.setattr(
        build,
        "refine_comment_with_ollama",
        lambda *args, **kwargs: pytest.fail("--no-llm相当ではLLMを呼ばない"),
    )

    result = build.process_excel_comment(
        tmp_path,
        Path("python.exe"),
        source,
        sheet_name="消内（案）",
        use_llm=False,
    )

    context_call = next(args for script, args in calls if script.endswith("excel_comment_context.py"))
    assert context_call == [str(source), "--sheet", "消内（案）"]
    assert result.comments[0]["sheet"] == "消内（案）"


def test_polish_model_is_one_doctor_knows_about() -> None:
    """
    実在しないモデル名を書くと、整形が無言で無効化される。

    qwen3-coder削除後に実際そうなっていた(例外を握って下書きへ落ちるため
    エラーも出なかった)。doctorが在庫を見ているモデルに限定して結びつける。
    """
    import doctor

    known = set(doctor.REQUIRED_MODELS) | set(doctor.MAINTENANCE_MODELS)
    assert build.COMMENT_POLISH_MODEL in known, (
        f"{build.COMMENT_POLISH_MODEL} は doctor の監視対象外です。"
        "モデルを変えるなら doctor.py の一覧も揃えてください"
    )


def test_文体整形では思考を抑制する(monkeypatch) -> None:
    """
    gemma4はthinkingモデル。抑制しないと残りcontextを全部思考に使い切り、
    本文0文字・done_reason=lengthで返る(2026-08-17に実測)。
    下書きを言い換えるだけのこの用途に思考は要らない。
    """
    import json as _json

    sent: dict = {}

    class _Response:
        def read(self):
            return b'{"message":{"content":""}}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def capture(request, *args, **kwargs):
        sent.update(_json.loads(request.data.decode("utf-8")))
        return _Response()

    monkeypatch.setattr(build.urllib.request, "urlopen", capture)

    build.refine_comment_with_ollama({"sheet": "内科（案）", "facts": []}, "下書き本文", "", "", [])

    assert sent["think"] is False


def test_skipped_polish_is_reported_instead_of_silently_ignored(monkeypatch) -> None:
    def explode(*args, **kwargs):
        raise OSError("model not found")

    monkeypatch.setattr(build.urllib.request, "urlopen", explode)
    notes: list[str] = []

    result = build.refine_comment_with_ollama(
        {"sheet": "内科（案）", "facts": []}, "下書き本文", "", "", notes
    )

    # 処理は止めない。ただし理由は必ず残す
    assert result == "下書き本文"
    assert notes and "内科（案）" in notes[0] and "文体整形を省略" in notes[0]


def test_notation_differences_are_not_treated_as_value_changes() -> None:
    """
    表記の揺れで整形結果を捨てていた(2026-08-14に判明)。

    下書きは「4％増」、整形結果は「+4.0％」と書く。値は同じなのに
    文字列比較では別物になり、整形結果が毎回捨てられていた。
    """
    draft = "収入は4％増、材料費は5％増。稼働率は94.10％である。"
    polished = "収入は+4.0％、材料費は+5.0％。稼働率は94.1％である。"

    assert build._numeric_claims(draft) == build._numeric_claims(polished)


def test_real_value_changes_are_still_caught() -> None:
    draft = "収入は4.5％増。患者数は298人増。"

    assert build._numeric_claims(draft) != build._numeric_claims("収入は5.5％増。患者数は298人増。")
    assert build._numeric_claims(draft) != build._numeric_claims("収入は4.5％増。患者数は300人増。")
    # 単位の取り違えも見逃さない
    assert build._numeric_claims("4.5ポイント") != build._numeric_claims("4.5％")
    # 数値の欠落も
    assert build._numeric_claims(draft) != build._numeric_claims("収入は4.5％増。")


def test_number_mismatch_reports_which_values_differ(monkeypatch) -> None:
    verified = "《入院》\nⅠ．収入は10％増。\nⅡ．1人増。\nⅢ．1件増。\nⅣ．稼働率80％。\n《外来》\nⅠ．収入は10％増。\nⅤ．1人増。\n《まとめ》\n・10％増。"
    changed = verified.replace("稼働率80％", "稼働率99％")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            import json as _json

            return ('{"message":{"content":' + _json.dumps(changed) + "}}").encode()

    monkeypatch.setattr(build.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    notes: list[str] = []
    build.refine_comment_with_ollama({"sheet": "分析", "facts": []}, verified, "", "", notes)

    # 「変わった」だけでは調べようがない。何が増えて何が消えたかを出す
    assert notes and "99%" in notes[0] and "80%" in notes[0]


def test_ollama_refinement_falls_back_when_numbers_change(monkeypatch) -> None:
    verified = "《入院》\nⅠ．収入はR7年度比10％増加。\nⅡ．1人増加。\nⅢ．1件増加。\nⅣ．稼働率80％。\n《外来》\nⅠ．収入は10％増加。\nⅤ．1人増加。\n《まとめ》\n・10％増加。"

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            changed = verified.replace("10％", "99％", 1)
            return ('{"message":{"content":' + __import__("json").dumps(changed) + '}}').encode()

    monkeypatch.setattr(build.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    assert build.refine_comment_with_ollama({"sheet": "分析", "facts": []}, verified, "") == verified


def test_verified_comment_uses_h30_and_r6_comparisons() -> None:
    def record(kind, section, metric, previous_year, previous, latest, change, absolute):
        return {
            "kind": kind,
            "section": section,
            "metric": metric,
            "previous_year": previous_year,
            "previous_value": previous,
            "latest_year": "R8年度",
            "latest_value": latest,
            "change": change,
            "absolute_change": absolute,
        }

    records = [
        record("comparison", "入院", "③患者数の推移", "R7年度", 4891, 5032, 2.9, 141),
        record("long_comparison", "入院", "③患者数の推移", "H30年度", 9000, 5032, -44.1, -3968),
        record("comparison", "入院", "④手術件数", "R7年度", 95, 98, 3.2, 3),
        record("trend_comparison", "入院", "④手術件数", "R6年度", 94, 98, 4.3, 4),
        record("comparison", "外来", "1日あたり初診患者数", "R7年度", 20, 19, -5, -1),
        record("long_comparison", "外来", "1日あたり初診患者数", "H30年度", 29, 19, -34.5, -10),
        {
            "kind": "target", "section": "外来", "metric": "逆紹介率",
            "target": 50.0, "latest_value": 41.0, "gap": -9.0,
        },
    ]

    assert "H30年度との比較では3,968人減少" in build.build_verified_comment({"fact_records": records})


def test_missing_values_are_not_reported_as_zero() -> None:
    # データが取れないシートで「稼働率は0％である」と実在しない実績を書いていた
    comment = build.build_verified_comment({"fact_records": []})

    assert "稼働率は比較可能な値がない。" in comment
    assert "0％である" not in comment
    # まとめ行が「・。」だけになるのも防ぐ
    assert "・。" not in comment
    assert "目標と比較できる指標がない" in comment


def test_style_template_is_passed_to_the_writer_when_present(
    tmp_path, monkeypatch
) -> None:
    """
    templates/gui のひな型Excelがあれば、書込み時に書式の正本として渡す。

    利用者がExcelで書式(フォント・サイズ・色・太さ)を変えれば、
    コードを触らずに次の生成から出力へ反映されるという約束の入口。
    """
    source = tmp_path / "work" / "sample.xlsx"
    source.parent.mkdir()
    source.write_bytes(b"original")
    updated = tmp_path / "output" / "sample_updated.xlsx"
    template = tmp_path / build.STYLE_TEMPLATE
    template.parent.mkdir(parents=True)
    template.write_bytes(b"template")
    office_args: list[list[str]] = []

    base = _fake_python(updated)

    def run(python, root, script, args):
        if script.endswith("office_excel.py"):
            office_args.append(args)
        return base(python, root, script, args)

    monkeypatch.setattr(build, "_run_python", run)
    _skip_polish(monkeypatch)
    build.process_excel_comment(tmp_path, Path("python.exe"), source)

    assert office_args and "--style-template" in office_args[0]
    assert office_args[0][office_args[0].index("--style-template") + 1] == str(template)


def test_style_template_notes_reach_the_caller(tmp_path, monkeypatch) -> None:
    # 適用できなかった理由が無言で消えると、書式が変わらない原因を調べようがない
    source = tmp_path / "work" / "sample.xlsx"
    source.parent.mkdir()
    source.write_bytes(b"original")
    updated = tmp_path / "output" / "sample_updated.xlsx"

    base = _fake_python(updated)

    def run(python, root, script, args):
        output = base(python, root, script, args)
        if script.endswith("office_excel.py"):
            return "書式テンプレートを適用できません: 名前がありません\n" + output
        return output

    monkeypatch.setattr(build, "_run_python", run)
    _skip_polish(monkeypatch)
    result = build.process_excel_comment(tmp_path, Path("python.exe"), source)

    assert any("書式テンプレートを適用できません" in note for note in result.notes)


def test_kilo_plugin_delegates_instead_of_reimplementing() -> None:
    source = PLUGIN.read_text(encoding="utf-8")

    # Kilo経由でもOpen WebUI経由でも同じ実装を通す(過去に食い違った)
    assert 'runPython(directory, "tools/excel_comment_build.py"' in source
    for reimplemented in (
        "buildVerifiedComment",
        "numericFactMismatch",
        "REQUIRED_COMMENT_STRUCTURE",
        "127.0.0.1:11434",
    ):
        assert reimplemented not in source, f"プラグインに {reimplemented} が残っている"


def test_確認用レポートは既定で作らない(monkeypatch, tmp_path) -> None:
    """
    利用者が要らないと判断した(2026-08-18)。作るぶん待ち時間が延び、
    返るファイルも2つになる。既定で作らないことを固定する。
    """
    import inspect

    source = inspect.getsource(build.process_excel_comment)
    assert "make_report: bool = False" in inspect.getsource(build.process_excel_comment) or (
        "make_report" in str(inspect.signature(build.process_excel_comment))
    )
    assert "if make_report and" in source, "レポート生成が既定で走る形に戻っている"
    # レポート単体は別ツールで後から作れる
    assert (build.ROOT / "tools" / "excel_comment_report.py").is_file()


def test_reportを付けたときだけレポートを作る() -> None:
    import inspect

    signature = inspect.signature(build.process_excel_comment)
    assert signature.parameters["make_report"].default is False


# ---------------------------------------------------------------------------
# 整形へ渡す材料から数字を除く
# ---------------------------------------------------------------------------


def test_参考用の文章から数字だけ伏せる() -> None:
    masked = build._mask_numbers("入院収入は6.2％増、患者数は1,234人増加している。")
    assert "6.2" not in masked and "1,234" not in masked
    assert "入院収入" in masked and "増加している" in masked, "言い回しまで消している"


def test_整形プロンプトに表の生数値を渡さない(monkeypatch) -> None:
    """
    プロンプトに数字が229個あり、使ってよいのは下書きの33個だけだった。
    8シート中7シートで数値ガードに退避していた(2026-08-18に実測)。
    """
    import json as _json

    sent = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"message":{"content":""}}'

    def fake_urlopen(request, *args, **kwargs):
        sent["prompt"] = _json.loads(request.data.decode("utf-8"))["messages"][0]["content"]
        return _Response()

    monkeypatch.setattr(build.urllib.request, "urlopen", fake_urlopen)

    context = {
        "sheet": "見本(案)",
        "facts": ["入院収入 123456円", "患者数 789人", "稼働率 88.88%"],
        "template": "入院収入は9.9％増加している。",
    }
    build.refine_comment_with_ollama(context, "《入院》Ⅰ．収入は6.2％増加している。", "", "見本は4.4％増。", [])

    prompt = sent["prompt"]
    assert "123456" not in prompt and "789" not in prompt, "検証済み事実の生数値を渡している"
    assert "9.9" not in prompt and "4.4" not in prompt, "ひな形の数値を渡している"
    assert "6.2" in prompt, "下書きの数値まで消している"
    assert "◯" in prompt, "数字を伏せた参考文が渡っていない"
